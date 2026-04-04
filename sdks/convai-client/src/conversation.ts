// ============================================================================
// @echo-prime/convai-client — Conversation
// Main WebSocket conversation client with auto-reconnect, tool handling,
// audio transport, and full event system.
// ============================================================================

import {
  type ConversationOptions,
  type ConnectOptions,
  type ConversationStatus,
  type ConversationMode,
  type ConversationEventMap,
  type ConversationEventHandler,
  type ClientToolHandler,
  type ServerMessage,
  type ConversationInitiationMetadata,
  type AudioFormatString,
  SDK_VERSION,
  SDK_SOURCE,
  DEFAULT_SERVER_URL,
  WS_SUBPROTOCOL,
  DEFAULT_RECONNECT_ATTEMPTS,
  DEFAULT_RECONNECT_BASE_DELAY,
  DEFAULT_AUDIO_FORMAT,
} from "./types.js";

import {
  arrayBufferToBase64,
  base64ToArrayBuffer,
} from "./audio.js";

// ---------------------------------------------------------------------------
// Typed event emitter (internal)
// ---------------------------------------------------------------------------

type ListenerMap = {
  [K in keyof ConversationEventMap]?: Set<ConversationEventHandler<K>>;
};

// ---------------------------------------------------------------------------
// WebSocket abstraction for Node.js compatibility
// ---------------------------------------------------------------------------

interface WebSocketLike {
  readonly readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number; reason: string }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

const WS_OPEN = 1;

// ---------------------------------------------------------------------------
// Conversation class
// ---------------------------------------------------------------------------

/**
 * Main client for Echo ConvAI WebSocket conversations.
 *
 * Manages the full lifecycle: connect, send/receive messages, audio transport,
 * client tool execution, ping/pong keepalive, interruption handling, and
 * reconnection with exponential backoff.
 *
 * Compatible with the ElevenLabs ConvAI WebSocket protocol.
 *
 * @example
 * ```typescript
 * const conversation = new Conversation();
 *
 * conversation.on('agent_response', ({ text }) => console.log('Agent:', text));
 * conversation.on('transcript', ({ text }) => console.log('User:', text));
 * conversation.on('error', ({ message }) => console.error('Error:', message));
 *
 * await conversation.connect('agent_abc123');
 *
 * conversation.sendMessage('Hello!');
 *
 * // Later:
 * conversation.endSession();
 * ```
 */
export class Conversation {
  // Connection state
  private socket: WebSocketLike | null = null;
  private _status: ConversationStatus = "disconnected";
  private _mode: ConversationMode = "listening";
  private _conversationId: string | null = null;
  private _agentId: string | null = null;
  private inputAudioFormat: AudioFormatString = DEFAULT_AUDIO_FORMAT;
  private outputAudioFormat: AudioFormatString = DEFAULT_AUDIO_FORMAT;

  // Reconnection state
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private shouldReconnect = false;
  private lastConnectOptions: ConnectOptions | null = null;

  // Interruption tracking
  private lastInterruptId: number = -1;

  // Keepalive
  private pingTimer: ReturnType<typeof setInterval> | null = null;

  // Events
  private listeners: ListenerMap = {};

  // Client tools
  private clientTools: Map<string, ClientToolHandler> = new Map();

  // Options
  private options: ConversationOptions;

  constructor(options: ConversationOptions = {}) {
    this.options = options;
  }

  // -------------------------------------------------------------------------
  // Public properties
  // -------------------------------------------------------------------------

  /** Current connection status */
  get status(): ConversationStatus {
    return this._status;
  }

  /** Current conversation mode (listening or speaking) */
  get mode(): ConversationMode {
    return this._mode;
  }

  /** Conversation ID assigned by the server */
  get conversationId(): string | null {
    return this._conversationId;
  }

  /** Agent ID connected to */
  get agentId(): string | null {
    return this._agentId;
  }

  /** Whether we are currently connected */
  get isConnected(): boolean {
    return this._status === "connected";
  }

  /** Whether the agent is currently speaking */
  get isSpeaking(): boolean {
    return this._mode === "speaking";
  }

  /** Whether the SDK is listening for user input */
  get isListening(): boolean {
    return this._mode === "listening";
  }

  /** The negotiated input audio format */
  get userInputAudioFormat(): AudioFormatString {
    return this.inputAudioFormat;
  }

  /** The negotiated output audio format */
  get agentOutputAudioFormat(): AudioFormatString {
    return this.outputAudioFormat;
  }

  // -------------------------------------------------------------------------
  // Connection
  // -------------------------------------------------------------------------

  /**
   * Connect to an Echo ConvAI agent via WebSocket.
   *
   * @param agentId The agent ID to connect to (ignored if signedUrl is set in options).
   * @param options Additional connect options (merged with constructor options).
   * @returns Promise that resolves when the connection is established and
   *          conversation_initiation_metadata is received.
   */
  async connect(agentId?: string, options?: ConversationOptions): Promise<string> {
    if (this._status === "connected" || this._status === "connecting") {
      throw new Error(`Cannot connect: already ${this._status}`);
    }

    const mergedOptions: ConnectOptions = { ...this.options, ...options, agentId };
    this.lastConnectOptions = mergedOptions;
    this._agentId = agentId ?? mergedOptions.agentId ?? null;
    this.shouldReconnect = mergedOptions.autoReconnect !== false;

    return this.doConnect(mergedOptions);
  }

  private doConnect(options: ConnectOptions): Promise<string> {
    return new Promise<string>((resolve, reject) => {
      this.setStatus("connecting");

      // Build WebSocket URL
      const wsUrl = this.buildWsUrl(options);

      // Build subprotocols
      const protocols: string[] = [WS_SUBPROTOCOL];
      if (options.authorization) {
        protocols.push(`bearer.${options.authorization}`);
      }

      // Create WebSocket (browser native or polyfilled for Node.js)
      try {
        const WS = typeof WebSocket !== "undefined" ? WebSocket : this.getNodeWebSocket();
        this.socket = new WS(wsUrl, protocols) as unknown as WebSocketLike;
      } catch (err) {
        this.setStatus("error");
        reject(new Error(`Failed to create WebSocket: ${err instanceof Error ? err.message : String(err)}`));
        return;
      }

      let metadataReceived = false;

      this.socket.onopen = () => {
        // Send conversation_initiation_client_data
        this.sendJson({
          type: "conversation_initiation_client_data",
          conversation_config_override: this.buildConfigOverride(options),
          custom_llm_extra_body: options.customLlmExtraBody ?? {},
          dynamic_variables: options.dynamicVariables ?? {},
          user_id: options.userId,
          source_info: { source: SDK_SOURCE, version: SDK_VERSION },
          tool_mock_config: options.toolMockConfig,
        });
      };

      this.socket.onmessage = (event: { data: unknown }) => {
        const raw = typeof event.data === "string" ? event.data : String(event.data);
        let message: ServerMessage;
        try {
          message = JSON.parse(raw) as ServerMessage;
        } catch {
          return;
        }

        // First message must be conversation_initiation_metadata
        if (!metadataReceived && message.type === "conversation_initiation_metadata") {
          metadataReceived = true;
          this.handleInitMetadata(message as ConversationInitiationMetadata);
          this.reconnectAttempts = 0;
          resolve(this._conversationId!);
          return;
        }

        if (metadataReceived) {
          this.handleMessage(message);
        }
      };

      this.socket.onerror = () => {
        // Let onclose handle the actual disconnect logic
      };

      this.socket.onclose = (event: { code: number; reason: string }) => {
        this.clearPingTimer();

        const wasConnected = this._status === "connected";
        const reason = event.code === 1000 ? "agent" : "error";

        if (!metadataReceived) {
          this.setStatus("error");
          reject(new Error(
            `WebSocket closed before metadata (code=${event.code}, reason=${event.reason || "unknown"})`
          ));
          return;
        }

        if (wasConnected) {
          this.emitEvent("disconnected", { reason, code: event.code });
          this.setMode("listening");

          // Attempt reconnect if appropriate
          if (
            reason === "error" &&
            this.shouldReconnect &&
            this.reconnectAttempts < (options.maxReconnectAttempts ?? DEFAULT_RECONNECT_ATTEMPTS)
          ) {
            this.attemptReconnect();
          } else {
            this.setStatus("disconnected");
          }
        }
      };
    });
  }

  /**
   * Gracefully end the conversation session.
   */
  endSession(): void {
    this.shouldReconnect = false;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.clearPingTimer();

    if (this.socket && this.socket.readyState === WS_OPEN) {
      this.socket.close(1000, "user_ended");
    }

    this.socket = null;
    this._conversationId = null;
    this.lastInterruptId = -1;
    this.setMode("listening");
    this.setStatus("disconnected");
  }

  // -------------------------------------------------------------------------
  // Send methods
  // -------------------------------------------------------------------------

  /**
   * Send a base64-encoded PCM audio chunk to the server.
   * This is the low-level method. For raw ArrayBuffer, use sendAudio().
   */
  sendAudioBase64(base64Audio: string): void {
    this.sendJson({ user_audio_chunk: base64Audio });
  }

  /**
   * Send raw PCM audio data (ArrayBuffer) to the server.
   * The audio is base64-encoded before sending.
   */
  sendAudio(chunk: ArrayBuffer): void {
    this.sendAudioBase64(arrayBufferToBase64(chunk));
  }

  /**
   * Send a text message to the agent (text-only or hybrid mode).
   */
  sendMessage(text: string): void {
    this.sendJson({ type: "user_message", text });
  }

  /**
   * Send a contextual update — injects context without interrupting the agent.
   */
  sendContextualUpdate(text: string): void {
    this.sendJson({ type: "contextual_update", text });
  }

  /**
   * Send a multimodal message with optional text and file reference.
   */
  sendMultimodalMessage(text?: string, fileId?: string): void {
    const msg: Record<string, unknown> = { type: "multimodal_message" };
    if (text) msg.text = { type: "user_message", text };
    if (fileId) msg.file = { type: "file_input", file_id: fileId };
    this.sendJson(msg);
  }

  /**
   * Send user activity signal (prevents inactivity timeout).
   */
  sendUserActivity(): void {
    this.sendJson({ type: "user_activity" });
  }

  /**
   * Send feedback on the current agent response.
   */
  sendFeedback(score: "like" | "dislike", eventId?: number): void {
    this.sendJson({ type: "feedback", score, event_id: eventId });
  }

  /**
   * Send MCP tool approval result.
   */
  sendMCPToolApproval(toolCallId: string, isApproved: boolean): void {
    this.sendJson({ type: "mcp_tool_approval_result", tool_call_id: toolCallId, is_approved: isApproved });
  }

  // -------------------------------------------------------------------------
  // Client tools
  // -------------------------------------------------------------------------

  /**
   * Register a client-side tool handler.
   * When the server invokes this tool, the handler is called and
   * the result is automatically sent back.
   *
   * @param name Tool name (must match server-side tool definition).
   * @param handler Function that receives parameters and returns result string.
   */
  registerToolHandler(name: string, handler: ClientToolHandler): void {
    this.clientTools.set(name, handler);
  }

  /**
   * Unregister a client-side tool handler.
   */
  unregisterToolHandler(name: string): void {
    this.clientTools.delete(name);
  }

  /**
   * Manually send a tool result (for tools handled outside the SDK).
   */
  sendToolResult(toolCallId: string, result: string, isError = false): void {
    this.sendJson({
      type: "client_tool_result",
      tool_call_id: toolCallId,
      result,
      is_error: isError,
    });
  }

  // -------------------------------------------------------------------------
  // Events
  // -------------------------------------------------------------------------

  /**
   * Subscribe to a conversation event.
   * Returns an unsubscribe function.
   */
  on<K extends keyof ConversationEventMap>(
    event: K,
    handler: ConversationEventHandler<K>
  ): () => void {
    if (!this.listeners[event]) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (this.listeners as any)[event] = new Set();
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (this.listeners[event] as Set<any>).add(handler);
    return () => this.off(event, handler);
  }

  /**
   * Unsubscribe from a conversation event.
   */
  off<K extends keyof ConversationEventMap>(
    event: K,
    handler: ConversationEventHandler<K>
  ): void {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (this.listeners[event] as Set<any> | undefined)?.delete(handler);
  }

  /**
   * Remove all listeners for an event (or all events if none specified).
   */
  removeAllListeners(event?: keyof ConversationEventMap): void {
    if (event) {
      delete this.listeners[event];
    } else {
      this.listeners = {};
    }
  }

  // -------------------------------------------------------------------------
  // Private: message handling
  // -------------------------------------------------------------------------

  private handleInitMetadata(msg: ConversationInitiationMetadata): void {
    const meta = msg.conversation_initiation_metadata_event;
    this._conversationId = meta.conversation_id;
    this.outputAudioFormat = meta.agent_output_audio_format ?? DEFAULT_AUDIO_FORMAT;
    this.inputAudioFormat = meta.user_input_audio_format ?? DEFAULT_AUDIO_FORMAT;

    this.setStatus("connected");
    this.startPingTimer();

    this.emitEvent("connected", meta);
  }

  private handleMessage(msg: ServerMessage): void {
    switch (msg.type) {
      case "audio":
        this.handleAudioEvent(msg);
        break;
      case "agent_response":
        this.emitEvent("agent_response", {
          text: msg.agent_response_event.agent_response,
          event_id: msg.agent_response_event.event_id,
        });
        break;
      case "agent_chat_response_part":
        this.emitEvent("agent_response_part", {
          type: msg.text_response_part.type,
          text: msg.text_response_part.text,
        });
        break;
      case "agent_response_correction":
        this.emitEvent("agent_response_correction", {
          original: msg.agent_response_correction_event.original_agent_response,
          corrected: msg.agent_response_correction_event.corrected_agent_response,
        });
        break;
      case "user_transcript":
        this.emitEvent("transcript", {
          text: msg.user_transcription_event.user_transcript,
          is_final: true,
          event_id: msg.user_transcription_event.event_id,
        });
        break;
      case "interruption":
        this.handleInterruption(msg);
        break;
      case "ping":
        this.handlePing(msg);
        break;
      case "client_tool_call":
        this.handleToolCall(msg);
        break;
      case "vad_score":
        this.emitEvent("vad_score", { score: msg.vad_score_event.vad_score });
        break;
      case "mcp_tool_call":
        this.emitEvent("mcp_tool_call", msg.mcp_tool_call);
        break;
      case "mcp_connection_status":
        this.emitEvent("mcp_connection_status", msg.mcp_connection_status);
        break;
      case "agent_tool_request":
        this.emitEvent("agent_tool_request", msg.agent_tool_request);
        break;
      case "agent_tool_response":
        this.handleAgentToolResponse(msg);
        break;
      case "guardrail_triggered":
        this.emitEvent("guardrail_triggered", msg.guardrail_triggered_event);
        break;
      case "error":
        this.handleError(msg);
        break;
      case "asr_initiation_metadata":
      case "internal_tentative_agent_response":
        // These are informational/debug — no user-facing event needed
        break;
      default:
        // Unknown message type — silently ignore for forward compatibility
        break;
    }
  }

  private handleAudioEvent(msg: ServerMessage & { type: "audio" }): void {
    const audioEvent = msg.audio_event;
    const eventId = parseInt(audioEvent.event_id, 10);

    // Drop audio events from before the last interruption
    if (!isNaN(eventId) && eventId <= this.lastInterruptId) {
      return;
    }

    this.setMode("speaking");

    const audioBuffer = base64ToArrayBuffer(audioEvent.audio_base_64);
    this.emitEvent("audio", {
      audio: audioBuffer,
      event_id: audioEvent.event_id,
      alignment: audioEvent.alignment,
    });
  }

  private handleInterruption(msg: ServerMessage & { type: "interruption" }): void {
    const eventId = parseInt(msg.interruption_event.event_id, 10);
    if (!isNaN(eventId)) {
      this.lastInterruptId = eventId;
    }
    this.setMode("listening");
    this.emitEvent("interruption", { event_id: msg.interruption_event.event_id });
  }

  private handlePing(msg: ServerMessage & { type: "ping" }): void {
    // Respond with pong immediately
    this.sendJson({ type: "pong", event_id: msg.ping_event.event_id });
    this.emitEvent("ping", {
      event_id: msg.ping_event.event_id,
      ping_ms: msg.ping_event.ping_ms,
    });
  }

  private handleToolCall(msg: ServerMessage & { type: "client_tool_call" }): void {
    const { tool_call_id, tool_name, parameters } = msg.client_tool_call;

    this.emitEvent("tool_call", { tool_call_id, tool_name, parameters });

    // Auto-execute if handler is registered
    const handler = this.clientTools.get(tool_name);
    if (handler) {
      this.executeToolHandler(tool_call_id, tool_name, parameters, handler);
    }
  }

  private async executeToolHandler(
    toolCallId: string,
    toolName: string,
    parameters: Record<string, unknown>,
    handler: ClientToolHandler
  ): Promise<void> {
    try {
      const result = await handler(parameters);
      this.sendToolResult(toolCallId, result, false);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : String(err);
      this.sendToolResult(
        toolCallId,
        `Tool "${toolName}" failed: ${errorMessage}`,
        true
      );
    }
  }

  private handleAgentToolResponse(msg: ServerMessage & { type: "agent_tool_response" }): void {
    const resp = msg.agent_tool_response;
    // If the tool is "end_call", the agent is ending the session
    if (resp.tool_name === "end_call") {
      this.emitEvent("disconnected", { reason: "agent" });
      this.shouldReconnect = false;
      this.endSession();
      return;
    }
    this.emitEvent("agent_tool_response", resp);
  }

  private handleError(msg: ServerMessage & { type: "error" }): void {
    const err = msg.error_event;
    this.emitEvent("error", {
      type: err.error_type,
      message: err.message,
      code: err.code,
      details: err.details,
    });

    // max_duration_exceeded ends the session
    if (err.error_type === "max_duration_exceeded") {
      this.emitEvent("disconnected", { reason: "timeout" });
      this.shouldReconnect = false;
      this.endSession();
    }
  }

  // -------------------------------------------------------------------------
  // Private: connection helpers
  // -------------------------------------------------------------------------

  private buildWsUrl(options: ConnectOptions): string {
    if (options.signedUrl) {
      const sep = options.signedUrl.includes("?") ? "&" : "?";
      return `${options.signedUrl}${sep}source=${SDK_SOURCE}&version=${SDK_VERSION}`;
    }

    const serverUrl = options.serverUrl ?? DEFAULT_SERVER_URL;
    const base = serverUrl.replace(/\/$/, "");
    const params = new URLSearchParams({
      agent_id: options.agentId ?? "",
      source: SDK_SOURCE,
      version: SDK_VERSION,
    });

    return `${base}/v1/convai/conversation?${params.toString()}`;
  }

  private buildConfigOverride(options: ConnectOptions): Record<string, unknown> {
    const override: Record<string, unknown> = {};

    const agent: Record<string, unknown> = {};
    if (options.overridePrompt) agent.prompt = { prompt: options.overridePrompt };
    if (options.overrideFirstMessage) agent.first_message = options.overrideFirstMessage;
    if (options.overrideLanguage) agent.language = options.overrideLanguage;
    if (Object.keys(agent).length > 0) override.agent = agent;

    const tts: Record<string, unknown> = {};
    if (options.overrideVoiceId) tts.voice_id = options.overrideVoiceId;
    if (options.ttsSpeed !== undefined) tts.speed = options.ttsSpeed;
    if (options.ttsStability !== undefined) tts.stability = options.ttsStability;
    if (options.ttsSimilarityBoost !== undefined) tts.similarity_boost = options.ttsSimilarityBoost;
    if (Object.keys(tts).length > 0) override.tts = tts;

    if (options.textOnly) {
      override.conversation = { text_only: true };
      override.text_only = true; // Python SDK compat
    }

    return override;
  }

  private sendJson(data: unknown): void {
    if (this.socket && this.socket.readyState === WS_OPEN) {
      this.socket.send(JSON.stringify(data));
    }
  }

  // -------------------------------------------------------------------------
  // Private: reconnection
  // -------------------------------------------------------------------------

  private attemptReconnect(): void {
    this.reconnectAttempts++;
    this.setStatus("reconnecting");

    const maxAttempts = this.lastConnectOptions?.maxReconnectAttempts ?? DEFAULT_RECONNECT_ATTEMPTS;
    const baseDelay = this.lastConnectOptions?.reconnectBaseDelay ?? DEFAULT_RECONNECT_BASE_DELAY;

    // Exponential backoff with jitter
    const delay = Math.min(
      baseDelay * Math.pow(2, this.reconnectAttempts - 1) + Math.random() * 500,
      30000
    );

    this.reconnectTimer = setTimeout(async () => {
      if (!this.shouldReconnect || !this.lastConnectOptions) return;
      try {
        await this.doConnect(this.lastConnectOptions);
      } catch {
        if (this.reconnectAttempts < maxAttempts && this.shouldReconnect) {
          this.attemptReconnect();
        } else {
          this.setStatus("disconnected");
          this.emitEvent("disconnected", { reason: "error" });
        }
      }
    }, delay);
  }

  // -------------------------------------------------------------------------
  // Private: keepalive
  // -------------------------------------------------------------------------

  private startPingTimer(): void {
    // We respond to server pings; no need to send our own.
    // But we do send user_activity periodically to prevent inactivity timeout.
    this.clearPingTimer();
    this.pingTimer = setInterval(() => {
      if (this._status === "connected") {
        this.sendUserActivity();
      }
    }, 30_000); // Every 30 seconds
  }

  private clearPingTimer(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  // -------------------------------------------------------------------------
  // Private: state management
  // -------------------------------------------------------------------------

  private setStatus(status: ConversationStatus): void {
    const previous = this._status;
    if (previous === status) return;
    this._status = status;
    this.emitEvent("status_change", { status, previous });
  }

  private setMode(mode: ConversationMode): void {
    const previous = this._mode;
    if (previous === mode) return;
    this._mode = mode;
    this.emitEvent("mode_change", { mode, previous });
  }

  private emitEvent<K extends keyof ConversationEventMap>(
    event: K,
    data: ConversationEventMap[K]
  ): void {
    const handlers = this.listeners[event] as Set<ConversationEventHandler<K>> | undefined;
    if (!handlers) return;
    for (const handler of handlers) {
      try {
        handler(data);
      } catch {
        // Don't let a handler error crash the event loop
      }
    }
  }

  // -------------------------------------------------------------------------
  // Private: Node.js WebSocket fallback
  // -------------------------------------------------------------------------

  private getNodeWebSocket(): new (url: string, protocols?: string[]) => WebSocketLike {
    // In Node.js 18+, there's a global WebSocket.
    // For older Node.js, the user must polyfill or install 'ws'.
    if (typeof globalThis.WebSocket !== "undefined") {
      return globalThis.WebSocket as unknown as new (url: string, protocols?: string[]) => WebSocketLike;
    }

    try {
      // Dynamic require for Node.js 'ws' package
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const ws = require("ws");
      return ws as unknown as new (url: string, protocols?: string[]) => WebSocketLike;
    } catch {
      throw new Error(
        "No WebSocket implementation found. " +
        "In Node.js < 21, install the 'ws' package: npm install ws"
      );
    }
  }
}

// ---------------------------------------------------------------------------
// Static factory (ElevenLabs-compatible API)
// ---------------------------------------------------------------------------

/**
 * Static helper to start a conversation session.
 * Mirrors the ElevenLabs `Conversation.startSession()` pattern.
 *
 * @example
 * ```typescript
 * const conversation = await startSession({
 *   agentId: 'agent_abc123',
 *   onAgentResponse: (text) => console.log('Agent:', text),
 *   onTranscript: (text) => console.log('User:', text),
 * });
 * ```
 */
export async function startSession(options: StartSessionOptions): Promise<Conversation> {
  const conversation = new Conversation({
    serverUrl: options.serverUrl,
    textOnly: options.textOnly,
    authorization: options.authorization,
    signedUrl: options.signedUrl,
    autoReconnect: options.autoReconnect,
    maxReconnectAttempts: options.maxReconnectAttempts,
    customLlmExtraBody: options.customLlmExtraBody,
    dynamicVariables: options.dynamicVariables,
    userId: options.userId,
    overridePrompt: options.overridePrompt,
    overrideFirstMessage: options.overrideFirstMessage,
    overrideLanguage: options.overrideLanguage,
    overrideVoiceId: options.overrideVoiceId,
    ttsSpeed: options.ttsSpeed,
    ttsStability: options.ttsStability,
    ttsSimilarityBoost: options.ttsSimilarityBoost,
  });

  // Register callbacks as event listeners
  if (options.onAgentResponse) {
    conversation.on("agent_response", ({ text }) => options.onAgentResponse!(text));
  }
  if (options.onAgentResponsePart) {
    conversation.on("agent_response_part", (data) => options.onAgentResponsePart!(data));
  }
  if (options.onTranscript) {
    conversation.on("transcript", ({ text }) => options.onTranscript!(text));
  }
  if (options.onAudio) {
    conversation.on("audio", ({ audio, event_id }) => options.onAudio!(audio, event_id));
  }
  if (options.onInterruption) {
    conversation.on("interruption", () => options.onInterruption!());
  }
  if (options.onError) {
    conversation.on("error", (err) => options.onError!(err));
  }
  if (options.onStatusChange) {
    conversation.on("status_change", ({ status }) => options.onStatusChange!(status));
  }
  if (options.onModeChange) {
    conversation.on("mode_change", ({ mode }) => options.onModeChange!(mode));
  }
  if (options.onToolCall) {
    conversation.on("tool_call", (data) => options.onToolCall!(data));
  }
  if (options.onDisconnected) {
    conversation.on("disconnected", (data) => options.onDisconnected!(data));
  }
  if (options.onConnected) {
    conversation.on("connected", (data) => options.onConnected!(data));
  }

  // Register client tools
  if (options.clientTools) {
    for (const [name, handler] of Object.entries(options.clientTools)) {
      conversation.registerToolHandler(name, handler);
    }
  }

  await conversation.connect(options.agentId);

  return conversation;
}

export interface StartSessionOptions extends ConversationOptions {
  agentId: string;
  onAgentResponse?: (text: string) => void;
  onAgentResponsePart?: (data: { type: string; text?: string }) => void;
  onTranscript?: (text: string) => void;
  onAudio?: (audio: ArrayBuffer, eventId: string) => void;
  onInterruption?: () => void;
  onError?: (error: { type: string; message: string; code?: number }) => void;
  onStatusChange?: (status: ConversationStatus) => void;
  onModeChange?: (mode: ConversationMode) => void;
  onToolCall?: (data: { tool_call_id: string; tool_name: string; parameters: Record<string, unknown> }) => void;
  onDisconnected?: (data: { reason: string; code?: number }) => void;
  onConnected?: (data: { conversation_id: string }) => void;
  clientTools?: Record<string, ClientToolHandler>;
}
