// ============================================================================
// Echo ConvAI Gateway — Durable Object: ConversationSession
// Manages a single WebSocket conversation lifecycle
// ============================================================================

import type {
  Env,
  AgentConfig,
  SessionState,
  SessionMetrics,
  ClientMessage,
  ServerMessage,
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
  ConversationConfigOverride,
  AudioFormatString,
} from "./types";

import {
  PING_INTERVAL_MS,
  DEFAULT_MAX_DURATION_MS,
  DEFAULT_AUDIO_FORMAT,
  INACTIVITY_TIMEOUT_MS,
  WS_CLOSE_NORMAL,
  WS_CLOSE_POLICY_VIOLATION,
  WS_CLOSE_INTERNAL_ERROR,
  WS_SUBPROTOCOL,
} from "./types";

export class ConversationSession implements DurableObject {
  private state: DurableObjectState;
  private clientWs: WebSocket | null = null;
  private backendWs: WebSocket | null = null;
  private session: SessionState | null = null;
  private pingInterval: ReturnType<typeof setInterval> | null = null;
  private durationTimeout: ReturnType<typeof setTimeout> | null = null;
  private inactivityTimeout: ReturnType<typeof setTimeout> | null = null;
  private bravoTunnelUrl: string = "";

  constructor(state: DurableObjectState, _env: Env) {
    this.state = state;
  }

  async fetch(request: Request): Promise<Response> {
    const conversationId = request.headers.get("X-Conversation-Id");
    const agentConfigStr = request.headers.get("X-Agent-Config");
    this.bravoTunnelUrl = request.headers.get("X-Bravo-Tunnel-Url") || "";
    const userId = request.headers.get("X-User-Id") || null;

    if (!conversationId || !agentConfigStr) {
      return Response.json({ error: "Missing conversation headers" }, { status: 400 });
    }

    let agentConfig: AgentConfig;
    try {
      agentConfig = JSON.parse(agentConfigStr);
    } catch {
      return Response.json({ error: "Invalid agent config" }, { status: 400 });
    }

    // Create WebSocket pair
    const pair = new WebSocketPair();
    const [client, server] = [pair[0], pair[1]];

    // Accept the server-side WebSocket
    this.state.acceptWebSocket(server);
    this.clientWs = server;

    // Initialize session state
    const now = Date.now();
    const maxDuration = agentConfig.max_duration_seconds
      ? agentConfig.max_duration_seconds * 1000
      : DEFAULT_MAX_DURATION_MS;

    this.session = {
      conversation_id: conversationId,
      agent_id: agentConfig.agent_id,
      agent_config: agentConfig,
      user_id: userId,
      text_only: false,
      started_at: now,
      max_duration_ms: maxDuration,
      last_activity_at: now,
      last_ping_at: 0,
      ping_event_id: 0,
      audio_event_id: 0,
      is_initialized: false,
      is_ended: false,
      dynamic_variables: {},
      custom_llm_extra_body: {},
      conversation_config_override: {},
      metrics: {
        conversation_id: conversationId,
        agent_id: agentConfig.agent_id,
        started_at: now,
        ended_at: null,
        duration_ms: 0,
        audio_chunks_received: 0,
        audio_chunks_sent: 0,
        text_messages_received: 0,
        agent_responses_sent: 0,
        interruptions: 0,
        tool_calls: 0,
        pings_sent: 0,
        pongs_received: 0,
        errors: 0,
        end_reason: null,
        user_id: userId,
        text_only: false,
      },
    };

    // Start ping interval
    this.startPingInterval();

    // Start max duration enforcement
    this.durationTimeout = setTimeout(() => {
      this.endSession("max_duration_exceeded");
    }, maxDuration);

    // Start inactivity timeout
    this.resetInactivityTimeout();

    // Return the client side of the WebSocket pair
    // Negotiate the "convai" subprotocol
    const responseHeaders = new Headers();
    const requestedProtocols = request.headers.get("Sec-WebSocket-Protocol") || "";
    if (requestedProtocols.includes(WS_SUBPROTOCOL)) {
      responseHeaders.set("Sec-WebSocket-Protocol", WS_SUBPROTOCOL);
    }

    return new Response(null, {
      status: 101,
      webSocket: client,
      headers: responseHeaders,
    });
  }

  // -----------------------------------------------------------------------
  // WebSocket event handlers (Hibernation API)
  // -----------------------------------------------------------------------

  async webSocketMessage(ws: WebSocket, rawMessage: string | ArrayBuffer): Promise<void> {
    if (this.session?.is_ended) return;

    let message: string;
    if (typeof rawMessage === "string") {
      message = rawMessage;
    } else {
      message = new TextDecoder().decode(rawMessage);
    }

    let parsed: ClientMessage;
    try {
      parsed = JSON.parse(message);
    } catch {
      this.sendError("invalid_json", "Failed to parse message as JSON");
      return;
    }

    this.resetInactivityTimeout();

    // Route by message type
    if ("user_audio_chunk" in parsed && !("type" in parsed)) {
      await this.handleUserAudioChunk(parsed as UserAudioChunkMessage);
    } else if ("type" in parsed) {
      switch (parsed.type) {
        case "conversation_initiation_client_data":
          await this.handleInitiation(parsed as ConversationInitiationClientData);
          break;
        case "pong":
          this.handlePong(parsed as PongMessage);
          break;
        case "user_message":
          await this.handleUserMessage(parsed as UserTextMessage);
          break;
        case "user_activity":
          this.handleUserActivity(parsed as UserActivityMessage);
          break;
        case "feedback":
          await this.handleFeedback(parsed as FeedbackMessage);
          break;
        case "client_tool_result":
          await this.handleClientToolResult(parsed as ClientToolResultMessage);
          break;
        case "mcp_tool_approval_result":
          await this.handleMCPToolApproval(parsed as MCPToolApprovalResultMessage);
          break;
        case "contextual_update":
          await this.handleContextualUpdate(parsed as ContextualUpdateMessage);
          break;
        case "multimodal_message":
          await this.handleMultimodalMessage(parsed as MultimodalMessage);
          break;
        case "enclave_setup_config":
          // On-prem setup — store for backend relay
          break;
        default:
          this.sendError("unknown_message_type", `Unknown message type: ${(parsed as Record<string, unknown>).type}`);
      }
    } else {
      this.sendError("invalid_message", "Message must have a 'type' field or 'user_audio_chunk' field");
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string, wasClean: boolean): Promise<void> {
    await this.endSession(code === WS_CLOSE_NORMAL ? "client" : "client_error");
  }

  async webSocketError(ws: WebSocket, error: unknown): Promise<void> {
    if (this.session) {
      this.session.metrics.errors++;
    }
    await this.endSession("error");
  }

  // -----------------------------------------------------------------------
  // Message handlers
  // -----------------------------------------------------------------------

  private async handleInitiation(msg: ConversationInitiationClientData): Promise<void> {
    if (!this.session) return;

    if (this.session.is_initialized) {
      this.sendError("already_initialized", "Conversation already initialized");
      return;
    }

    // Store client overrides
    if (msg.dynamic_variables) {
      this.session.dynamic_variables = msg.dynamic_variables;
    }
    if (msg.custom_llm_extra_body) {
      this.session.custom_llm_extra_body = msg.custom_llm_extra_body;
    }
    if (msg.conversation_config_override) {
      this.session.conversation_config_override = msg.conversation_config_override;
    }
    if (msg.user_id) {
      this.session.user_id = msg.user_id;
      this.session.metrics.user_id = msg.user_id;
    }

    // Determine text-only mode
    const override = msg.conversation_config_override;
    this.session.text_only =
      override?.text_only === true ||
      override?.conversation?.text_only === true;
    this.session.metrics.text_only = this.session.text_only;

    // Apply agent config overrides from the client
    const agentConfig = this.session.agent_config;
    if (override?.agent?.prompt?.prompt) {
      agentConfig.prompt = override.agent.prompt.prompt;
    }
    if (override?.agent?.first_message) {
      agentConfig.first_message = override.agent.first_message;
    }
    if (override?.agent?.language) {
      agentConfig.language = override.agent.language;
    }
    if (override?.tts?.voice_id) {
      agentConfig.voice_id = override.tts.voice_id;
    }
    if (override?.tts?.speed !== undefined) {
      agentConfig.tts_speed = override.tts.speed;
    }
    if (override?.tts?.stability !== undefined) {
      agentConfig.tts_stability = override.tts.stability;
    }
    if (override?.tts?.similarity_boost !== undefined) {
      agentConfig.tts_similarity_boost = override.tts.similarity_boost;
    }

    this.session.is_initialized = true;

    // Determine audio formats
    const inputFormat: AudioFormatString = DEFAULT_AUDIO_FORMAT;
    const outputFormat: AudioFormatString = DEFAULT_AUDIO_FORMAT;

    // Send conversation_initiation_metadata (MUST be first server message)
    this.sendToClient({
      type: "conversation_initiation_metadata",
      conversation_initiation_metadata_event: {
        conversation_id: this.session.conversation_id,
        agent_output_audio_format: outputFormat,
        user_input_audio_format: inputFormat,
      },
    });

    // Connect to the GPU backend for LLM + TTS processing
    await this.connectBackend();

    // Send first message if configured
    if (agentConfig.first_message) {
      this.sendToClient({
        type: "agent_response",
        agent_response_event: {
          agent_response: agentConfig.first_message,
        },
      });
      this.session.metrics.agent_responses_sent++;

      // If not text-only, synthesize the first message audio via backend
      if (!this.session.text_only) {
        this.relayToBackend({
          type: "synthesize_first_message",
          text: agentConfig.first_message,
          voice_id: agentConfig.voice_id,
          conversation_id: this.session.conversation_id,
        });
      }
    }
  }

  private async handleUserAudioChunk(msg: UserAudioChunkMessage): Promise<void> {
    if (!this.session || !this.session.is_initialized) {
      this.sendError("not_initialized", "Send conversation_initiation_client_data first");
      return;
    }

    this.session.metrics.audio_chunks_received++;
    this.session.last_activity_at = Date.now();

    // Relay audio to GPU backend for STT + LLM processing
    this.relayToBackend({
      type: "user_audio",
      audio_base_64: msg.user_audio_chunk,
      conversation_id: this.session.conversation_id,
    });
  }

  private handlePong(msg: PongMessage): void {
    if (!this.session) return;
    this.session.metrics.pongs_received++;
    this.session.last_activity_at = Date.now();
  }

  private async handleUserMessage(msg: UserTextMessage): Promise<void> {
    if (!this.session || !this.session.is_initialized) {
      this.sendError("not_initialized", "Send conversation_initiation_client_data first");
      return;
    }

    this.session.metrics.text_messages_received++;
    this.session.last_activity_at = Date.now();

    // Send user transcript event back to client
    this.sendToClient({
      type: "user_transcript",
      user_transcription_event: {
        user_transcript: msg.text,
      },
    });

    // Relay text to backend for LLM processing
    this.relayToBackend({
      type: "user_text",
      text: msg.text,
      conversation_id: this.session.conversation_id,
    });
  }

  private handleUserActivity(_msg: UserActivityMessage): void {
    if (!this.session) return;
    this.session.last_activity_at = Date.now();
  }

  private async handleFeedback(msg: FeedbackMessage): Promise<void> {
    if (!this.session) return;
    this.session.last_activity_at = Date.now();

    // Relay feedback to backend
    this.relayToBackend({
      type: "feedback",
      score: msg.score,
      event_id: msg.event_id,
      conversation_id: this.session.conversation_id,
    });
  }

  private async handleClientToolResult(msg: ClientToolResultMessage): Promise<void> {
    if (!this.session) return;
    this.session.last_activity_at = Date.now();

    // Relay tool result to backend so LLM can continue
    this.relayToBackend({
      type: "client_tool_result",
      tool_call_id: msg.tool_call_id,
      result: msg.result,
      is_error: msg.is_error,
      conversation_id: this.session.conversation_id,
    });
  }

  private async handleMCPToolApproval(msg: MCPToolApprovalResultMessage): Promise<void> {
    if (!this.session) return;
    this.session.last_activity_at = Date.now();

    this.relayToBackend({
      type: "mcp_tool_approval_result",
      tool_call_id: msg.tool_call_id,
      is_approved: msg.is_approved,
      conversation_id: this.session.conversation_id,
    });
  }

  private async handleContextualUpdate(msg: ContextualUpdateMessage): Promise<void> {
    if (!this.session) return;
    this.session.last_activity_at = Date.now();

    // Relay context to backend for LLM context injection
    this.relayToBackend({
      type: "contextual_update",
      text: msg.text,
      conversation_id: this.session.conversation_id,
    });
  }

  private async handleMultimodalMessage(msg: MultimodalMessage): Promise<void> {
    if (!this.session) return;
    this.session.last_activity_at = Date.now();

    // Relay multimodal message to backend
    this.relayToBackend({
      type: "multimodal_message",
      text: msg.text,
      file: msg.file,
      conversation_id: this.session.conversation_id,
    });
  }

  // -----------------------------------------------------------------------
  // Backend GPU connection
  // -----------------------------------------------------------------------

  private async connectBackend(): Promise<void> {
    if (!this.bravoTunnelUrl || !this.session) return;

    try {
      const backendUrl = `${this.bravoTunnelUrl.replace("https://", "wss://").replace("http://", "ws://")}/v1/convai/backend`;

      // Use fetch-based WebSocket to the GPU backend
      const resp = await fetch(backendUrl, {
        headers: {
          "Upgrade": "websocket",
          "X-Conversation-Id": this.session.conversation_id,
          "X-Agent-Config": JSON.stringify(this.session.agent_config),
          "X-Text-Only": String(this.session.text_only),
          "X-Dynamic-Variables": JSON.stringify(this.session.dynamic_variables),
          "X-Custom-LLM-Extra-Body": JSON.stringify(this.session.custom_llm_extra_body),
        },
      });

      const ws = resp.webSocket;
      if (!ws) {
        console.error("Backend did not return WebSocket");
        return;
      }

      ws.accept();
      this.backendWs = ws;

      // Listen for messages from the backend and relay to client
      ws.addEventListener("message", (event: MessageEvent) => {
        this.handleBackendMessage(event.data as string);
      });

      ws.addEventListener("close", () => {
        this.backendWs = null;
      });

      ws.addEventListener("error", () => {
        this.backendWs = null;
      });
    } catch (err) {
      console.error("Failed to connect to backend:", err);
      // Continue without backend — the gateway still handles protocol
    }
  }

  private handleBackendMessage(raw: string): void {
    if (!this.session || this.session.is_ended) return;

    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    const msgType = msg.type as string;

    // Backend sends messages in server->client format — relay directly
    switch (msgType) {
      case "audio": {
        this.session.metrics.audio_chunks_sent++;
        this.session.audio_event_id++;
        // Ensure the audio event has our sequential event_id
        const audioMsg = msg as unknown as import("./types").AudioEvent;
        audioMsg.audio_event.event_id = String(this.session.audio_event_id);
        this.sendToClient(audioMsg);
        break;
      }

      case "agent_response":
        this.session.metrics.agent_responses_sent++;
        this.sendToClient(msg as unknown as ServerMessage);
        break;

      case "agent_chat_response_part":
        this.sendToClient(msg as unknown as ServerMessage);
        break;

      case "agent_response_correction":
        this.sendToClient(msg as unknown as ServerMessage);
        break;

      case "user_transcript":
        this.sendToClient(msg as unknown as ServerMessage);
        break;

      case "interruption":
        this.session.metrics.interruptions++;
        this.sendToClient(msg as unknown as ServerMessage);
        break;

      case "client_tool_call":
        this.session.metrics.tool_calls++;
        this.sendToClient(msg as unknown as ServerMessage);
        break;

      case "vad_score":
      case "mcp_tool_call":
      case "mcp_connection_status":
      case "agent_tool_request":
      case "agent_tool_response":
      case "asr_initiation_metadata":
      case "guardrail_triggered":
      case "internal_tentative_agent_response":
        this.sendToClient(msg as unknown as ServerMessage);
        break;

      case "error": {
        this.session.metrics.errors++;
        const errorMsg = msg as unknown as import("./types").ErrorEvent;
        this.sendToClient(errorMsg);
        // max_duration_exceeded ends the session
        if (errorMsg.error_event.error_type === "max_duration_exceeded") {
          this.endSession("max_duration_exceeded");
        }
        break;
      }

      case "end_call":
        this.endSession("agent");
        break;

      default:
        // Unknown backend message — relay as-is
        this.sendToClient(msg as unknown as ServerMessage);
        break;
    }
  }

  private relayToBackend(msg: Record<string, unknown>): void {
    if (!this.backendWs) return;

    try {
      this.backendWs.send(JSON.stringify(msg));
    } catch {
      // Backend disconnected
      this.backendWs = null;
    }
  }

  // -----------------------------------------------------------------------
  // Outbound messaging to client
  // -----------------------------------------------------------------------

  private sendToClient(msg: ServerMessage | Record<string, unknown>): void {
    if (!this.clientWs || this.session?.is_ended) return;

    try {
      this.clientWs.send(JSON.stringify(msg));
    } catch {
      // Client disconnected
    }
  }

  private sendError(errorType: string, message: string, code?: number): void {
    if (!this.session) return;
    this.session.metrics.errors++;

    this.sendToClient({
      type: "error",
      error_event: {
        error_type: errorType,
        message,
        code: code ?? 400,
      },
    });
  }

  // -----------------------------------------------------------------------
  // Ping / keepalive
  // -----------------------------------------------------------------------

  private startPingInterval(): void {
    this.pingInterval = setInterval(() => {
      if (!this.session || this.session.is_ended) {
        this.stopPingInterval();
        return;
      }

      this.session.ping_event_id++;
      this.session.last_ping_at = Date.now();
      this.session.metrics.pings_sent++;

      const latency = this.session.metrics.pongs_received > 0
        ? Math.round((Date.now() - this.session.started_at) / this.session.metrics.pings_sent)
        : undefined;

      this.sendToClient({
        type: "ping",
        ping_event: {
          event_id: this.session.ping_event_id,
          ping_ms: latency,
        },
      });
    }, PING_INTERVAL_MS);
  }

  private stopPingInterval(): void {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }

  // -----------------------------------------------------------------------
  // Inactivity timeout
  // -----------------------------------------------------------------------

  private resetInactivityTimeout(): void {
    if (this.inactivityTimeout) {
      clearTimeout(this.inactivityTimeout);
    }
    this.inactivityTimeout = setTimeout(() => {
      this.endSession("inactivity_timeout");
    }, INACTIVITY_TIMEOUT_MS);
  }

  // -----------------------------------------------------------------------
  // Session lifecycle
  // -----------------------------------------------------------------------

  private async endSession(reason: string): Promise<void> {
    if (!this.session || this.session.is_ended) return;
    this.session.is_ended = true;

    const now = Date.now();
    this.session.metrics.ended_at = now;
    this.session.metrics.duration_ms = now - this.session.started_at;
    this.session.metrics.end_reason = reason;

    // Stop timers
    this.stopPingInterval();
    if (this.durationTimeout) {
      clearTimeout(this.durationTimeout);
      this.durationTimeout = null;
    }
    if (this.inactivityTimeout) {
      clearTimeout(this.inactivityTimeout);
      this.inactivityTimeout = null;
    }

    // Send max_duration_exceeded error if that was the reason
    if (reason === "max_duration_exceeded") {
      this.sendToClient({
        type: "error",
        error_event: {
          error_type: "max_duration_exceeded",
          message: `Conversation exceeded maximum duration of ${this.session.max_duration_ms / 1000} seconds`,
        },
      });
    }

    // Close backend WebSocket
    if (this.backendWs) {
      try {
        this.backendWs.close(WS_CLOSE_NORMAL, reason);
      } catch {
        // Already closed
      }
      this.backendWs = null;
    }

    // Close client WebSocket
    if (this.clientWs) {
      try {
        const closeCode = reason === "max_duration_exceeded" ? WS_CLOSE_POLICY_VIOLATION : WS_CLOSE_NORMAL;
        this.clientWs.close(closeCode, reason);
      } catch {
        // Already closed
      }
      this.clientWs = null;
    }

    // Persist session metrics to Durable Object storage
    await this.state.storage.put("metrics", this.session.metrics);
    await this.state.storage.put("ended", true);

    console.log(
      `[ConversationSession] Session ended: ${this.session.conversation_id} | ` +
      `reason=${reason} | duration=${this.session.metrics.duration_ms}ms | ` +
      `audio_in=${this.session.metrics.audio_chunks_received} | ` +
      `audio_out=${this.session.metrics.audio_chunks_sent} | ` +
      `text_in=${this.session.metrics.text_messages_received} | ` +
      `agent_out=${this.session.metrics.agent_responses_sent} | ` +
      `interruptions=${this.session.metrics.interruptions} | ` +
      `tools=${this.session.metrics.tool_calls} | ` +
      `errors=${this.session.metrics.errors}`
    );
  }

  // -----------------------------------------------------------------------
  // Alarm handler (for Hibernation API wake-up)
  // -----------------------------------------------------------------------

  async alarm(): Promise<void> {
    // Clean up if session is stale
    const ended = await this.state.storage.get<boolean>("ended");
    if (ended) {
      await this.state.storage.deleteAll();
    }
  }
}
