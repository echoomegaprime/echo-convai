// ============================================================================
// Echo ConvAI Gateway — Full TypeScript Types
// ElevenLabs-compatible WebSocket conversation protocol
// ============================================================================

// ---------------------------------------------------------------------------
// Environment bindings
// ---------------------------------------------------------------------------

export interface Env {
  DB: D1Database;
  CONVERSATION_SESSION: DurableObjectNamespace;
  BRAVO_TUNNEL_URL: string;
  ELEVENLABS_API_KEY?: string;
  CONVAI_SIGNING_SECRET?: string;
}

// ---------------------------------------------------------------------------
// D1 Agent Config
// ---------------------------------------------------------------------------

export interface AgentConfig {
  agent_id: string;
  name: string;
  prompt: string;
  first_message: string | null;
  language: string;
  voice_id: string;
  model_id: string;
  max_duration_seconds: number;
  requires_auth: boolean;
  tts_speed: number;
  tts_stability: number;
  tts_similarity_boost: number;
  tools: string | null; // JSON array of tool definitions
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Audio formats
// ---------------------------------------------------------------------------

export type AudioFormatString =
  | "pcm_8000"
  | "pcm_16000"
  | "pcm_22050"
  | "pcm_24000"
  | "pcm_44100"
  | "pcm_48000"
  | "ulaw_8000";

export interface AudioFormat {
  encoding: "pcm" | "ulaw";
  sample_rate: number;
}

export function parseAudioFormat(format: AudioFormatString): AudioFormat {
  const parts = format.split("_");
  return {
    encoding: parts[0] as "pcm" | "ulaw",
    sample_rate: parseInt(parts[1], 10),
  };
}

// ---------------------------------------------------------------------------
// Client -> Server message types
// ---------------------------------------------------------------------------

/** Must be the first message sent after WebSocket open */
export interface ConversationInitiationClientData {
  type: "conversation_initiation_client_data";
  custom_llm_extra_body?: Record<string, unknown>;
  conversation_config_override?: ConversationConfigOverride;
  dynamic_variables?: Record<string, string | number | boolean>;
  source_info?: SourceInfo;
  user_id?: string;
  tool_mock_config?: ToolMockConfig;
}

export interface ConversationConfigOverride {
  agent?: {
    prompt?: { prompt?: string };
    first_message?: string;
    language?: string;
  };
  tts?: {
    voice_id?: string;
    speed?: number;
    stability?: number;
    similarity_boost?: number;
  };
  conversation?: {
    text_only?: boolean;
  };
  text_only?: boolean; // Python SDK sends at top level
}

export interface SourceInfo {
  source: string;
  version: string;
}

export interface ToolMockConfig {
  mocking_strategy: "none" | "all" | "selected";
  mocked_tool_names?: string[];
  fallback_strategy?: "raise_error" | "call_real_tool";
}

/** Raw PCM audio chunk, base64 encoded */
export interface UserAudioChunkMessage {
  user_audio_chunk: string; // base64 PCM 16-bit mono
}

/** Pong response to server ping */
export interface PongMessage {
  type: "pong";
  event_id: number;
}

/** User text message (text-only or hybrid mode) */
export interface UserTextMessage {
  type: "user_message";
  text: string;
}

/** Keep-alive user activity signal */
export interface UserActivityMessage {
  type: "user_activity";
}

/** Feedback on agent response */
export interface FeedbackMessage {
  type: "feedback";
  score?: "like" | "dislike";
  event_id?: number;
}

/** Result from client-side tool execution */
export interface ClientToolResultMessage {
  type: "client_tool_result";
  tool_call_id: string;
  result: string;
  is_error: boolean;
}

/** MCP tool approval result */
export interface MCPToolApprovalResultMessage {
  type: "mcp_tool_approval_result";
  tool_call_id: string;
  is_approved: boolean;
}

/** Non-interrupting context injection */
export interface ContextualUpdateMessage {
  type: "contextual_update";
  text: string;
}

/** Multimodal message with optional text and file */
export interface MultimodalMessage {
  type: "multimodal_message";
  text?: { type: "user_message"; text: string };
  file?: { type: "file_input"; file_id: string };
}

/** On-prem enclave setup (sent before initiation) */
export interface EnclaveSetupConfig {
  type: "enclave_setup_config";
  agent_config_dict?: Record<string, unknown>;
  override_agent_config_list?: Record<string, unknown>[];
  tools_config_list?: Record<string, unknown>[];
  post_call_transcription_webhook_url?: string;
  post_call_audio_webhook_url?: string;
  prompt_knowledge_base?: string[];
}

/** Union of all client-to-server messages */
export type ClientMessage =
  | ConversationInitiationClientData
  | UserAudioChunkMessage
  | PongMessage
  | UserTextMessage
  | UserActivityMessage
  | FeedbackMessage
  | ClientToolResultMessage
  | MCPToolApprovalResultMessage
  | ContextualUpdateMessage
  | MultimodalMessage
  | EnclaveSetupConfig;

// ---------------------------------------------------------------------------
// Server -> Client message types
// ---------------------------------------------------------------------------

/** First server message — conversation metadata */
export interface ConversationInitiationMetadata {
  type: "conversation_initiation_metadata";
  conversation_initiation_metadata_event: {
    conversation_id: string;
    agent_output_audio_format?: AudioFormatString;
    user_input_audio_format?: AudioFormatString;
  };
}

/** Audio chunk from agent TTS */
export interface AudioEvent {
  type: "audio";
  audio_event: {
    event_id: string;
    audio_base_64: string;
    alignment?: AudioAlignment;
  };
}

export interface AudioAlignment {
  chars: string[];
  char_start_times_ms: number[];
  char_durations_ms: number[];
}

/** Complete agent text response */
export interface AgentResponseEvent {
  type: "agent_response";
  agent_response_event: {
    agent_response: string;
    event_id?: string;
  };
}

/** Streaming agent text response part */
export interface AgentChatResponsePartEvent {
  type: "agent_chat_response_part";
  text_response_part: {
    type: "start" | "delta" | "stop";
    text?: string;
  };
}

/** Agent response correction (replaces previous) */
export interface AgentResponseCorrectionEvent {
  type: "agent_response_correction";
  agent_response_correction_event: {
    original_agent_response: string;
    corrected_agent_response: string;
  };
}

/** User speech transcription */
export interface UserTranscriptEvent {
  type: "user_transcript";
  user_transcription_event: {
    user_transcript: string;
    event_id?: string;
  };
}

/** Agent was interrupted by user */
export interface InterruptionEvent {
  type: "interruption";
  interruption_event: {
    event_id: string;
  };
}

/** Internal tentative agent response (debug) */
export interface InternalTentativeAgentResponseEvent {
  type: "internal_tentative_agent_response";
  tentative_agent_response_internal_event: {
    tentative_agent_response: string;
  };
}

/** Ping keepalive — client must respond with pong */
export interface PingEvent {
  type: "ping";
  ping_event: {
    event_id: number;
    ping_ms?: number;
  };
}

/** Request client to execute a tool */
export interface ClientToolCallEvent {
  type: "client_tool_call";
  client_tool_call: {
    tool_call_id: string;
    tool_name: string;
    parameters: Record<string, unknown>;
  };
}

/** VAD score from speech detection */
export interface VADScoreEvent {
  type: "vad_score";
  vad_score_event: {
    vad_score: number;
  };
}

/** MCP tool call request */
export interface MCPToolCallEvent {
  type: "mcp_tool_call";
  mcp_tool_call: {
    tool_call_id: string;
    tool_name: string;
    server_name: string;
    parameters: Record<string, unknown>;
  };
}

/** MCP connection status update */
export interface MCPConnectionStatusEvent {
  type: "mcp_connection_status";
  mcp_connection_status: {
    server_name: string;
    status: "connected" | "disconnected" | "error";
    message?: string;
  };
}

/** Server-side tool request (for logging/display) */
export interface AgentToolRequestEvent {
  type: "agent_tool_request";
  agent_tool_request: {
    tool_name: string;
    tool_call_id: string;
    parameters: Record<string, unknown>;
  };
}

/** Server-side tool response (for logging/display) */
export interface AgentToolResponseEvent {
  type: "agent_tool_response";
  agent_tool_response: {
    tool_name: string;
    tool_call_id: string;
    result: string;
    is_error: boolean;
  };
}

/** ASR initiation metadata (speech recognition config) */
export interface ASRInitiationMetadataEvent {
  type: "asr_initiation_metadata";
  asr_initiation_metadata_event: {
    model_id?: string;
    language_code?: string;
  };
}

/** Guardrail was triggered */
export interface GuardrailTriggeredEvent {
  type: "guardrail_triggered";
  guardrail_triggered_event: {
    guardrail_id: string;
    message: string;
  };
}

/** Error from server */
export interface ErrorEvent {
  type: "error";
  error_event: {
    error_type: string;
    message: string;
    reason?: string;
    code?: number;
    debug_message?: string;
    details?: Record<string, unknown>;
  };
}

/** Union of all server-to-client messages */
export type ServerMessage =
  | ConversationInitiationMetadata
  | AudioEvent
  | AgentResponseEvent
  | AgentChatResponsePartEvent
  | AgentResponseCorrectionEvent
  | UserTranscriptEvent
  | InterruptionEvent
  | InternalTentativeAgentResponseEvent
  | PingEvent
  | ClientToolCallEvent
  | VADScoreEvent
  | MCPToolCallEvent
  | MCPConnectionStatusEvent
  | AgentToolRequestEvent
  | AgentToolResponseEvent
  | ASRInitiationMetadataEvent
  | GuardrailTriggeredEvent
  | ErrorEvent;

// ---------------------------------------------------------------------------
// Session metrics
// ---------------------------------------------------------------------------

export interface SessionMetrics {
  conversation_id: string;
  agent_id: string;
  started_at: number;
  ended_at: number | null;
  duration_ms: number;
  audio_chunks_received: number;
  audio_chunks_sent: number;
  text_messages_received: number;
  agent_responses_sent: number;
  interruptions: number;
  tool_calls: number;
  pings_sent: number;
  pongs_received: number;
  errors: number;
  end_reason: string | null;
  user_id: string | null;
  text_only: boolean;
}

// ---------------------------------------------------------------------------
// Internal session state
// ---------------------------------------------------------------------------

export interface SessionState {
  conversation_id: string;
  agent_id: string;
  agent_config: AgentConfig;
  user_id: string | null;
  text_only: boolean;
  started_at: number;
  max_duration_ms: number;
  last_activity_at: number;
  last_ping_at: number;
  ping_event_id: number;
  audio_event_id: number;
  is_initialized: boolean;
  is_ended: boolean;
  dynamic_variables: Record<string, string | number | boolean>;
  custom_llm_extra_body: Record<string, unknown>;
  conversation_config_override: ConversationConfigOverride;
  metrics: SessionMetrics;
}

// ---------------------------------------------------------------------------
// WebSocket close codes
// ---------------------------------------------------------------------------

export const WS_CLOSE_NORMAL = 1000;
export const WS_CLOSE_GOING_AWAY = 1001;
export const WS_CLOSE_PROTOCOL_ERROR = 1002;
export const WS_CLOSE_INVALID_DATA = 1003;
export const WS_CLOSE_POLICY_VIOLATION = 1008;
export const WS_CLOSE_MESSAGE_TOO_BIG = 1009;
export const WS_CLOSE_INTERNAL_ERROR = 1011;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const PING_INTERVAL_MS = 10_000;
export const MAX_WS_MESSAGE_SIZE = 16 * 1024 * 1024; // 16 MB
export const DEFAULT_MAX_DURATION_MS = 10 * 60 * 1000; // 10 minutes
export const DEFAULT_AUDIO_FORMAT: AudioFormatString = "pcm_16000";
export const INACTIVITY_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
export const WS_SUBPROTOCOL = "convai";
