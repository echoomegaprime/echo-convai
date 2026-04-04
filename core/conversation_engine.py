"""Echo ConvAI — Conversation Engine.

Core orchestrator that manages the full real-time conversation pipeline:
  Audio In → ASR → Turn-Taking → LLM → TTS → Audio Out

Reverse-engineered from ElevenLabs Conversational AI platform.
This is the central coordinator that wires ASR, LLM, TTS, and turn-taking together
over a WebSocket connection, speaking the exact same protocol as ElevenLabs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from loguru import logger
from pydantic import BaseModel, Field

from protocols.websocket_protocol import (
    AudioFormat,
    ClientEventType,
    ConversationConfigOverride,
    ConversationInitiation,
    ServerMessageType,
    TurnEagerness,
    TurnMode,
    UserAudioChunk,
    UserMessage,
    ContextualUpdate,
    PongMessage,
    ClientToolResult,
    FeedbackMessage,
    UserActivity,
    build_server_message,
    generate_conversation_id,
    generate_event_id,
    parse_client_message,
)


# ═══════════════════════════════════════════════════════════════════════════════
# CONVERSATION STATE
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationState(BaseModel):
    """Tracks the full state of an active conversation."""
    conversation_id: str = Field(default_factory=generate_conversation_id)
    agent_id: str = ""
    user_id: str | None = None
    started_at: float = Field(default_factory=time.time)
    max_duration_seconds: int = 1800
    is_active: bool = True

    # Audio config
    input_audio_format: AudioFormat = AudioFormat.PCM_16000
    output_audio_format: AudioFormat = AudioFormat.PCM_16000

    # Conversation history (for LLM context)
    messages: list[dict[str, str]] = Field(default_factory=list)
    system_prompt: str = ""
    first_message: str | None = None
    language: str = "en"

    # Dynamic variables (injected per-conversation)
    dynamic_variables: dict[str, str] = Field(default_factory=dict)

    # Turn-taking state
    turn_mode: TurnMode = TurnMode.TURN
    turn_eagerness: TurnEagerness = TurnEagerness.NORMAL
    is_agent_speaking: bool = False
    is_user_speaking: bool = False
    last_user_audio_time: float = 0.0
    speculative_response: str | None = None

    # Tool state
    pending_tool_calls: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # Metrics
    total_agent_tokens: int = 0
    total_user_messages: int = 0
    total_audio_chunks_in: int = 0
    total_audio_chunks_out: int = 0

    # Events subscribed by client
    client_events: list[str] = Field(default_factory=lambda: [
        "audio", "interruption", "agent_response", "user_transcript",
    ])


class AgentDefinition(BaseModel):
    """Agent configuration loaded from database."""
    agent_id: str
    name: str = "Echo Agent"
    system_prompt: str = ""
    first_message: str | None = None
    language: str = "en"
    voice_id: str = ""
    tts_model: str = "eleven_v3_conversational"
    llm_model: str = "gemini-2.5-flash"
    llm_temperature: float = 0.85
    llm_max_tokens: int = 400
    tools: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    turn_timeout: float = 15.0
    turn_eagerness: TurnEagerness = TurnEagerness.NORMAL
    max_duration_seconds: int = 1800
    is_public: bool = True


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE INTERFACES (implemented by ASR, LLM, TTS, Turn-Taking modules)
# ═══════════════════════════════════════════════════════════════════════════════

class ASRInterface:
    """Abstract interface for speech-to-text."""

    async def process_audio_chunk(self, audio_b64: str, conversation_id: str) -> None:
        """Feed an audio chunk to the ASR pipeline."""
        raise NotImplementedError

    async def get_transcript(self, conversation_id: str) -> dict[str, Any] | None:
        """Get latest transcript (tentative or final)."""
        raise NotImplementedError

    async def reset(self, conversation_id: str) -> None:
        """Reset ASR state for new utterance."""
        raise NotImplementedError


class LLMInterface:
    """Abstract interface for language model."""

    async def generate_response(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.85,
        max_tokens: int = 400,
        model: str = "gemini-2.5-flash",
    ) -> AsyncIterator[str]:
        """Stream LLM response tokens."""
        raise NotImplementedError

    async def generate_tool_call(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        model: str = "gemini-2.5-flash",
    ) -> dict[str, Any] | None:
        """Check if LLM wants to call a tool."""
        raise NotImplementedError


class TTSInterface:
    """Abstract interface for text-to-speech."""

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        model: str = "eleven_v3_conversational",
        output_format: AudioFormat = AudioFormat.PCM_16000,
    ) -> AsyncIterator[bytes]:
        """Stream synthesized audio chunks."""
        raise NotImplementedError


class TurnTakingInterface:
    """Abstract interface for turn-taking decisions."""

    async def update_vad(self, audio_chunk: bytes) -> float:
        """Process audio and return VAD score (0-1)."""
        raise NotImplementedError

    async def predict_turn_end(self, vad_score: float, silence_ms: int, transcript: str) -> float:
        """Predict probability that user has finished speaking (0-1)."""
        raise NotImplementedError

    def get_threshold(self, eagerness: TurnEagerness) -> float:
        """Get turn-end threshold based on eagerness setting."""
        thresholds = {
            TurnEagerness.PATIENT: 0.85,
            TurnEagerness.NORMAL: 0.65,
            TurnEagerness.EAGER: 0.40,
        }
        return thresholds.get(eagerness, 0.65)


# ═══════════════════════════════════════════════════════════════════════════════
# CONVERSATION ENGINE — The Core Orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationEngine:
    """Orchestrates real-time voice conversations.

    This is the heart of the system — it wires together ASR, LLM, TTS, and
    turn-taking into a coherent real-time pipeline, communicating over WebSocket
    using the exact same protocol as ElevenLabs Conversational AI.

    Pipeline:
        User Audio → [VAD] → [ASR] → transcript
                                        ↓
        [Turn-Taking] decides when user is done
                                        ↓
        [RAG retrieval] → [LLM] → streaming text response
                                        ↓
        [TTS] → streaming audio → client
    """

    def __init__(
        self,
        asr: ASRInterface,
        llm: LLMInterface,
        tts: TTSInterface,
        turn_taking: TurnTakingInterface,
        send_message: Callable[[dict[str, Any]], Any],
    ):
        self.asr = asr
        self.llm = llm
        self.tts = tts
        self.turn_taking = turn_taking
        self.send_message = send_message
        self.state: ConversationState | None = None
        self.agent: AgentDefinition | None = None

        # Internal queues
        self._audio_queue: asyncio.Queue[str] = asyncio.Queue()
        self._tts_queue: asyncio.Queue[str] = asyncio.Queue()
        self._interrupt_event = asyncio.Event()

        # Background tasks
        self._tasks: list[asyncio.Task] = []
        self._ping_counter = 0

    # ─── LIFECYCLE ────────────────────────────────────────────────────────────

    async def start(self, agent: AgentDefinition, initiation: ConversationInitiation | None = None) -> str:
        """Initialize a new conversation. Returns conversation_id."""
        self.agent = agent
        self.state = ConversationState(
            agent_id=agent.agent_id,
            system_prompt=agent.system_prompt,
            first_message=agent.first_message,
            language=agent.language,
            turn_mode=TurnMode.TURN,
            turn_eagerness=agent.turn_eagerness,
            max_duration_seconds=agent.max_duration_seconds,
        )

        # Apply client overrides
        if initiation and initiation.conversation_config_override:
            self._apply_overrides(initiation.conversation_config_override)

        if initiation and initiation.dynamic_variables:
            self.state.dynamic_variables.update(initiation.dynamic_variables)

        if initiation and initiation.user_id:
            self.state.user_id = initiation.user_id

        # Send initiation metadata
        await self._send_event(ClientEventType.CONVERSATION_INITIATION_METADATA, {
            "conversation_initiation_metadata_event": {
                "conversation_id": self.state.conversation_id,
                "agent_output_audio_format": self.state.output_audio_format.value,
                "user_input_audio_format": self.state.input_audio_format.value,
            }
        })

        # Start background pipelines
        self._tasks.append(asyncio.create_task(self._audio_processing_loop()))
        self._tasks.append(asyncio.create_task(self._ping_loop()))
        self._tasks.append(asyncio.create_task(self._duration_watchdog()))

        # Send first message if configured
        if self.state.first_message:
            resolved_msg = self._resolve_variables(self.state.first_message)
            asyncio.create_task(self._generate_and_speak(resolved_msg, is_first_message=True))

        logger.info(
            f"Conversation started: {self.state.conversation_id} "
            f"agent={agent.agent_id} lang={agent.language}"
        )
        return self.state.conversation_id

    async def stop(self, code: int = 1000, reason: str = "conversation_ended") -> None:
        """Gracefully end the conversation."""
        if self.state:
            self.state.is_active = False

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        logger.info(
            f"Conversation ended: {self.state.conversation_id if self.state else 'unknown'} "
            f"code={code} reason={reason}"
        )

    # ─── MESSAGE HANDLING ─────────────────────────────────────────────────────

    async def handle_message(self, raw: dict[str, Any]) -> None:
        """Route an incoming WebSocket message to the appropriate handler."""
        if not self.state or not self.state.is_active:
            return

        try:
            msg = parse_client_message(raw)
        except ValueError as e:
            logger.warning(f"Unknown message: {e}")
            return

        if isinstance(msg, UserAudioChunk):
            await self._handle_audio(msg)
        elif isinstance(msg, UserMessage):
            await self._handle_text_input(msg)
        elif isinstance(msg, UserActivity):
            await self._handle_user_activity()
        elif isinstance(msg, ContextualUpdate):
            await self._handle_contextual_update(msg)
        elif isinstance(msg, PongMessage):
            self._handle_pong(msg)
        elif isinstance(msg, ClientToolResult):
            await self._handle_tool_result(msg)
        elif isinstance(msg, FeedbackMessage):
            self._handle_feedback(msg)
        elif isinstance(msg, ConversationInitiation):
            pass  # Already handled in start()

    # ─── AUDIO PIPELINE ──────────────────────────────────────────────────────

    async def _handle_audio(self, msg: UserAudioChunk) -> None:
        """Process incoming user audio chunk."""
        self.state.total_audio_chunks_in += 1
        self.state.last_user_audio_time = time.time()
        self.state.is_user_speaking = True

        # Check for interruption
        if self.state.is_agent_speaking:
            await self._handle_interruption()

        # Feed to ASR
        await self.asr.process_audio_chunk(msg.user_audio_chunk, self.state.conversation_id)

        # Feed to VAD for turn-taking
        audio_bytes = base64.b64decode(msg.user_audio_chunk)
        vad_score = await self.turn_taking.update_vad(audio_bytes)

        # Send VAD score to client if subscribed
        if "vad_score" in self.state.client_events:
            await self._send_event(ClientEventType.VAD_SCORE, {
                "vad_score_event": {"score": round(vad_score, 3)}
            })

        # Get latest transcript
        transcript = await self.asr.get_transcript(self.state.conversation_id)
        if transcript:
            if transcript.get("is_final"):
                await self._send_event(ClientEventType.USER_TRANSCRIPT, {
                    "user_transcription_event": {"user_transcript": transcript["text"]}
                })
            else:
                await self._send_event(ClientEventType.TENTATIVE_USER_TRANSCRIPT, {
                    "user_transcription_event": {"user_transcript": transcript["text"]}
                })

    async def _audio_processing_loop(self) -> None:
        """Background loop: detect end-of-turn and trigger LLM response."""
        while self.state and self.state.is_active:
            try:
                await asyncio.sleep(0.05)  # 50ms poll interval

                if not self.state.is_user_speaking:
                    continue

                # Calculate silence duration
                silence_ms = int((time.time() - self.state.last_user_audio_time) * 1000)

                # If we haven't received audio in a while, check turn-taking
                if silence_ms > 200:  # 200ms minimum silence
                    transcript = await self.asr.get_transcript(self.state.conversation_id)
                    transcript_text = transcript["text"] if transcript else ""

                    vad_score = 0.0  # No voice activity during silence
                    turn_prob = await self.turn_taking.predict_turn_end(
                        vad_score, silence_ms, transcript_text
                    )

                    # Send turn probability if subscribed
                    if "internal_turn_probability" in self.state.client_events:
                        await self._send_event(ClientEventType.INTERNAL_TURN_PROBABILITY, {
                            "turn_probability_event": {"probability": round(turn_prob, 3)}
                        })

                    threshold = self.turn_taking.get_threshold(self.state.turn_eagerness)

                    if turn_prob > threshold and transcript_text.strip():
                        self.state.is_user_speaking = False
                        await self.asr.reset(self.state.conversation_id)

                        # Add user message to history
                        self.state.messages.append({
                            "role": "user",
                            "content": transcript_text.strip(),
                        })
                        self.state.total_user_messages += 1

                        # Trigger LLM response
                        asyncio.create_task(
                            self._generate_and_speak(transcript_text.strip())
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Audio processing error: {e}")

    # ─── TEXT INPUT ───────────────────────────────────────────────────────────

    async def _handle_text_input(self, msg: UserMessage) -> None:
        """Handle text-only user input (no audio)."""
        self.state.messages.append({"role": "user", "content": msg.text})
        self.state.total_user_messages += 1

        await self._send_event(ClientEventType.USER_TRANSCRIPT, {
            "user_transcription_event": {"user_transcript": msg.text}
        })

        asyncio.create_task(self._generate_and_speak(msg.text))

    # ─── LLM + TTS PIPELINE ──────────────────────────────────────────────────

    async def _generate_and_speak(self, user_text: str, is_first_message: bool = False) -> None:
        """Generate LLM response and stream it through TTS.

        This is the core response pipeline:
        1. Build messages array with system prompt + history
        2. Stream LLM response tokens
        3. Buffer tokens into sentence-level chunks
        4. Send each chunk through TTS
        5. Stream audio back to client
        """
        self.state.is_agent_speaking = True
        self._interrupt_event.clear()

        try:
            # Build LLM context
            system_prompt = self._resolve_variables(self.state.system_prompt)
            messages = list(self.state.messages)

            if is_first_message:
                # For first message, just speak it directly — no LLM needed
                full_response = user_text
            else:
                # Stream LLM response
                full_response = ""
                sentence_buffer = ""

                # Send streaming start
                await self._send_event(ClientEventType.AGENT_CHAT_RESPONSE_PART, {
                    "agent_chat_response_part_event": {"role": "start", "content": ""}
                })

                async for token in self.llm.generate_response(
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=self.agent.tools if self.agent else None,
                    temperature=self.agent.llm_temperature if self.agent else 0.85,
                    max_tokens=self.agent.llm_max_tokens if self.agent else 400,
                    model=self.agent.llm_model if self.agent else "gemini-2.5-flash",
                ):
                    if self._interrupt_event.is_set():
                        break

                    full_response += token
                    sentence_buffer += token

                    # Send streaming delta
                    await self._send_event(ClientEventType.AGENT_CHAT_RESPONSE_PART, {
                        "agent_chat_response_part_event": {"role": "delta", "content": token}
                    })

                    # When we hit a sentence boundary, send to TTS
                    if self._is_sentence_boundary(sentence_buffer):
                        await self._synthesize_and_send(sentence_buffer.strip())
                        sentence_buffer = ""

                # Flush remaining buffer
                if sentence_buffer.strip() and not self._interrupt_event.is_set():
                    await self._synthesize_and_send(sentence_buffer.strip())

                # Send streaming stop
                await self._send_event(ClientEventType.AGENT_CHAT_RESPONSE_PART, {
                    "agent_chat_response_part_event": {"role": "stop", "content": ""}
                })

            # Send complete agent response
            if full_response and not self._interrupt_event.is_set():
                await self._send_event(ClientEventType.AGENT_RESPONSE, {
                    "agent_response_event": {"agent_response": full_response}
                })

                # Add to conversation history
                self.state.messages.append({
                    "role": "assistant",
                    "content": full_response,
                })

                # For first message, synthesize separately
                if is_first_message:
                    await self._synthesize_and_send(full_response)

        except Exception as e:
            logger.error(f"Response generation error: {e}")
            await self._send_event(ClientEventType.CLIENT_ERROR, {
                "error_event": {"message": str(e), "code": "llm_error"}
            })
        finally:
            self.state.is_agent_speaking = False

    async def _synthesize_and_send(self, text: str) -> None:
        """Synthesize text to audio and send chunks to client."""
        if not text or self._interrupt_event.is_set():
            return

        try:
            voice_id = self.agent.voice_id if self.agent else ""
            tts_model = self.agent.tts_model if self.agent else "eleven_v3_conversational"

            async for audio_chunk in self.tts.synthesize_stream(
                text=text,
                voice_id=voice_id,
                model=tts_model,
                output_format=self.state.output_audio_format,
            ):
                if self._interrupt_event.is_set():
                    break

                audio_b64 = base64.b64encode(audio_chunk).decode("ascii")
                event_id = generate_event_id()

                await self._send_event(ClientEventType.AUDIO, {
                    "audio_event": {
                        "audio_base_64": audio_b64,
                        "event_id": event_id,
                    }
                })
                self.state.total_audio_chunks_out += 1

        except Exception as e:
            logger.error(f"TTS error: {e}")

    # ─── INTERRUPTION HANDLING ────────────────────────────────────────────────

    async def _handle_interruption(self) -> None:
        """Handle user interrupting the agent."""
        self._interrupt_event.set()
        self.state.is_agent_speaking = False

        await self._send_event(ClientEventType.INTERRUPTION, {
            "interruption_event": {"event_id": generate_event_id()}
        })

        logger.debug(f"Interruption detected in {self.state.conversation_id}")

    # ─── TOOL EXECUTION ──────────────────────────────────────────────────────

    async def _handle_tool_result(self, msg: ClientToolResult) -> None:
        """Handle result from client-side tool execution."""
        tool_call_id = msg.tool_call_id
        if tool_call_id in self.state.pending_tool_calls:
            tool_info = self.state.pending_tool_calls.pop(tool_call_id)
            logger.info(f"Tool result received: {tool_info.get('name')} = {msg.result[:100]}")

            # Inject result back into conversation
            self.state.messages.append({
                "role": "tool",
                "content": msg.result,
                "tool_call_id": tool_call_id,
            })

            # Trigger LLM to continue with tool result
            asyncio.create_task(self._generate_and_speak(""))

    async def execute_webhook_tool(self, tool: dict[str, Any], params: dict[str, Any]) -> str:
        """Execute a server-side webhook tool."""
        import aiohttp

        schema = tool.get("api_schema", {})
        url = schema.get("url", "")
        method = schema.get("method", "POST").upper()
        headers = schema.get("headers", {})

        # Resolve dynamic variables in URL and headers
        url = self._resolve_variables(url)
        headers = {k: self._resolve_variables(v) for k, v in headers.items()}

        timeout = aiohttp.ClientTimeout(total=tool.get("response_timeout", 20))

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, json=params, headers=headers) as resp:
                result = await resp.text()
                return result

    # ─── CONTEXTUAL UPDATES ───────────────────────────────────────────────────

    async def _handle_contextual_update(self, msg: ContextualUpdate) -> None:
        """Inject context without interrupting conversation flow."""
        self.state.messages.append({
            "role": "system",
            "content": f"[Context Update] {msg.text}",
        })
        logger.debug(f"Context update: {msg.text[:100]}")

    async def _handle_user_activity(self) -> None:
        """Handle user activity signal (delays agent response ~2s)."""
        self.state.last_user_audio_time = time.time()
        logger.debug("User activity detected — delaying response")

    # ─── FEEDBACK ─────────────────────────────────────────────────────────────

    def _handle_feedback(self, msg: FeedbackMessage) -> None:
        """Record conversation feedback."""
        logger.info(f"Feedback: {msg.feedback.value} (rating={msg.rating})")

    def _handle_pong(self, msg: PongMessage) -> None:
        """Handle latency measurement pong."""
        # Can compute round-trip latency from event_id timestamp
        pass

    # ─── BACKGROUND TASKS ─────────────────────────────────────────────────────

    async def _ping_loop(self) -> None:
        """Send periodic pings for keepalive and latency measurement."""
        while self.state and self.state.is_active:
            try:
                await asyncio.sleep(5.0)
                self._ping_counter += 1
                await self._send_event(ClientEventType.PING, {
                    "ping_event": {
                        "event_id": self._ping_counter,
                        "ping_ms": 0,
                    }
                })
            except asyncio.CancelledError:
                break

    async def _duration_watchdog(self) -> None:
        """Enforce max conversation duration."""
        while self.state and self.state.is_active:
            try:
                await asyncio.sleep(10.0)
                elapsed = time.time() - self.state.started_at
                if elapsed >= self.state.max_duration_seconds:
                    logger.info(f"Max duration reached: {elapsed:.0f}s")
                    await self.stop(code=1000, reason="max_duration_reached")
                    break
            except asyncio.CancelledError:
                break

    # ─── HELPERS ──────────────────────────────────────────────────────────────

    def _apply_overrides(self, overrides: ConversationConfigOverride) -> None:
        """Apply client-provided configuration overrides."""
        if overrides.agent:
            if overrides.agent.first_message is not None:
                self.state.first_message = overrides.agent.first_message
            if overrides.agent.language:
                self.state.language = overrides.agent.language
            if overrides.agent.prompt and overrides.agent.prompt.prompt:
                self.state.system_prompt = overrides.agent.prompt.prompt

        if overrides.tts:
            if overrides.tts.agent_output_audio_format:
                self.state.output_audio_format = overrides.tts.agent_output_audio_format

        if overrides.asr:
            if overrides.asr.user_input_audio_format:
                self.state.input_audio_format = overrides.asr.user_input_audio_format

        if overrides.turn:
            if overrides.turn.mode:
                self.state.turn_mode = overrides.turn.mode
            if overrides.turn.turn_eagerness:
                self.state.turn_eagerness = overrides.turn.turn_eagerness

        if overrides.conversation:
            if overrides.conversation.max_duration_seconds:
                self.state.max_duration_seconds = overrides.conversation.max_duration_seconds
            if overrides.conversation.client_events:
                self.state.client_events = overrides.conversation.client_events

    def _resolve_variables(self, text: str) -> str:
        """Replace {{variable}} placeholders with dynamic variable values."""
        if not self.state or not text:
            return text
        for key, value in self.state.dynamic_variables.items():
            text = text.replace(f"{{{{{key}}}}}", value)
        return text

    @staticmethod
    def _is_sentence_boundary(text: str) -> bool:
        """Detect natural sentence boundaries for TTS chunking.

        ElevenLabs chunks on sentence boundaries to balance latency vs quality.
        Short sentences = lower latency. Full sentences = better prosody.
        """
        text = text.rstrip()
        if not text:
            return False
        # Sentence-ending punctuation
        if text[-1] in ".!?;:":
            return True
        # Comma + minimum length (natural pause point)
        if text[-1] == "," and len(text) > 40:
            return True
        # Very long buffer — force chunk
        if len(text) > 150:
            return True
        return False

    async def _send_event(self, event_type: ClientEventType, payload: dict[str, Any]) -> None:
        """Send a typed event to the client if subscribed."""
        # Check if client subscribed to this event type
        if event_type.value not in self.state.client_events and event_type not in (
            ClientEventType.CONVERSATION_INITIATION_METADATA,
            ClientEventType.PING,
            ClientEventType.CLIENT_ERROR,
        ):
            return

        msg = build_server_message(event_type, **payload)
        await self.send_message(msg)
