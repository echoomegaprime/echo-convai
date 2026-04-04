"""ElevenLabs Conversational AI WebSocket Protocol — Complete Reverse Engineering.

Reconstructed from OpenAPI spec (220 endpoints, 1039 schemas), SDK source analysis,
and live endpoint probing. This module defines every message type, enum, and schema
in the ElevenLabs ConvAI WebSocket protocol (AsyncAPI 2.6.0 compatible).

Target: Build a drop-in compatible WebSocket server that speaks the same protocol
as ElevenLabs' Conversational AI, allowing any ElevenLabs SDK to connect to our server.
"""

from __future__ import annotations

import enum
import hashlib
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS — Reverse-engineered from OpenAPI spec + SDK source
# ═══════════════════════════════════════════════════════════════════════════════

class ClientEventType(str, enum.Enum):
    """Events the server sends to the client. Captured from SDK ClientEvent enum."""
    CONVERSATION_INITIATION_METADATA = "conversation_initiation_metadata"
    ASR_INITIATION_METADATA = "asr_initiation_metadata"
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
    MCP_CONNECTION_STATUS = "mcp_connection_status"
    AGENT_TOOL_REQUEST = "agent_tool_request"
    AGENT_TOOL_RESPONSE = "agent_tool_response"
    PING = "ping"
    VAD_SCORE = "vad_score"
    CLIENT_ERROR = "client_error"
    GUARDRAIL_TRIGGERED = "guardrail_triggered"
    DTMF_REQUEST = "dtmf_request"
    INTERNAL_TURN_PROBABILITY = "internal_turn_probability"
    INTERNAL_TENTATIVE_AGENT_RESPONSE = "internal_tentative_agent_response"


class ServerMessageType(str, enum.Enum):
    """Messages the client sends to the server."""
    CONVERSATION_INITIATION = "conversation_initiation_client_data"
    USER_MESSAGE = "user_message"
    USER_ACTIVITY = "user_activity"
    CONTEXTUAL_UPDATE = "contextual_update"
    PONG = "pong"
    CLIENT_TOOL_RESULT = "client_tool_result"
    MCP_TOOL_APPROVAL = "mcp_tool_approval_result"
    FEEDBACK = "feedback"
    MULTIMODAL_MESSAGE = "multimodal_message"
    # Audio chunks have NO type field — identified by presence of user_audio_chunk


class AudioFormat(str, enum.Enum):
    """Supported audio formats for ConvAI I/O."""
    PCM_8000 = "pcm_8000"
    PCM_16000 = "pcm_16000"
    PCM_22050 = "pcm_22050"
    PCM_24000 = "pcm_24000"
    PCM_32000 = "pcm_32000"
    PCM_44100 = "pcm_44100"
    PCM_48000 = "pcm_48000"
    ULAW_8000 = "ulaw_8000"


class TurnMode(str, enum.Enum):
    """Turn-taking modes."""
    TURN = "turn"
    SILENCE = "silence"


class TurnEagerness(str, enum.Enum):
    """How quickly the agent responds after detecting end-of-turn."""
    PATIENT = "patient"
    NORMAL = "normal"
    EAGER = "eager"


class TurnModel(str, enum.Enum):
    """Turn detection model version."""
    V2 = "turn_v2"
    V3 = "turn_v3"


class SpellingPatience(str, enum.Enum):
    """Wait behavior when user is spelling."""
    AUTO = "auto"
    OFF = "off"


class ASRProvider(str, enum.Enum):
    """ASR backend provider."""
    ELEVENLABS = "elevenlabs"
    SCRIBE_REALTIME = "scribe_realtime"


class ASRQuality(str, enum.Enum):
    """ASR quality level."""
    HIGH = "high"
    LOW = "low"


class TTSModel(str, enum.Enum):
    """TTS models available for ConvAI."""
    TURBO_V2 = "eleven_turbo_v2"
    TURBO_V2_5 = "eleven_turbo_v2_5"
    FLASH_V2 = "eleven_flash_v2"
    FLASH_V2_5 = "eleven_flash_v2_5"
    MULTILINGUAL_V2 = "eleven_multilingual_v2"
    V3_CONVERSATIONAL = "eleven_v3_conversational"


class LLMModel(str, enum.Enum):
    """Supported LLM models (77 total in ElevenLabs, key ones listed)."""
    GPT_4O = "gpt-4o"
    GPT_4O_MINI = "gpt-4o-mini"
    GPT_4_1 = "gpt-4.1"
    GPT_4_1_MINI = "gpt-4.1-mini"
    GPT_4_1_NANO = "gpt-4.1-nano"
    GEMINI_2_5_FLASH = "gemini-2.5-flash"
    GEMINI_2_5_FLASH_LITE = "gemini-2.5-flash-lite"
    CLAUDE_SONNET_4 = "claude-sonnet-4"
    CLAUDE_SONNET_4_5 = "claude-sonnet-4-5"
    CLAUDE_SONNET_4_6 = "claude-sonnet-4-6"
    CLAUDE_HAIKU_4_5 = "claude-haiku-4-5"
    GROK_BETA = "grok-beta"
    CUSTOM_LLM = "custom-llm"


class ToolType(str, enum.Enum):
    """Agent tool types."""
    SYSTEM = "system"
    WEBHOOK = "webhook"
    CLIENT = "client"
    MCP = "mcp"
    WORKFLOW = "workflow"
    API_INTEGRATION_WEBHOOK = "api_integration_webhook"
    API_INTEGRATION_MCP = "api_integration_mcp"
    SMB = "smb"


class SystemToolName(str, enum.Enum):
    """Built-in system tools."""
    END_CALL = "end_call"
    TRANSFER_TO_NUMBER = "transfer_to_number"
    TRANSFER_TO_AGENT = "transfer_to_agent"
    SKIP_TURN = "skip_turn"
    VOICEMAIL_DETECTION = "voicemail_detection"
    MEMORY_ENTRY_SEARCH = "memory_entry_search"
    MEMORY_ENTRY_CREATE = "memory_entry_create"
    MEMORY_ENTRY_UPDATE = "memory_entry_update"


class ConversationSource(str, enum.Enum):
    """How the conversation was initiated."""
    ANDROID_SDK = "android_sdk"
    NODE_JS_SDK = "node_js_sdk"
    REACT_NATIVE_SDK = "react_native_sdk"
    REACT_SDK = "react_sdk"
    JS_SDK = "js_sdk"
    PYTHON_SDK = "python_sdk"
    WIDGET = "widget"
    SIP_TRUNK = "sip_trunk"
    TWILIO = "twilio"
    GENESYS = "genesys"
    SWIFT_SDK = "swift_sdk"
    WHATSAPP = "whatsapp"
    FLUTTER_SDK = "flutter_sdk"
    ZENDESK = "zendesk_integration"
    SLACK = "slack_integration"


class AuthMode(str, enum.Enum):
    """Agent authentication modes."""
    PUBLIC = "public"
    AUTHORIZATION_HEADER = "authorization_header"
    SIGNED_URL = "signed_url"
    SHAREABLE_LINK = "shareable_link"
    LIVEKIT_TOKEN = "livekit_token"
    GENESYS_API_KEY = "genesys_api_key"
    WHATSAPP = "whatsapp"


class WebSocketCloseCode(int, enum.Enum):
    """WebSocket close codes used by ElevenLabs."""
    NORMAL = 1000           # Agent ended, max duration reached
    PROTOCOL_ERROR = 1002   # Timeout, safety violation, LLM/TTS/ASR failure
    POLICY_VIOLATION = 1008 # Invalid message, auth error, validation failure
    INTERNAL_ERROR = 1011   # Server error


class FeedbackType(str, enum.Enum):
    """Conversation feedback types."""
    LIKE = "like"
    DISLIKE = "dislike"


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENT → SERVER MESSAGE SCHEMAS
# ═══════════════════════════════════════════════════════════════════════════════

class VoiceSettings(BaseModel):
    """TTS voice configuration."""
    stability: float = Field(default=0.5, ge=0.0, le=1.0)
    similarity_boost: float = Field(default=0.75, ge=0.0, le=1.0)
    style: float = Field(default=0.0, ge=0.0, le=1.0)
    speed: float = Field(default=1.0, ge=0.7, le=5.0)
    use_speaker_boost: bool = True


class ASRConfig(BaseModel):
    """ASR configuration for conversation."""
    quality: ASRQuality = ASRQuality.HIGH
    provider: ASRProvider = ASRProvider.ELEVENLABS
    user_input_audio_format: AudioFormat = AudioFormat.PCM_16000
    keywords: list[dict[str, str]] = Field(default_factory=list)


class TTSConfig(BaseModel):
    """TTS configuration for conversation."""
    voice_id: str | None = None
    model_id: TTSModel = TTSModel.TURBO_V2_5
    stability: float = Field(default=0.5, ge=0.0, le=1.0)
    similarity_boost: float = Field(default=0.75, ge=0.0, le=1.0)
    speed: float = Field(default=1.0, ge=0.7, le=1.2)
    expressive_mode: bool = False
    agent_output_audio_format: AudioFormat = AudioFormat.PCM_16000
    optimize_streaming_latency: int = Field(default=3, ge=0, le=4)
    suggested_audio_tags: list[dict[str, str]] = Field(default_factory=list)


class TurnConfig(BaseModel):
    """Turn-taking configuration."""
    mode: TurnMode = TurnMode.TURN
    turn_timeout: float = Field(default=15.0, ge=1.0, le=60.0)
    silence_end_call_timeout: float = Field(default=30.0, ge=5.0, le=300.0)
    turn_eagerness: TurnEagerness = TurnEagerness.NORMAL
    spelling_patience: SpellingPatience = SpellingPatience.AUTO
    speculative_turn: bool = False
    turn_model: TurnModel = TurnModel.V2
    soft_timeout_config: dict[str, Any] | None = None


class RAGConfig(BaseModel):
    """RAG retrieval configuration."""
    enabled: bool = True
    embedding_model: str = "e5_mistral_7b_instruct"
    max_vector_distance: float = Field(default=0.7, ge=0.0, le=1.0)


class BackupLLMConfig(BaseModel):
    """Backup LLM cascade configuration."""
    preference: str = "default"


class PromptConfig(BaseModel):
    """LLM prompt configuration."""
    prompt: str = ""
    llm: str = "gemini-2.5-flash"
    temperature: float = Field(default=0.85, ge=0.0, le=2.0)
    max_tokens: int = Field(default=400, ge=1, le=8192)
    reasoning_effort: str | None = None
    thinking_budget: int | None = None
    rag: RAGConfig = Field(default_factory=RAGConfig)
    backup_llm_config: BackupLLMConfig = Field(default_factory=BackupLLMConfig)
    cascade_timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)
    enable_parallel_tool_calls: bool = True
    built_in_tools: dict[str, Any] = Field(default_factory=dict)


class VADConfig(BaseModel):
    """Voice activity detection configuration."""
    background_voice_detection: bool = True


class ConversationSettings(BaseModel):
    """Conversation-level settings."""
    text_only: bool = False
    max_duration_seconds: int = Field(default=1800, ge=60, le=7200)
    client_events: list[str] = Field(default_factory=lambda: [
        "audio", "interruption", "agent_response", "user_transcript",
        "agent_response_correction", "agent_tool_response",
    ])
    file_input: dict[str, Any] | None = None
    monitoring_enabled: bool = False


class AgentConfig(BaseModel):
    """Agent-level configuration."""
    first_message: str | None = None
    language: str = "en"
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    dynamic_variables: dict[str, str] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    native_mcp_server_ids: list[str] = Field(default_factory=list)


class ConversationConfigOverride(BaseModel):
    """Config overrides sent during conversation initiation."""
    agent: AgentConfig | None = None
    asr: ASRConfig | None = None
    tts: TTSConfig | None = None
    turn: TurnConfig | None = None
    conversation: ConversationSettings | None = None
    vad: VADConfig | None = None


class ConversationInitiation(BaseModel):
    """Client→Server: Session setup message."""
    type: Literal["conversation_initiation_client_data"] = "conversation_initiation_client_data"
    conversation_config_override: ConversationConfigOverride | None = None
    custom_llm_extra_body: dict[str, Any] = Field(default_factory=dict)
    dynamic_variables: dict[str, str] = Field(default_factory=dict)
    user_id: str | None = None
    source_info: dict[str, Any] = Field(default_factory=dict)


class UserAudioChunk(BaseModel):
    """Client→Server: Raw audio data. NOTE: No 'type' field — identified by user_audio_chunk key."""
    user_audio_chunk: str  # Base64-encoded PCM


class UserMessage(BaseModel):
    """Client→Server: Text input from user."""
    type: Literal["user_message"] = "user_message"
    text: str


class UserActivity(BaseModel):
    """Client→Server: Activity indicator (pauses agent ~2s)."""
    type: Literal["user_activity"] = "user_activity"


class ContextualUpdate(BaseModel):
    """Client→Server: Non-interrupting context injection."""
    type: Literal["contextual_update"] = "contextual_update"
    text: str


class PongMessage(BaseModel):
    """Client→Server: Latency measurement response."""
    type: Literal["pong"] = "pong"
    event_id: int


class ClientToolResult(BaseModel):
    """Client→Server: Result of client-side tool execution."""
    type: Literal["client_tool_result"] = "client_tool_result"
    tool_call_id: str
    result: str
    is_error: bool = False


class McpToolApprovalResult(BaseModel):
    """Client→Server: MCP tool approval decision."""
    type: Literal["mcp_tool_approval_result"] = "mcp_tool_approval_result"
    tool_call_id: str
    approved: bool


class FeedbackMessage(BaseModel):
    """Client→Server: Conversation feedback."""
    type: Literal["feedback"] = "feedback"
    feedback: FeedbackType
    rating: int | None = None


class MultimodalMessage(BaseModel):
    """Client→Server: Combined text + file input."""
    type: Literal["multimodal_message"] = "multimodal_message"
    text: dict[str, str] | None = None  # {"type": "user_message", "text": "..."}
    file: dict[str, str] | None = None  # {"type": "file_input", "file_id": "..."}


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER → CLIENT MESSAGE SCHEMAS
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationInitiationMetadata(BaseModel):
    """Server→Client: Session negotiation response."""
    type: Literal["conversation_initiation_metadata"] = "conversation_initiation_metadata"
    conversation_initiation_metadata_event: dict[str, Any] = Field(default_factory=dict)
    # Contains: conversation_id, agent_output_audio_format, user_input_audio_format


class ASRInitiationMetadata(BaseModel):
    """Server→Client: ASR configuration metadata."""
    type: Literal["asr_initiation_metadata"] = "asr_initiation_metadata"
    asr_initiation_metadata_event: dict[str, Any] = Field(default_factory=dict)


class AudioEvent(BaseModel):
    """Server→Client: Agent audio response chunk."""
    type: Literal["audio"] = "audio"
    audio_event: dict[str, Any]
    # audio_event.audio_base_64: str — Base64 PCM
    # audio_event.event_id: int
    # audio_event.alignment: optional char-level timing


class InterruptionEvent(BaseModel):
    """Server→Client: User interrupted the agent."""
    type: Literal["interruption"] = "interruption"
    interruption_event: dict[str, Any] = Field(default_factory=dict)
    # interruption_event.event_id: int


class UserTranscript(BaseModel):
    """Server→Client: Final ASR transcription."""
    type: Literal["user_transcript"] = "user_transcript"
    user_transcription_event: dict[str, str]
    # user_transcription_event.user_transcript: str


class TentativeUserTranscript(BaseModel):
    """Server→Client: Interim ASR result."""
    type: Literal["tentative_user_transcript"] = "tentative_user_transcript"
    user_transcription_event: dict[str, str]


class AgentResponse(BaseModel):
    """Server→Client: Complete LLM-generated text."""
    type: Literal["agent_response"] = "agent_response"
    agent_response_event: dict[str, str]
    # agent_response_event.agent_response: str


class AgentResponseCorrection(BaseModel):
    """Server→Client: Corrected response (safety filter applied)."""
    type: Literal["agent_response_correction"] = "agent_response_correction"
    agent_response_correction_event: dict[str, str]
    # original_agent_response, corrected_agent_response


class AgentResponseMetadata(BaseModel):
    """Server→Client: Response metadata."""
    type: Literal["agent_response_metadata"] = "agent_response_metadata"
    agent_response_metadata_event: dict[str, Any] = Field(default_factory=dict)


class AgentChatResponsePart(BaseModel):
    """Server→Client: Streaming text chunk (start/delta/stop)."""
    type: Literal["agent_chat_response_part"] = "agent_chat_response_part"
    agent_chat_response_part_event: dict[str, Any]
    # role: "start" | "delta" | "stop"
    # content: str (for delta)


class ClientToolCall(BaseModel):
    """Server→Client: Invoke registered client function."""
    type: Literal["client_tool_call"] = "client_tool_call"
    client_tool_call: dict[str, Any]
    # tool_name: str, tool_call_id: str, parameters: dict


class McpToolCall(BaseModel):
    """Server→Client: MCP tool invocation requiring approval."""
    type: Literal["mcp_tool_call"] = "mcp_tool_call"
    mcp_tool_call_event: dict[str, Any]


class McpConnectionStatus(BaseModel):
    """Server→Client: MCP service status change."""
    type: Literal["mcp_connection_status"] = "mcp_connection_status"
    mcp_connection_status_event: dict[str, Any]


class AgentToolRequest(BaseModel):
    """Server→Client: Tool execution request lifecycle."""
    type: Literal["agent_tool_request"] = "agent_tool_request"
    agent_tool_request_event: dict[str, Any]


class AgentToolResponse(BaseModel):
    """Server→Client: Tool execution result."""
    type: Literal["agent_tool_response"] = "agent_tool_response"
    agent_tool_response_event: dict[str, Any]


class PingMessage(BaseModel):
    """Server→Client: Keepalive ping."""
    type: Literal["ping"] = "ping"
    ping_event: dict[str, Any]
    # event_id: int, ping_ms: int (optional delay before pong)


class VADScoreEvent(BaseModel):
    """Server→Client: Voice activity detection score."""
    type: Literal["vad_score"] = "vad_score"
    vad_score_event: dict[str, float]
    # score: 0.0-1.0


class ClientError(BaseModel):
    """Server→Client: Protocol error."""
    type: Literal["client_error"] = "client_error"
    error_event: dict[str, Any]
    # message: str, code: str


class GuardrailTriggered(BaseModel):
    """Server→Client: Safety guardrail activation."""
    type: Literal["guardrail_triggered"] = "guardrail_triggered"
    guardrail_event: dict[str, Any]


class DTMFRequest(BaseModel):
    """Server→Client: DTMF tone request (telephony)."""
    type: Literal["dtmf_request"] = "dtmf_request"
    dtmf_event: dict[str, str]


class InternalTurnProbability(BaseModel):
    """Server→Client: Turn detection probability."""
    type: Literal["internal_turn_probability"] = "internal_turn_probability"
    turn_probability_event: dict[str, float]
    # probability: 0.0-1.0


class InternalTentativeAgentResponse(BaseModel):
    """Server→Client: Pre-final agent response (speculative generation)."""
    type: Literal["internal_tentative_agent_response"] = "internal_tentative_agent_response"
    tentative_response_event: dict[str, str]


# ═══════════════════════════════════════════════════════════════════════════════
# FULL AGENT CONFIGURATION SCHEMA
# ═══════════════════════════════════════════════════════════════════════════════

class WebhookToolSchema(BaseModel):
    """Server/webhook tool definition."""
    type: Literal["webhook"] = "webhook"
    name: str
    description: str
    api_schema: dict[str, Any]  # url, method, headers, request_body, response_body
    execution_mode: Literal["immediate", "post_tool_speech", "async"] = "immediate"
    force_pre_tool_speech: bool = False
    tool_call_sound: str | None = None
    assignments: list[dict[str, str]] = Field(default_factory=list)
    # assignments: [{"variable": "var_name", "json_path": "$.result"}]


class ClientToolSchema(BaseModel):
    """Client-side tool definition."""
    type: Literal["client"] = "client"
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class SystemToolSchema(BaseModel):
    """Built-in system tool."""
    type: Literal["system"] = "system"
    name: SystemToolName
    params: dict[str, Any] = Field(default_factory=dict)


class TransferConfig(BaseModel):
    """Phone transfer configuration."""
    phone_number: str
    condition: str = ""
    transfer_type: Literal["blind", "conference", "sip_refer"] = "blind"


class SoftTimeoutConfig(BaseModel):
    """Soft timeout — agent prompts user when silence detected."""
    timeout_seconds: float = 8.0
    message: str = ""
    use_llm_generated_message: bool = True


class FullAgentDefinition(BaseModel):
    """Complete agent configuration — matches ElevenLabs' create/update schema."""
    name: str
    conversation_config: dict[str, Any] = Field(default_factory=dict)
    # Nested: agent, asr, tts, turn, conversation, vad
    platform_settings: dict[str, Any] = Field(default_factory=dict)
    # Nested: widget, auth, privacy
    metadata: dict[str, Any] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# PROTOCOL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def generate_conversation_id() -> str:
    """Generate a unique conversation ID matching ElevenLabs format."""
    return f"conv_{uuid.uuid4().hex[:24]}"


def generate_event_id() -> int:
    """Generate a monotonic event ID."""
    return int(time.time() * 1000) % (2**31)


def generate_signed_url(agent_id: str, secret: str, ttl_seconds: int = 900) -> str:
    """Generate a signed conversation URL (for private agents)."""
    expires = int(time.time()) + ttl_seconds
    conv_id = generate_conversation_id()
    payload = f"{agent_id}:{conv_id}:{expires}"
    signature = hashlib.sha256(f"{payload}:{secret}".encode()).hexdigest()[:32]
    token = f"cvtkn_{signature}_{expires}"
    return f"wss://echo-convai.bmcii1976.workers.dev/v1/convai/conversation?agent_id={agent_id}&conversation_signature={token}"


def parse_client_message(raw: dict[str, Any]) -> BaseModel:
    """Parse an incoming WebSocket message from client to typed model."""
    if "user_audio_chunk" in raw:
        return UserAudioChunk(**raw)

    msg_type = raw.get("type")
    parsers = {
        "conversation_initiation_client_data": ConversationInitiation,
        "user_message": UserMessage,
        "user_activity": UserActivity,
        "contextual_update": ContextualUpdate,
        "pong": PongMessage,
        "client_tool_result": ClientToolResult,
        "mcp_tool_approval_result": McpToolApprovalResult,
        "feedback": FeedbackMessage,
        "multimodal_message": MultimodalMessage,
    }
    parser = parsers.get(msg_type)
    if parser is None:
        raise ValueError(f"Unknown client message type: {msg_type}")
    return parser(**raw)


def build_server_message(event_type: ClientEventType, **kwargs: Any) -> dict[str, Any]:
    """Build a server→client message dict."""
    msg: dict[str, Any] = {"type": event_type.value}
    msg.update(kwargs)
    return msg
