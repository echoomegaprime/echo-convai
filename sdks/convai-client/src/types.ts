// ============================================================================
// @echo-prime/convai-client — TypeScript Types
// ElevenLabs-compatible WebSocket conversation protocol
// ============================================================================

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

export type AudioEncoding = "pcm" | "ulaw";

export interface AudioFormat {
  encoding: AudioEncoding;
  sample_rate: number;
}

export function parseAudioFormat(format: AudioFormatString): AudioFormat {
  const parts = format.split("_");
  return {
    encoding: parts[0] as AudioEncoding,
    sample_rate: parseInt(parts[1], 10),
  };
}

// ---------------------------------------------------------------------------
// Configuration types
// ---------------------------------------------------------------------------

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
  text_only?: boolean;
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

export interface AudioAlignment {
  chars: string[];
  char_start_times_ms: number[];
  char_durations_ms: number[];
}

// ---------------------------------------------------------------------------
// Client -> Server message types
// ---------------------------------------------------------------------------

export interface ConversationInitiationClientData {
  type: "conversation_initiation_client_data";
  custom_llm_extra_body?: Record<string, unknown>;
  conversation_config_override?: ConversationConfigOverride;
  dynamic_variables?: Record<string, string | number | boolean>;
  source_info?: SourceInfo;
  user_id?: string;
  tool_mock_config?: ToolMockConfig;
}

export interface UserAudioChunkMessage {
  user_audio_chunk: string;
}

export interface PongMessage {
  type: "pong";
  event_id: number;
}

export interface UserTextMessage {
  type: "user_message";
  text: string;
}

export interface UserActivityMessage {
  type: "user_activity";
}

export interface FeedbackMessage {
  type: "feedback";
  score?: "like" | "dislike";
  event_id?: number;
}

export interface ClientToolResultMessage {
  type: "client_tool_result";
  tool_call_id: string;
  result: string;
  is_error: boolean;
}

export interface MCPToolApprovalResultMessage {
  type: "mcp_tool_approval_result";
  tool_call_id: string;
  is_approved: boolean;
}

export interface ContextualUpdateMessage {
  type: "contextual_update";
  text: string;
}

export interface MultimodalMessage {
  type: "multimodal_message";
  text?: { type: "user_message"; text: string };
  file?: { type: "file_input"; file_id: string };
}

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
  | MultimodalMessage;

// ---------------------------------------------------------------------------
// Server -> Client message types
// ---------------------------------------------------------------------------

export interface ConversationInitiationMetadata {
  type: "conversation_initiation_metadata";
  conversation_initiation_metadata_event: {
    conversation_id: string;
    agent_output_audio_format?: AudioFormatString;
    user_input_audio_format?: AudioFormatString;
  };
}

export interface AudioEvent {
  type: "audio";
  audio_event: {
    event_id: string;
    audio_base_64: string;
    alignment?: AudioAlignment;
  };
}

export interface AgentResponseEvent {
  type: "agent_response";
  agent_response_event: {
    agent_response: string;
    event_id?: string;
  };
}

export interface AgentChatResponsePartEvent {
  type: "agent_chat_response_part";
  text_response_part: {
    type: "start" | "delta" | "stop";
    text?: string;
  };
}

export interface AgentResponseCorrectionEvent {
  type: "agent_response_correction";
  agent_response_correction_event: {
    original_agent_response: string;
    corrected_agent_response: string;
  };
}

export interface UserTranscriptEvent {
  type: "user_transcript";
  user_transcription_event: {
    user_transcript: string;
    event_id?: string;
  };
}

export interface InterruptionEvent {
  type: "interruption";
  interruption_event: {
    event_id: string;
  };
}

export interface InternalTentativeAgentResponseEvent {
  type: "internal_tentative_agent_response";
  tentative_agent_response_internal_event: {
    tentative_agent_response: string;
  };
}

export interface PingEvent {
  type: "ping";
  ping_event: {
    event_id: number;
    ping_ms?: number;
  };
}

export interface ClientToolCallEvent {
  type: "client_tool_call";
  client_tool_call: {
    tool_call_id: string;
    tool_name: string;
    parameters: Record<string, unknown>;
  };
}

export interface VADScoreEvent {
  type: "vad_score";
  vad_score_event: {
    vad_score: number;
  };
}

export interface MCPToolCallEvent {
  type: "mcp_tool_call";
  mcp_tool_call: {
    tool_call_id: string;
    tool_name: string;
    server_name: string;
    parameters: Record<string, unknown>;
  };
}

export interface MCPConnectionStatusEvent {
  type: "mcp_connection_status";
  mcp_connection_status: {
    server_name: string;
    status: "connected" | "disconnected" | "error";
    message?: string;
  };
}

export interface AgentToolRequestEvent {
  type: "agent_tool_request";
  agent_tool_request: {
    tool_name: string;
    tool_call_id: string;
    parameters: Record<string, unknown>;
  };
}

export interface AgentToolResponseEvent {
  type: "agent_tool_response";
  agent_tool_response: {
    tool_name: string;
    tool_call_id: string;
    result: string;
    is_error: boolean;
  };
}

export interface ASRInitiationMetadataEvent {
  type: "asr_initiation_metadata";
  asr_initiation_metadata_event: {
    model_id?: string;
    language_code?: string;
  };
}

export interface GuardrailTriggeredEvent {
  type: "guardrail_triggered";
  guardrail_triggered_event: {
    guardrail_id: string;
    message: string;
  };
}

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
// SDK Configuration
// ---------------------------------------------------------------------------

export type ConversationStatus = "disconnected" | "connecting" | "connected" | "reconnecting" | "error";
export type ConversationMode = "listening" | "speaking";

export interface ConversationOptions {
  /** Override server URL (default: wss://echo-convai-gateway.bmcii1976.workers.dev) */
  serverUrl?: string;
  /** Override agent prompt */
  overridePrompt?: string;
  /** Override first message */
  overrideFirstMessage?: string;
  /** Override language */
  overrideLanguage?: string;
  /** Override TTS voice ID */
  overrideVoiceId?: string;
  /** TTS speed multiplier */
  ttsSpeed?: number;
  /** TTS stability (0-1) */
  ttsStability?: number;
  /** TTS similarity boost (0-1) */
  ttsSimilarityBoost?: number;
  /** Text-only mode (no audio) */
  textOnly?: boolean;
  /** Custom LLM extra body */
  customLlmExtraBody?: Record<string, unknown>;
  /** Dynamic template variables */
  dynamicVariables?: Record<string, string | number | boolean>;
  /** User ID for tracking */
  userId?: string;
  /** Authorization token for private agents */
  authorization?: string;
  /** Pre-signed WebSocket URL (bypasses agent_id param) */
  signedUrl?: string;
  /** Auto-reconnect on disconnect (default: true) */
  autoReconnect?: boolean;
  /** Max reconnect attempts (default: 5) */
  maxReconnectAttempts?: number;
  /** Base reconnect delay in ms (default: 1000) */
  reconnectBaseDelay?: number;
  /** Tool mock configuration */
  toolMockConfig?: ToolMockConfig;
}

export interface ConnectOptions extends ConversationOptions {
  /** Agent ID to connect to */
  agentId?: string;
}

// ---------------------------------------------------------------------------
// Event handler types
// ---------------------------------------------------------------------------

export interface ConversationEventMap {
  connected: ConversationInitiationMetadata["conversation_initiation_metadata_event"];
  disconnected: { reason: "agent" | "user" | "error" | "timeout"; code?: number };
  transcript: { text: string; is_final: boolean; event_id?: string };
  agent_response: { text: string; event_id?: string };
  agent_response_correction: { original: string; corrected: string };
  agent_response_part: { type: "start" | "delta" | "stop"; text?: string };
  audio: { audio: ArrayBuffer; event_id: string; alignment?: AudioAlignment };
  interruption: { event_id: string };
  error: { type: string; message: string; code?: number; details?: Record<string, unknown> };
  tool_call: { tool_call_id: string; tool_name: string; parameters: Record<string, unknown> };
  mcp_tool_call: { tool_call_id: string; tool_name: string; server_name: string; parameters: Record<string, unknown> };
  mcp_connection_status: { server_name: string; status: string; message?: string };
  agent_tool_request: { tool_name: string; tool_call_id: string; parameters: Record<string, unknown> };
  agent_tool_response: { tool_name: string; tool_call_id: string; result: string; is_error: boolean };
  vad_score: { score: number };
  turn_probability: { probability: number };
  ping: { event_id: number; ping_ms?: number };
  status_change: { status: ConversationStatus; previous: ConversationStatus };
  mode_change: { mode: ConversationMode; previous: ConversationMode };
  guardrail_triggered: { guardrail_id: string; message: string };
}

export type ConversationEventHandler<K extends keyof ConversationEventMap> = (
  data: ConversationEventMap[K]
) => void;

// ---------------------------------------------------------------------------
// Client tool handler
// ---------------------------------------------------------------------------

export type ClientToolHandler = (
  parameters: Record<string, unknown>
) => string | Promise<string>;

// ---------------------------------------------------------------------------
// REST API types
// ---------------------------------------------------------------------------

export interface AgentCreateConfig {
  name: string;
  prompt: string;
  first_message?: string;
  language?: string;
  voice_id?: string;
  model_id?: string;
  max_duration_seconds?: number;
  requires_auth?: boolean;
  tts_speed?: number;
  tts_stability?: number;
  tts_similarity_boost?: number;
  tools?: ToolDefinition[];
}

export interface AgentUpdateConfig {
  name?: string;
  prompt?: string;
  first_message?: string;
  language?: string;
  voice_id?: string;
  model_id?: string;
  max_duration_seconds?: number;
  requires_auth?: boolean;
  tts_speed?: number;
  tts_stability?: number;
  tts_similarity_boost?: number;
  tools?: ToolDefinition[];
}

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  is_client_tool?: boolean;
}

export interface AgentResponse {
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
  tools: ToolDefinition[];
  created_at: string;
  updated_at: string;
}

export interface SignedUrlResponse {
  signed_url: string;
}

export interface ConversationRecord {
  conversation_id: string;
  agent_id: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number;
  text_only: boolean;
  user_id: string | null;
  end_reason: string | null;
}

export interface TextToSpeechOptions {
  model_id?: string;
  voice_settings?: {
    stability?: number;
    similarity_boost?: number;
    style?: number;
    use_speaker_boost?: boolean;
  };
  output_format?: string;
}

export interface ConvAIApiClientConfig {
  baseUrl?: string;
  apiKey?: string;
  timeout?: number;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const SDK_VERSION = "1.0.0";
export const SDK_SOURCE = "echo_convai_js_sdk";
export const DEFAULT_SERVER_URL = "wss://echo-convai-gateway.bmcii1976.workers.dev";
export const DEFAULT_API_URL = "https://echo-convai-gateway.bmcii1976.workers.dev";
export const WS_SUBPROTOCOL = "convai";
export const MAX_WS_MESSAGE_SIZE = 16 * 1024 * 1024;
export const DEFAULT_RECONNECT_ATTEMPTS = 5;
export const DEFAULT_RECONNECT_BASE_DELAY = 1000;
export const DEFAULT_AUDIO_FORMAT: AudioFormatString = "pcm_16000";
