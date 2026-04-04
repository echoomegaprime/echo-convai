"""Echo ConvAI SDK — Pydantic models for all API and WebSocket message types.

All models use Pydantic v2 and match the ElevenLabs-compatible wire protocol
for full interop with existing ElevenLabs SDKs and our custom Echo ConvAI server.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════════════════════


class AudioFormat(str, enum.Enum):
    """Supported PCM audio formats (sample rate encoded in name)."""

    PCM_8000 = "pcm_8000"
    PCM_16000 = "pcm_16000"
    PCM_22050 = "pcm_22050"
    PCM_24000 = "pcm_24000"
    PCM_32000 = "pcm_32000"
    PCM_44100 = "pcm_44100"
    PCM_48000 = "pcm_48000"
    ULAW_8000 = "ulaw_8000"

    @property
    def sample_rate(self) -> int:
        """Extract integer sample rate from the format name."""
        return int(self.value.split("_")[1])


class TurnMode(str, enum.Enum):
    """Turn-taking detection mode."""

    TURN = "turn"
    SILENCE = "silence"


class TurnEagerness(str, enum.Enum):
    """How quickly the agent responds after end-of-turn detection."""

    PATIENT = "patient"
    NORMAL = "normal"
    EAGER = "eager"


class ConversationState(str, enum.Enum):
    """Client-side conversation state machine states."""

    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    DISCONNECTED = "disconnected"


class FeedbackType(str, enum.Enum):
    """Conversation quality feedback."""

    LIKE = "like"
    DISLIKE = "dislike"


class AgentChatResponsePartType(str, enum.Enum):
    """Streaming text response chunk type."""

    START = "start"
    DELTA = "delta"
    STOP = "stop"


class AuthMode(str, enum.Enum):
    """Agent authentication mode."""

    PUBLIC = "public"
    SIGNED_URL = "signed_url"
    API_KEY = "api_key"


class ToolType(str, enum.Enum):
    """Agent tool invocation type."""

    SYSTEM = "system"
    WEBHOOK = "webhook"
    CLIENT = "client"
    MCP = "mcp"


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER → CLIENT EVENT TYPES (what the server sends)
# ═══════════════════════════════════════════════════════════════════════════════


class ServerEventType(str, enum.Enum):
    """All event types the server can send to the client."""

    CONVERSATION_INITIATION_METADATA = "conversation_initiation_metadata"
    AUDIO = "audio"
    INTERRUPTION = "interruption"
    USER_TRANSCRIPT = "user_transcript"
    TENTATIVE_USER_TRANSCRIPT = "tentative_user_transcript"
    AGENT_RESPONSE = "agent_response"
    AGENT_RESPONSE_CORRECTION = "agent_response_correction"
    AGENT_RESPONSE_METADATA = "agent_response_metadata"
    AGENT_CHAT_RESPONSE_PART = "agent_chat_response_part"
    CLIENT_TOOL_CALL = "client_tool_call"
    MCP_TOOL_CALL = "mcp_tool_call"
    AGENT_TOOL_REQUEST = "agent_tool_request"
    AGENT_TOOL_RESPONSE = "agent_tool_response"
    PING = "ping"
    VAD_SCORE = "vad_score"
    CLIENT_ERROR = "client_error"
    GUARDRAIL_TRIGGERED = "guardrail_triggered"
    INTERNAL_TURN_PROBABILITY = "internal_turn_probability"


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENT → SERVER MESSAGE MODELS
# ═══════════════════════════════════════════════════════════════════════════════


class ASRConfig(BaseModel):
    """Automatic Speech Recognition configuration."""

    quality: str = "high"
    provider: str = "elevenlabs"
    user_input_audio_format: AudioFormat = AudioFormat.PCM_16000
    keywords: list[dict[str, str]] = Field(default_factory=list)


class TTSConfig(BaseModel):
    """Text-to-Speech output configuration."""

    voice_id: str | None = None
    model_id: str = "eleven_turbo_v2_5"
    stability: float = Field(default=0.5, ge=0.0, le=1.0)
    similarity_boost: float = Field(default=0.75, ge=0.0, le=1.0)
    speed: float = Field(default=1.0, ge=0.7, le=5.0)
    agent_output_audio_format: AudioFormat = AudioFormat.PCM_16000
    optimize_streaming_latency: int = Field(default=3, ge=0, le=4)


class TurnConfig(BaseModel):
    """Turn-taking detection configuration."""

    mode: TurnMode = TurnMode.TURN
    turn_timeout: float = Field(default=15.0, ge=1.0, le=60.0)
    silence_end_call_timeout: float = Field(default=30.0, ge=5.0, le=300.0)
    turn_eagerness: TurnEagerness = TurnEagerness.NORMAL


class ConversationSettings(BaseModel):
    """Conversation-level settings for a session."""

    text_only: bool = False
    max_duration_seconds: int = Field(default=1800, ge=60, le=7200)
    client_events: list[str] = Field(
        default_factory=lambda: [
            "audio",
            "interruption",
            "agent_response",
            "user_transcript",
            "agent_response_correction",
            "agent_tool_response",
        ]
    )


class PromptConfig(BaseModel):
    """LLM prompt and model configuration."""

    prompt: str = ""
    llm: str = "gemini-2.5-flash"
    temperature: float = Field(default=0.85, ge=0.0, le=2.0)
    max_tokens: int = Field(default=400, ge=1, le=8192)


class AgentConfig(BaseModel):
    """Agent-level configuration for conversation initiation."""

    first_message: str | None = None
    language: str = "en"
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    dynamic_variables: dict[str, str] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)


class ConversationConfigOverride(BaseModel):
    """Config overrides sent during WebSocket initiation."""

    agent: AgentConfig | None = None
    asr: ASRConfig | None = None
    tts: TTSConfig | None = None
    turn: TurnConfig | None = None
    conversation: ConversationSettings | None = None
    text_only: bool | None = None


class ConversationInitiation(BaseModel):
    """Client -> Server: Session setup sent immediately after WS connect."""

    type: Literal["conversation_initiation_client_data"] = "conversation_initiation_client_data"
    conversation_config_override: ConversationConfigOverride | None = None
    custom_llm_extra_body: dict[str, Any] = Field(default_factory=dict)
    dynamic_variables: dict[str, str] = Field(default_factory=dict)
    user_id: str | None = None
    source_info: dict[str, Any] = Field(
        default_factory=lambda: {"source": "echo_convai_python_sdk", "version": "0.1.0"}
    )


class UserAudioChunk(BaseModel):
    """Client -> Server: Base64-encoded PCM audio (no type field in wire format)."""

    user_audio_chunk: str


class UserMessage(BaseModel):
    """Client -> Server: Text message to the agent."""

    type: Literal["user_message"] = "user_message"
    text: str


class UserActivity(BaseModel):
    """Client -> Server: Keep-alive activity signal (pauses agent ~2s)."""

    type: Literal["user_activity"] = "user_activity"


class ContextualUpdate(BaseModel):
    """Client -> Server: Non-interrupting context injection."""

    type: Literal["contextual_update"] = "contextual_update"
    text: str


class PongMessage(BaseModel):
    """Client -> Server: Latency measurement response to server ping."""

    type: Literal["pong"] = "pong"
    event_id: int


class ClientToolResult(BaseModel):
    """Client -> Server: Result of a client-side tool execution."""

    type: Literal["client_tool_result"] = "client_tool_result"
    tool_call_id: str
    result: str
    is_error: bool = False


class FeedbackMessage(BaseModel):
    """Client -> Server: Conversation quality feedback."""

    type: Literal["feedback"] = "feedback"
    feedback: FeedbackType
    rating: int | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# REST API MODELS — Agent CRUD
# ═══════════════════════════════════════════════════════════════════════════════


class CreateAgentRequest(BaseModel):
    """REST API: Create a new agent."""

    name: str
    conversation_config: dict[str, Any] = Field(default_factory=dict)
    platform_settings: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class UpdateAgentRequest(BaseModel):
    """REST API: Partial update to an existing agent."""

    name: str | None = None
    conversation_config: dict[str, Any] | None = None
    platform_settings: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    tags: list[str] | None = None


class AgentSummary(BaseModel):
    """REST API: Agent summary returned from list endpoints."""

    agent_id: str
    name: str
    created_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class AgentDetail(BaseModel):
    """REST API: Full agent detail returned from get endpoint."""

    agent_id: str
    name: str
    conversation_config: dict[str, Any] = Field(default_factory=dict)
    platform_settings: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class SignedUrlResponse(BaseModel):
    """REST API: Signed WebSocket URL for authenticated conversation."""

    signed_url: str


class ConversationHistoryItem(BaseModel):
    """REST API: Summary of a past conversation."""

    conversation_id: str
    agent_id: str
    status: str = ""
    start_time: str | None = None
    end_time: str | None = None
    duration_seconds: float | None = None
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    analysis: dict[str, Any] = Field(default_factory=dict)


class ConversationListResponse(BaseModel):
    """REST API: Paginated list of conversations."""

    conversations: list[ConversationHistoryItem] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None


class AgentListResponse(BaseModel):
    """REST API: Paginated list of agents."""

    agents: list[AgentSummary] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER → CLIENT EVENT MODELS (parsed from WebSocket JSON)
# ═══════════════════════════════════════════════════════════════════════════════


class AudioEventAlignment(BaseModel):
    """Character-level timing alignment for TTS audio."""

    chars: list[str] = Field(default_factory=list)
    char_start_times_ms: list[int] = Field(default_factory=list)
    char_durations_ms: list[int] = Field(default_factory=list)


class AudioEventData(BaseModel):
    """Parsed audio event payload."""

    audio_base_64: str
    event_id: int | str = 0
    alignment: AudioEventAlignment | None = None


class TranscriptEventData(BaseModel):
    """Parsed transcript event payload."""

    user_transcript: str


class AgentResponseEventData(BaseModel):
    """Parsed agent response event payload."""

    agent_response: str


class AgentResponseCorrectionData(BaseModel):
    """Parsed agent response correction payload."""

    original_agent_response: str
    corrected_agent_response: str


class PingEventData(BaseModel):
    """Parsed ping event payload."""

    event_id: int
    ping_ms: int | None = None


class ClientToolCallData(BaseModel):
    """Parsed client tool call payload."""

    tool_call_id: str
    tool_name: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class InterruptionEventData(BaseModel):
    """Parsed interruption event payload."""

    event_id: int | str = 0
