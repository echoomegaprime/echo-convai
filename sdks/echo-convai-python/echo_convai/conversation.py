"""Echo ConvAI SDK — WebSocket conversation handler.

Manages the full lifecycle of a real-time conversation with an Echo ConvAI agent:
connection, audio streaming, transcript callbacks, tool calls, ping/pong keepalive,
interruption handling, and graceful shutdown.

Usage::

    from echo_convai import Conversation

    async with Conversation(
        agent_id="agent_xxx",
        server_url="wss://echo-convai.example.com",
        api_key="your-key",
    ) as conv:
        conv.on_transcript(lambda t: print(f"User said: {t}"))
        conv.on_agent_response(lambda r: print(f"Agent: {r}"))
        await conv.send_message("Hello!")
        await conv.wait_until_done()
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Awaitable, Callable

import websockets
import websockets.asyncio.client
from loguru import logger

from echo_convai.types import (
    AgentChatResponsePartType,
    AudioEventData,
    ClientToolCallData,
    ClientToolResult,
    ContextualUpdate,
    ConversationConfigOverride,
    ConversationInitiation,
    ConversationState,
    FeedbackMessage,
    FeedbackType,
    InterruptionEventData,
    PingEventData,
    PongMessage,
    ServerEventType,
    TranscriptEventData,
    UserActivity,
    UserAudioChunk,
    UserMessage,
)

# Type aliases for callback signatures
TranscriptCallback = Callable[[str], Awaitable[None] | None]
AgentResponseCallback = Callable[[str], Awaitable[None] | None]
AudioCallback = Callable[[bytes], Awaitable[None] | None]
ErrorCallback = Callable[[str, str], Awaitable[None] | None]
InterruptionCallback = Callable[[], Awaitable[None] | None]
ToolCallCallback = Callable[[str, str, dict[str, Any]], Awaitable[str] | str]
StreamPartCallback = Callable[[str, AgentChatResponsePartType], Awaitable[None] | None]
LatencyCallback = Callable[[int], Awaitable[None] | None]
EndSessionCallback = Callable[[], Awaitable[None] | None]

WS_MAX_SIZE = 16 * 1024 * 1024  # 16 MB
RECV_TIMEOUT = 0.5  # seconds


class Conversation:
    """Real-time WebSocket conversation with an Echo ConvAI agent.

    Supports both audio and text-only modes. Handles the complete ElevenLabs-compatible
    WebSocket protocol including ping/pong keepalive, interruption tracking, client
    tool calls, and streaming text responses.

    Args:
        agent_id: The agent to converse with.
        server_url: WebSocket base URL (e.g., ``wss://echo-convai.example.com``).
        api_key: API key for authentication (sent as subprotocol or signed URL).
        signed_url: Pre-authenticated WebSocket URL (overrides server_url + api_key).
        text_only: If True, disables audio and uses text-only mode.
        config_override: Optional conversation config overrides.
        user_id: Optional user identifier for the session.
        dynamic_variables: Template variables injected into the agent prompt.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        server_url: str = "wss://api.elevenlabs.io",
        api_key: str | None = None,
        signed_url: str | None = None,
        text_only: bool = False,
        config_override: ConversationConfigOverride | None = None,
        user_id: str | None = None,
        dynamic_variables: dict[str, str] | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._server_url = server_url.rstrip("/")
        self._api_key = api_key
        self._signed_url = signed_url
        self._text_only = text_only
        self._user_id = user_id
        self._dynamic_variables = dynamic_variables or {}

        # Build config override
        self._config_override = config_override or ConversationConfigOverride()
        if self._text_only:
            self._config_override.text_only = True

        # State
        self._state = ConversationState.IDLE
        self._conversation_id: str | None = None
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._done_event = asyncio.Event()
        self._last_interrupt_id: int = -1

        # Callbacks
        self._on_transcript: TranscriptCallback | None = None
        self._on_tentative_transcript: TranscriptCallback | None = None
        self._on_agent_response: AgentResponseCallback | None = None
        self._on_agent_response_correction: Callable[[str, str], Awaitable[None] | None] | None = None
        self._on_stream_part: StreamPartCallback | None = None
        self._on_audio: AudioCallback | None = None
        self._on_error: ErrorCallback | None = None
        self._on_interruption: InterruptionCallback | None = None
        self._on_latency: LatencyCallback | None = None
        self._on_end_session: EndSessionCallback | None = None
        self._tool_handlers: dict[str, ToolCallCallback] = {}

        logger.debug(
            "Conversation created | agent_id={} text_only={} server={}",
            agent_id,
            text_only,
            self._server_url,
        )

    # ───────────────────────────────────────────────────────────────────────
    # Properties
    # ───────────────────────────────────────────────────────────────────────

    @property
    def state(self) -> ConversationState:
        """Current conversation state."""
        return self._state

    @property
    def conversation_id(self) -> str | None:
        """Server-assigned conversation ID (available after connect)."""
        return self._conversation_id

    @property
    def is_connected(self) -> bool:
        """Whether the WebSocket is currently connected."""
        return self._state == ConversationState.CONNECTED

    # ───────────────────────────────────────────────────────────────────────
    # Callback registration
    # ───────────────────────────────────────────────────────────────────────

    def on_transcript(self, callback: TranscriptCallback) -> None:
        """Register callback for final user transcript events."""
        self._on_transcript = callback

    def on_tentative_transcript(self, callback: TranscriptCallback) -> None:
        """Register callback for interim/tentative user transcript events."""
        self._on_tentative_transcript = callback

    def on_agent_response(self, callback: AgentResponseCallback) -> None:
        """Register callback for complete agent response events."""
        self._on_agent_response = callback

    def on_agent_response_correction(self, callback: Callable[[str, str], Awaitable[None] | None]) -> None:
        """Register callback for agent response correction events."""
        self._on_agent_response_correction = callback

    def on_stream_part(self, callback: StreamPartCallback) -> None:
        """Register callback for streaming agent text response parts."""
        self._on_stream_part = callback

    def on_audio(self, callback: AudioCallback) -> None:
        """Register callback for received agent audio chunks (raw PCM bytes)."""
        self._on_audio = callback

    def on_error(self, callback: ErrorCallback) -> None:
        """Register callback for protocol error events."""
        self._on_error = callback

    def on_interruption(self, callback: InterruptionCallback) -> None:
        """Register callback for user interruption events."""
        self._on_interruption = callback

    def on_latency(self, callback: LatencyCallback) -> None:
        """Register callback for latency measurement (ping round-trip ms)."""
        self._on_latency = callback

    def on_end_session(self, callback: EndSessionCallback) -> None:
        """Register callback fired when the session ends."""
        self._on_end_session = callback

    def register_tool(self, tool_name: str, handler: ToolCallCallback) -> None:
        """Register a client-side tool handler.

        The handler receives (tool_call_id, tool_name, parameters) and must return
        a string result. Can be sync or async.

        Args:
            tool_name: Name matching the tool definition on the agent.
            handler: Callable that processes the tool call and returns a result string.
        """
        self._tool_handlers[tool_name] = handler
        logger.debug("Tool registered | name={}", tool_name)

    # ───────────────────────────────────────────────────────────────────────
    # Connection lifecycle
    # ───────────────────────────────────────────────────────────────────────

    def _build_ws_url(self) -> str:
        """Construct the WebSocket URL with query parameters."""
        if self._signed_url:
            separator = "&" if "?" in self._signed_url else "?"
            return f"{self._signed_url}{separator}source=echo_convai_python_sdk&version=0.1.0"
        url = f"{self._server_url}/v1/convai/conversation?agent_id={self._agent_id}"
        url += "&source=echo_convai_python_sdk&version=0.1.0"
        return url

    def _build_extra_headers(self) -> dict[str, str]:
        """Build additional headers for the WebSocket handshake."""
        headers: dict[str, str] = {}
        if self._api_key and not self._signed_url:
            headers["xi-api-key"] = self._api_key
        return headers

    async def connect(self) -> None:
        """Open the WebSocket connection and start the receive loop.

        Sends the conversation initiation message immediately after connecting.
        The receive loop runs as a background task processing server events.
        """
        if self._state != ConversationState.IDLE:
            raise RuntimeError(f"Cannot connect from state {self._state.value}")

        self._state = ConversationState.CONNECTING
        ws_url = self._build_ws_url()
        extra_headers = self._build_extra_headers()

        logger.info("Connecting to {} ...", ws_url[:80] + "...")

        self._ws = await websockets.asyncio.client.connect(
            ws_url,
            max_size=WS_MAX_SIZE,
            additional_headers=extra_headers,
        )

        # Send initiation message
        initiation = ConversationInitiation(
            conversation_config_override=self._config_override,
            dynamic_variables=self._dynamic_variables,
            user_id=self._user_id,
        )
        await self._ws.send(initiation.model_dump_json(exclude_none=True))
        logger.debug("Initiation message sent")

        self._state = ConversationState.CONNECTED
        self._done_event.clear()

        # Start background receive loop
        self._recv_task = asyncio.create_task(self._receive_loop(), name="convai-recv")
        logger.info("Connected and receive loop started")

    async def disconnect(self) -> None:
        """Gracefully close the WebSocket connection."""
        if self._state not in (ConversationState.CONNECTED, ConversationState.CONNECTING):
            return

        self._state = ConversationState.DISCONNECTING
        logger.info("Disconnecting conversation_id={}", self._conversation_id)

        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            try:
                await self._ws.close()
            except Exception as exc:
                logger.debug("WS close error (ignored): {}", exc)

        self._state = ConversationState.DISCONNECTED
        self._done_event.set()
        await self._fire_callback(self._on_end_session)
        logger.info("Disconnected")

    async def wait_until_done(self) -> str | None:
        """Block until the conversation ends. Returns the conversation_id."""
        await self._done_event.wait()
        return self._conversation_id

    # ───────────────────────────────────────────────────────────────────────
    # Context manager
    # ───────────────────────────────────────────────────────────────────────

    async def __aenter__(self) -> Conversation:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    # ───────────────────────────────────────────────────────────────────────
    # Sending messages
    # ───────────────────────────────────────────────────────────────────────

    async def _send_json(self, data: dict[str, Any] | str) -> None:
        """Send a JSON message over the WebSocket."""
        if not self._ws or self._state != ConversationState.CONNECTED:
            raise RuntimeError("Not connected")
        payload = data if isinstance(data, str) else json.dumps(data)
        await self._ws.send(payload)

    async def send_audio(self, chunk: bytes) -> None:
        """Send a raw PCM audio chunk to the server.

        Args:
            chunk: Raw 16-bit PCM mono audio bytes (default 16kHz).
                   Will be base64-encoded before sending.
        """
        encoded = base64.b64encode(chunk).decode("ascii")
        msg = UserAudioChunk(user_audio_chunk=encoded)
        await self._send_json(msg.model_dump())

    async def send_message(self, text: str) -> None:
        """Send a text message to the agent.

        Args:
            text: The user's text input.
        """
        msg = UserMessage(text=text)
        await self._send_json(msg.model_dump())
        logger.debug("Text message sent | len={}", len(text))

    async def send_contextual_update(self, text: str) -> None:
        """Send a non-interrupting context update to the agent.

        This injects context into the conversation without triggering a response
        or interrupting the agent's current speech.

        Args:
            text: Context text to inject.
        """
        msg = ContextualUpdate(text=text)
        await self._send_json(msg.model_dump())
        logger.debug("Contextual update sent | len={}", len(text))

    async def send_activity(self) -> None:
        """Send a user activity signal (prevents timeout, pauses agent ~2s)."""
        msg = UserActivity()
        await self._send_json(msg.model_dump())

    async def send_feedback(self, feedback: FeedbackType, rating: int | None = None) -> None:
        """Send conversation quality feedback.

        Args:
            feedback: LIKE or DISLIKE.
            rating: Optional numeric rating.
        """
        msg = FeedbackMessage(feedback=feedback, rating=rating)
        await self._send_json(msg.model_dump(exclude_none=True))
        logger.debug("Feedback sent | type={}", feedback.value)

    async def end_session(self) -> None:
        """End the current conversation session."""
        await self.disconnect()

    # ───────────────────────────────────────────────────────────────────────
    # Receive loop and message dispatch
    # ───────────────────────────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        """Background task: receive and dispatch WebSocket messages."""
        if not self._ws:
            return

        try:
            async for raw_message in self._ws:
                try:
                    if isinstance(raw_message, bytes):
                        logger.warning("Unexpected binary message received, skipping")
                        continue
                    data = json.loads(raw_message)
                    await self._handle_message(data)
                except json.JSONDecodeError as exc:
                    logger.error("Failed to parse WS message: {}", exc)
                except Exception as exc:
                    logger.error("Error handling message: {} | data={}", exc, raw_message[:200] if raw_message else "")
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("WebSocket closed | code={} reason={}", exc.code, exc.reason)
        except asyncio.CancelledError:
            logger.debug("Receive loop cancelled")
            return
        except Exception as exc:
            logger.error("Receive loop error: {}", exc)

        # Connection ended
        if self._state == ConversationState.CONNECTED:
            self._state = ConversationState.DISCONNECTED
            self._done_event.set()
            await self._fire_callback(self._on_end_session)

    async def _handle_message(self, data: dict[str, Any]) -> None:
        """Dispatch a parsed server message to the appropriate handler."""
        msg_type = data.get("type")
        if msg_type is None:
            logger.warning("Received message with no type field: {}", list(data.keys()))
            return

        try:
            event_type = ServerEventType(msg_type)
        except ValueError:
            logger.debug("Unknown server event type: {}", msg_type)
            return

        handler = self._MESSAGE_HANDLERS.get(event_type)
        if handler:
            await handler(self, data)
        else:
            logger.debug("Unhandled event type: {}", msg_type)

    # ───────────────────────────────────────────────────────────────────────
    # Individual message handlers
    # ───────────────────────────────────────────────────────────────────────

    async def _handle_initiation_metadata(self, data: dict[str, Any]) -> None:
        """Handle conversation_initiation_metadata — extract conversation_id."""
        meta = data.get("conversation_initiation_metadata_event", {})
        self._conversation_id = meta.get("conversation_id")
        logger.info("Conversation initiated | id={}", self._conversation_id)

    async def _handle_audio(self, data: dict[str, Any]) -> None:
        """Handle audio event — decode base64 PCM and fire callback."""
        event = data.get("audio_event", {})
        event_id = int(event.get("event_id", 0))

        # Drop audio from before the last interruption
        if event_id <= self._last_interrupt_id:
            return

        audio_b64 = event.get("audio_base_64", "")
        if not audio_b64:
            return

        audio_bytes = base64.b64decode(audio_b64)
        if self._on_audio:
            await self._fire_callback(self._on_audio, audio_bytes)

    async def _handle_user_transcript(self, data: dict[str, Any]) -> None:
        """Handle user_transcript — final ASR result."""
        event = data.get("user_transcription_event", {})
        transcript = event.get("user_transcript", "")
        if transcript and self._on_transcript:
            await self._fire_callback(self._on_transcript, transcript)

    async def _handle_tentative_transcript(self, data: dict[str, Any]) -> None:
        """Handle tentative_user_transcript — interim ASR result."""
        event = data.get("user_transcription_event", {})
        transcript = event.get("user_transcript", "")
        if transcript and self._on_tentative_transcript:
            await self._fire_callback(self._on_tentative_transcript, transcript)

    async def _handle_agent_response(self, data: dict[str, Any]) -> None:
        """Handle agent_response — complete LLM response text."""
        event = data.get("agent_response_event", {})
        response = event.get("agent_response", "")
        if response and self._on_agent_response:
            await self._fire_callback(self._on_agent_response, response)

    async def _handle_agent_response_correction(self, data: dict[str, Any]) -> None:
        """Handle agent_response_correction — safety-filtered correction."""
        event = data.get("agent_response_correction_event", {})
        original = event.get("original_agent_response", "")
        corrected = event.get("corrected_agent_response", "")
        if self._on_agent_response_correction:
            await self._fire_callback(self._on_agent_response_correction, original, corrected)

    async def _handle_stream_part(self, data: dict[str, Any]) -> None:
        """Handle agent_chat_response_part — streaming text chunk."""
        event = data.get("agent_chat_response_part_event", data.get("text_response_part", {}))
        part_type_str = event.get("type", "delta")
        content = event.get("text", event.get("content", ""))
        try:
            part_type = AgentChatResponsePartType(part_type_str)
        except ValueError:
            part_type = AgentChatResponsePartType.DELTA
        if self._on_stream_part:
            await self._fire_callback(self._on_stream_part, content, part_type)

    async def _handle_interruption(self, data: dict[str, Any]) -> None:
        """Handle interruption — user interrupted agent speech."""
        event = data.get("interruption_event", {})
        event_id = int(event.get("event_id", 0))
        self._last_interrupt_id = event_id
        logger.debug("Interruption received | event_id={}", event_id)
        if self._on_interruption:
            await self._fire_callback(self._on_interruption)

    async def _handle_ping(self, data: dict[str, Any]) -> None:
        """Handle ping — respond with pong for keepalive."""
        event = data.get("ping_event", {})
        event_id = event.get("event_id", 0)
        pong = PongMessage(event_id=event_id)
        await self._send_json(pong.model_dump())

        ping_ms = event.get("ping_ms")
        if ping_ms is not None and self._on_latency:
            await self._fire_callback(self._on_latency, int(ping_ms))

    async def _handle_client_tool_call(self, data: dict[str, Any]) -> None:
        """Handle client_tool_call — execute registered tool and return result."""
        call_data = data.get("client_tool_call", {})
        parsed = ClientToolCallData.model_validate(call_data)

        handler = self._tool_handlers.get(parsed.tool_name)
        if handler is None:
            logger.warning("No handler for tool: {}", parsed.tool_name)
            result = ClientToolResult(
                tool_call_id=parsed.tool_call_id,
                result=f"Error: no handler registered for tool '{parsed.tool_name}'",
                is_error=True,
            )
            await self._send_json(result.model_dump())
            return

        try:
            handler_result = handler(parsed.tool_call_id, parsed.tool_name, parsed.parameters)
            if asyncio.iscoroutine(handler_result):
                handler_result = await handler_result
            result_str = str(handler_result) if handler_result is not None else ""
            result = ClientToolResult(
                tool_call_id=parsed.tool_call_id,
                result=result_str,
                is_error=False,
            )
            logger.debug("Tool executed | name={} call_id={}", parsed.tool_name, parsed.tool_call_id)
        except Exception as exc:
            logger.error("Tool execution failed | name={} error={}", parsed.tool_name, exc)
            result = ClientToolResult(
                tool_call_id=parsed.tool_call_id,
                result=f"Error: {exc}",
                is_error=True,
            )
        await self._send_json(result.model_dump())

    async def _handle_error(self, data: dict[str, Any]) -> None:
        """Handle client_error — protocol/server error."""
        event = data.get("error_event", {})
        message = event.get("message", "Unknown error")
        code = event.get("code", "UNKNOWN")
        logger.error("Server error | code={} message={}", code, message)
        if self._on_error:
            await self._fire_callback(self._on_error, code, message)

    async def _handle_vad_score(self, data: dict[str, Any]) -> None:
        """Handle vad_score — voice activity detection score (logged only)."""
        event = data.get("vad_score_event", {})
        score = event.get("score", 0.0)
        logger.trace("VAD score: {:.2f}", score)

    async def _handle_guardrail(self, data: dict[str, Any]) -> None:
        """Handle guardrail_triggered — safety guardrail activation."""
        event = data.get("guardrail_event", {})
        logger.warning("Guardrail triggered: {}", event)

    async def _handle_agent_tool_request(self, data: dict[str, Any]) -> None:
        """Handle agent_tool_request — tool execution lifecycle event."""
        event = data.get("agent_tool_request_event", {})
        logger.debug("Agent tool request: {}", event.get("tool_name", "unknown"))

    async def _handle_agent_tool_response(self, data: dict[str, Any]) -> None:
        """Handle agent_tool_response — tool execution result."""
        event = data.get("agent_tool_response_event", {})
        logger.debug("Agent tool response: {}", event.get("tool_name", "unknown"))

    async def _handle_agent_response_metadata(self, data: dict[str, Any]) -> None:
        """Handle agent_response_metadata — response metadata (logged only)."""
        logger.debug("Agent response metadata received")

    async def _handle_turn_probability(self, data: dict[str, Any]) -> None:
        """Handle internal_turn_probability — turn detection score (logged only)."""
        event = data.get("turn_probability_event", {})
        logger.trace("Turn probability: {:.2f}", event.get("probability", 0.0))

    # Handler dispatch table
    _MESSAGE_HANDLERS: dict[ServerEventType, Callable[..., Awaitable[None]]] = {
        ServerEventType.CONVERSATION_INITIATION_METADATA: _handle_initiation_metadata,
        ServerEventType.AUDIO: _handle_audio,
        ServerEventType.USER_TRANSCRIPT: _handle_user_transcript,
        ServerEventType.TENTATIVE_USER_TRANSCRIPT: _handle_tentative_transcript,
        ServerEventType.AGENT_RESPONSE: _handle_agent_response,
        ServerEventType.AGENT_RESPONSE_CORRECTION: _handle_agent_response_correction,
        ServerEventType.AGENT_CHAT_RESPONSE_PART: _handle_stream_part,
        ServerEventType.INTERRUPTION: _handle_interruption,
        ServerEventType.PING: _handle_ping,
        ServerEventType.CLIENT_TOOL_CALL: _handle_client_tool_call,
        ServerEventType.CLIENT_ERROR: _handle_error,
        ServerEventType.VAD_SCORE: _handle_vad_score,
        ServerEventType.GUARDRAIL_TRIGGERED: _handle_guardrail,
        ServerEventType.AGENT_TOOL_REQUEST: _handle_agent_tool_request,
        ServerEventType.AGENT_TOOL_RESPONSE: _handle_agent_tool_response,
        ServerEventType.AGENT_RESPONSE_METADATA: _handle_agent_response_metadata,
        ServerEventType.INTERNAL_TURN_PROBABILITY: _handle_turn_probability,
    }

    # ───────────────────────────────────────────────────────────────────────
    # Callback execution helper
    # ───────────────────────────────────────────────────────────────────────

    @staticmethod
    async def _fire_callback(callback: Callable[..., Any] | None, *args: Any) -> None:
        """Fire a callback, handling both sync and async callables."""
        if callback is None:
            return
        try:
            result = callback(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.error("Callback error: {}", exc)
