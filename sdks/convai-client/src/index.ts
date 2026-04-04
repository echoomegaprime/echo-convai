// ============================================================================
// @echo-prime/convai-client — Public API
// Drop-in replacement for @elevenlabs/client ConvAI functionality
// ============================================================================

// Conversation client
export { Conversation, startSession } from "./conversation.js";
export type { StartSessionOptions } from "./conversation.js";

// REST API client
export { ConvAIApiClient, ConvAIApiError } from "./api.js";

// Audio utilities
export {
  AudioCaptureManager,
  AudioPlaybackManager,
  arrayBufferToBase64,
  base64ToArrayBuffer,
  float32ToPcm16,
  pcm16ToFloat32,
  float32ToUlaw,
  ulawToFloat32,
  decodeAudioBytes,
  encodeAudioBytes,
  calculateRmsVolume,
  calculatePeakVolume,
  resampleLinear,
} from "./audio.js";

export type {
  AudioCaptureConfig,
  AudioChunkCallback,
  AudioPlaybackConfig,
} from "./audio.js";

// Types — all re-exported
export type {
  // Audio formats
  AudioFormatString,
  AudioEncoding,
  AudioFormat,
  AudioAlignment,

  // Configuration
  ConversationConfigOverride,
  SourceInfo,
  ToolMockConfig,
  ConversationOptions,
  ConnectOptions,

  // Client messages
  ConversationInitiationClientData,
  UserAudioChunkMessage,
  PongMessage,
  UserTextMessage,
  UserActivityMessage,
  FeedbackMessage,
  ClientToolResultMessage,
  MCPToolApprovalResultMessage,
  ContextualUpdateMessage,
  MultimodalMessage,
  ClientMessage,

  // Server messages
  ConversationInitiationMetadata,
  AudioEvent,
  AgentResponseEvent,
  AgentChatResponsePartEvent,
  AgentResponseCorrectionEvent,
  UserTranscriptEvent,
  InterruptionEvent,
  InternalTentativeAgentResponseEvent,
  PingEvent,
  ClientToolCallEvent,
  VADScoreEvent,
  MCPToolCallEvent,
  MCPConnectionStatusEvent,
  AgentToolRequestEvent,
  AgentToolResponseEvent,
  ASRInitiationMetadataEvent,
  GuardrailTriggeredEvent,
  ErrorEvent,
  ServerMessage,

  // State
  ConversationStatus,
  ConversationMode,
  ConversationEventMap,
  ConversationEventHandler,
  ClientToolHandler,

  // REST API types
  AgentCreateConfig,
  AgentUpdateConfig,
  AgentResponse,
  SignedUrlResponse,
  ConversationRecord,
  TextToSpeechOptions,
  ToolDefinition,
  ConvAIApiClientConfig,
} from "./types.js";

// Type utility
export { parseAudioFormat } from "./types.js";

// Constants
export {
  SDK_VERSION,
  SDK_SOURCE,
  DEFAULT_SERVER_URL,
  DEFAULT_API_URL,
  WS_SUBPROTOCOL,
  MAX_WS_MESSAGE_SIZE,
  DEFAULT_RECONNECT_ATTEMPTS,
  DEFAULT_RECONNECT_BASE_DELAY,
  DEFAULT_AUDIO_FORMAT,
} from "./types.js";
