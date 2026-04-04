// ============================================================================
// @echo-prime/convai-client — REST API Client
// HTTP client for Echo ConvAI agent management, TTS, and conversation APIs.
// ============================================================================

import {
  type AgentCreateConfig,
  type AgentUpdateConfig,
  type AgentResponse,
  type SignedUrlResponse,
  type ConversationRecord,
  type TextToSpeechOptions,
  type ConvAIApiClientConfig,
  DEFAULT_API_URL,
  SDK_VERSION,
  SDK_SOURCE,
} from "./types.js";

// ---------------------------------------------------------------------------
// Error class
// ---------------------------------------------------------------------------

export class ConvAIApiError extends Error {
  public readonly status: number;
  public readonly statusText: string;
  public readonly body: unknown;

  constructor(status: number, statusText: string, body: unknown) {
    const bodyStr = typeof body === "string" ? body : JSON.stringify(body);
    super(`ConvAI API error ${status} ${statusText}: ${bodyStr}`);
    this.name = "ConvAIApiError";
    this.status = status;
    this.statusText = statusText;
    this.body = body;
  }
}

// ---------------------------------------------------------------------------
// Fetch wrapper (browser & Node.js compatible)
// ---------------------------------------------------------------------------

async function apiFetch(
  url: string,
  init: RequestInit,
  timeout: number
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      ...init,
      signal: controller.signal,
    });
    return response;
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// ConvAIApiClient
// ---------------------------------------------------------------------------

/**
 * REST API client for Echo ConvAI.
 *
 * Provides methods for agent CRUD, signed URL generation,
 * conversation listing, and text-to-speech.
 *
 * @example
 * ```typescript
 * const api = new ConvAIApiClient({ apiKey: 'your-api-key' });
 *
 * // Create an agent
 * const agent = await api.createAgent({
 *   name: 'My Agent',
 *   prompt: 'You are a helpful assistant.',
 * });
 *
 * // Get a signed URL for WebSocket connection
 * const { signed_url } = await api.getSignedUrl(agent.agent_id);
 * ```
 */
export class ConvAIApiClient {
  private baseUrl: string;
  private apiKey: string | undefined;
  private timeout: number;

  constructor(config: ConvAIApiClientConfig = {}) {
    this.baseUrl = (config.baseUrl ?? DEFAULT_API_URL).replace(/\/$/, "");
    this.apiKey = config.apiKey;
    this.timeout = config.timeout ?? 30_000;
  }

  // -------------------------------------------------------------------------
  // Agent CRUD
  // -------------------------------------------------------------------------

  /**
   * Create a new conversational AI agent.
   */
  async createAgent(config: AgentCreateConfig): Promise<AgentResponse> {
    return this.request<AgentResponse>("POST", "/v1/convai/agents/create", config);
  }

  /**
   * Get an agent by ID.
   */
  async getAgent(agentId: string): Promise<AgentResponse> {
    return this.request<AgentResponse>("GET", `/v1/convai/agents/${encodeURIComponent(agentId)}`);
  }

  /**
   * Update an existing agent.
   */
  async updateAgent(agentId: string, config: AgentUpdateConfig): Promise<AgentResponse> {
    return this.request<AgentResponse>(
      "PATCH",
      `/v1/convai/agents/${encodeURIComponent(agentId)}`,
      config
    );
  }

  /**
   * Delete an agent.
   */
  async deleteAgent(agentId: string): Promise<void> {
    await this.request<void>("DELETE", `/v1/convai/agents/${encodeURIComponent(agentId)}`);
  }

  /**
   * List all agents.
   */
  async listAgents(): Promise<AgentResponse[]> {
    return this.request<AgentResponse[]>("GET", "/v1/convai/agents");
  }

  // -------------------------------------------------------------------------
  // Conversation management
  // -------------------------------------------------------------------------

  /**
   * Get a signed WebSocket URL for connecting to an agent.
   * Required for agents with `requires_auth: true`.
   */
  async getSignedUrl(agentId: string): Promise<SignedUrlResponse> {
    return this.request<SignedUrlResponse>(
      "POST",
      "/v1/convai/conversation/get-signed-url",
      { agent_id: agentId }
    );
  }

  /**
   * List conversation records.
   * @param agentId Optional filter by agent ID.
   * @param limit Max number of records to return (default: 100).
   * @param offset Pagination offset (default: 0).
   */
  async listConversations(
    agentId?: string,
    limit = 100,
    offset = 0
  ): Promise<ConversationRecord[]> {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });
    if (agentId) params.set("agent_id", agentId);
    return this.request<ConversationRecord[]>("GET", `/v1/convai/conversations?${params.toString()}`);
  }

  /**
   * Get a specific conversation by ID.
   */
  async getConversation(conversationId: string): Promise<ConversationRecord> {
    return this.request<ConversationRecord>(
      "GET",
      `/v1/convai/conversations/${encodeURIComponent(conversationId)}`
    );
  }

  /**
   * Send feedback for a conversation.
   */
  async sendConversationFeedback(
    conversationId: string,
    feedback: { feedback?: "like" | "dislike"; rating?: number; comment?: string }
  ): Promise<void> {
    await this.request<void>(
      "POST",
      `/v1/convai/conversations/${encodeURIComponent(conversationId)}/feedback`,
      feedback
    );
  }

  // -------------------------------------------------------------------------
  // Text-to-Speech
  // -------------------------------------------------------------------------

  /**
   * Convert text to speech using a specific voice.
   * Returns raw audio bytes as an ArrayBuffer.
   *
   * @param voiceId The voice ID to use.
   * @param text The text to convert.
   * @param options Additional TTS options.
   */
  async textToSpeech(
    voiceId: string,
    text: string,
    options: TextToSpeechOptions = {}
  ): Promise<ArrayBuffer> {
    const body: Record<string, unknown> = { text };
    if (options.model_id) body.model_id = options.model_id;
    if (options.voice_settings) body.voice_settings = options.voice_settings;
    if (options.output_format) body.output_format = options.output_format;

    const url = `${this.baseUrl}/v1/text-to-speech/${encodeURIComponent(voiceId)}`;
    const headers = this.buildHeaders();
    headers["Content-Type"] = "application/json";

    const response = await apiFetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    }, this.timeout);

    if (!response.ok) {
      const errorBody = await response.text().catch(() => "");
      throw new ConvAIApiError(response.status, response.statusText, errorBody);
    }

    return response.arrayBuffer();
  }

  /**
   * Stream text-to-speech audio. Returns a ReadableStream of audio chunks.
   *
   * @param voiceId The voice ID to use.
   * @param text The text to convert.
   * @param options Additional TTS options.
   */
  async textToSpeechStream(
    voiceId: string,
    text: string,
    options: TextToSpeechOptions = {}
  ): Promise<ReadableStream<Uint8Array>> {
    const body: Record<string, unknown> = { text };
    if (options.model_id) body.model_id = options.model_id;
    if (options.voice_settings) body.voice_settings = options.voice_settings;
    if (options.output_format) body.output_format = options.output_format;

    const url = `${this.baseUrl}/v1/text-to-speech/${encodeURIComponent(voiceId)}/stream`;
    const headers = this.buildHeaders();
    headers["Content-Type"] = "application/json";

    const response = await apiFetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    }, this.timeout);

    if (!response.ok) {
      const errorBody = await response.text().catch(() => "");
      throw new ConvAIApiError(response.status, response.statusText, errorBody);
    }

    if (!response.body) {
      throw new Error("Response body is null — streaming not supported in this environment");
    }

    return response.body;
  }

  // -------------------------------------------------------------------------
  // Health
  // -------------------------------------------------------------------------

  /**
   * Check if the ConvAI gateway is healthy.
   */
  async health(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("GET", "/health");
  }

  // -------------------------------------------------------------------------
  // Private helpers
  // -------------------------------------------------------------------------

  private buildHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      "User-Agent": `echo-convai-sdk/${SDK_VERSION}`,
      "X-SDK-Source": SDK_SOURCE,
      "X-SDK-Version": SDK_VERSION,
    };
    if (this.apiKey) {
      headers["xi-api-key"] = this.apiKey;
      headers["Authorization"] = `Bearer ${this.apiKey}`;
    }
    return headers;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const headers = this.buildHeaders();

    const init: RequestInit = { method, headers };

    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }

    const response = await apiFetch(url, init, this.timeout);

    if (!response.ok) {
      let errorBody: unknown;
      try {
        errorBody = await response.json();
      } catch {
        errorBody = await response.text().catch(() => "");
      }
      throw new ConvAIApiError(response.status, response.statusText, errorBody);
    }

    // Handle 204 No Content (e.g., DELETE)
    if (response.status === 204) {
      return undefined as T;
    }

    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      return response.json() as Promise<T>;
    }

    return (await response.text()) as unknown as T;
  }
}
