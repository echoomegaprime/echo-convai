/**
 * Echo ConvAI API — ElevenLabs-Compatible REST API
 * Cloudflare Worker with Hono router, D1 persistence, R2 storage, JWT auth.
 *
 * Endpoints mirror the ElevenLabs ConvAI OpenAPI surface:
 *   - Agent CRUD (create, get, update, delete, list)
 *   - Conversation history (list, get with messages)
 *   - Signed URL generation (JWT-based WebSocket auth)
 *   - TTS proxy (standard + streaming)
 *   - Knowledge base (upload to R2, list)
 */

import { Hono } from "hono";
import { cors } from "hono/cors";
import { logger } from "hono/logger";
import * as jose from "jose";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Env {
  DB: D1Database;
  STORAGE: R2Bucket;
  ENVIRONMENT: string;
  TTS_BACKEND_URL: string;
  JWT_ISSUER: string;
  JWT_AUDIENCE: string;
  JWT_SECRET?: string;
  MASTER_API_KEY?: string;
}

interface AgentRow {
  agent_id: string;
  workspace_id: string;
  name: string;
  tags: string;
  conversation_config: string;
  platform_settings: string;
  metadata: string;
  created_at: string;
  updated_at: string;
  is_archived: number;
}

interface ConversationRow {
  conversation_id: string;
  agent_id: string;
  workspace_id: string;
  status: string;
  start_time: string;
  end_time: string | null;
  duration_secs: number;
  metadata: string;
  analysis: string | null;
  call_successful: number | null;
  rating: number | null;
  feedback_comment: string | null;
  user_id: string | null;
}

interface MessageRow {
  message_id: string;
  conversation_id: string;
  role: string;
  content: string;
  timestamp: string;
  metadata: string;
}

interface KnowledgeDocRow {
  document_id: string;
  agent_id: string;
  workspace_id: string;
  name: string;
  mime_type: string;
  size_bytes: number;
  r2_key: string;
  status: string;
  metadata: string;
  created_at: string;
  updated_at: string;
}

interface ApiKeyRow {
  id: string;
  key_hash: string;
  workspace_id: string;
  name: string;
  permissions: string;
  created_at: string;
  last_used_at: string | null;
  is_active: number;
}

/** Shape returned to callers for an agent. */
interface AgentResponse {
  agent_id: string;
  name: string;
  tags: string[];
  conversation_config: Record<string, unknown>;
  platform_settings: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

/** Shape returned for a conversation. */
interface ConversationResponse {
  conversation_id: string;
  agent_id: string;
  status: string;
  start_time: string;
  end_time: string | null;
  duration_secs: number;
  metadata: Record<string, unknown>;
  analysis: Record<string, unknown> | null;
  call_successful: boolean | null;
  rating: number | null;
  feedback_comment: string | null;
  user_id: string | null;
}

/** Conversation with nested messages array. */
interface ConversationDetailResponse extends ConversationResponse {
  messages: Array<{
    message_id: string;
    role: string;
    content: string;
    timestamp: string;
    metadata: Record<string, unknown>;
  }>;
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function generateId(prefix: string): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let result = prefix + "_";
  for (let i = 0; i < 24; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}

function safeParse<T = Record<string, unknown>>(json: string, fallback: T): T {
  try {
    return JSON.parse(json) as T;
  } catch {
    return fallback;
  }
}

function rowToAgent(row: AgentRow): AgentResponse {
  return {
    agent_id: row.agent_id,
    name: row.name,
    tags: safeParse<string[]>(row.tags, []),
    conversation_config: safeParse(row.conversation_config, {}),
    platform_settings: safeParse(row.platform_settings, {}),
    metadata: safeParse(row.metadata, {}),
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

function rowToConversation(row: ConversationRow): ConversationResponse {
  return {
    conversation_id: row.conversation_id,
    agent_id: row.agent_id,
    status: row.status,
    start_time: row.start_time,
    end_time: row.end_time,
    duration_secs: row.duration_secs,
    metadata: safeParse(row.metadata, {}),
    analysis: row.analysis ? safeParse(row.analysis, null) : null,
    call_successful: row.call_successful !== null ? row.call_successful === 1 : null,
    rating: row.rating,
    feedback_comment: row.feedback_comment,
    user_id: row.user_id,
  };
}

function rowToMessage(row: MessageRow) {
  return {
    message_id: row.message_id,
    role: row.role,
    content: row.content,
    timestamp: row.timestamp,
    metadata: safeParse(row.metadata, {}),
  };
}

async function hashApiKey(key: string): Promise<string> {
  const encoder = new TextEncoder();
  const data = encoder.encode(key);
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map((b) => b.toString(16).padStart(2, "0")).join("");
}

function getJwtSecret(env: Env): Uint8Array {
  const secret = env.JWT_SECRET || "echo-convai-default-jwt-secret-change-me";
  return new TextEncoder().encode(secret);
}

function errorResponse(status: number, message: string, detail?: string) {
  return Response.json(
    {
      detail: {
        status: status === 401 ? "unauthorized" : status === 404 ? "not_found" : "error",
        message,
        ...(detail ? { detail } : {}),
      },
    },
    { status },
  );
}

// ---------------------------------------------------------------------------
// Auth middleware
// ---------------------------------------------------------------------------

type HonoEnv = { Bindings: Env; Variables: { workspace_id: string } };

const app = new Hono<HonoEnv>();

app.use("*", cors({ origin: "*", allowMethods: ["GET", "POST", "PATCH", "DELETE", "OPTIONS"] }));
app.use("*", logger());

/**
 * API key authentication middleware.
 * Checks `xi-api-key` header. If MASTER_API_KEY env var is set, that key
 * is accepted directly (workspace = "default"). Otherwise looks up the
 * hashed key in the api_keys table.
 */
async function authMiddleware(
  c: { req: { header: (name: string) => string | undefined }; set: (key: string, value: string) => void; env: Env },
  next: () => Promise<void>,
) {
  const apiKey = c.req.header("xi-api-key");
  if (!apiKey) {
    return errorResponse(401, "Missing xi-api-key header");
  }

  // Fast path: master key check
  if (c.env.MASTER_API_KEY && apiKey === c.env.MASTER_API_KEY) {
    c.set("workspace_id", "default");
    return next();
  }

  // Look up hashed key in D1
  const hash = await hashApiKey(apiKey);
  const row = await c.env.DB.prepare(
    "SELECT workspace_id FROM api_keys WHERE key_hash = ? AND is_active = 1",
  )
    .bind(hash)
    .first<{ workspace_id: string }>();

  if (!row) {
    return errorResponse(401, "Invalid API key");
  }

  // Update last_used_at (fire-and-forget)
  c.env.DB.prepare("UPDATE api_keys SET last_used_at = datetime('now') WHERE key_hash = ?")
    .bind(hash)
    .run()
    .catch(() => {});

  c.set("workspace_id", row.workspace_id);
  return next();
}

// Apply auth to all /v1 routes
app.use("/v1/*", authMiddleware as never);

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

app.get("/", (c) =>
  c.json({
    service: "echo-convai-api",
    version: "1.0.0",
    status: "operational",
    timestamp: new Date().toISOString(),
  }),
);

app.get("/health", (c) =>
  c.json({ status: "ok", timestamp: new Date().toISOString() }),
);

// ---------------------------------------------------------------------------
// AGENTS
// ---------------------------------------------------------------------------

/**
 * POST /v1/convai/agents/create
 * Create a new agent. ElevenLabs-compatible body shape.
 */
app.post("/v1/convai/agents/create", async (c) => {
  const workspaceId = c.get("workspace_id");
  const body = await c.req.json<{
    name?: string;
    tags?: string[];
    conversation_config?: Record<string, unknown>;
    platform_settings?: Record<string, unknown>;
    metadata?: Record<string, unknown>;
  }>();

  const agentId = generateId("agt");
  const now = new Date().toISOString();

  const conversationConfig = body.conversation_config ?? buildDefaultConversationConfig();
  const platformSettings = body.platform_settings ?? {};
  const meta = body.metadata ?? {};
  const name = body.name ?? "";
  const tags = body.tags ?? [];

  await c.env.DB.prepare(
    `INSERT INTO agents (agent_id, workspace_id, name, tags, conversation_config, platform_settings, metadata, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  )
    .bind(
      agentId,
      workspaceId,
      name,
      JSON.stringify(tags),
      JSON.stringify(conversationConfig),
      JSON.stringify(platformSettings),
      JSON.stringify(meta),
      now,
      now,
    )
    .run();

  const agent: AgentResponse = {
    agent_id: agentId,
    name,
    tags,
    conversation_config: conversationConfig,
    platform_settings: platformSettings,
    metadata: meta,
    created_at: now,
    updated_at: now,
  };

  return c.json(agent, 201);
});

/**
 * GET /v1/convai/agents/:agent_id
 * Retrieve a single agent by ID.
 */
app.get("/v1/convai/agents/:agent_id", async (c) => {
  const agentId = c.req.param("agent_id");
  const workspaceId = c.get("workspace_id");

  const row = await c.env.DB.prepare(
    "SELECT * FROM agents WHERE agent_id = ? AND workspace_id = ?",
  )
    .bind(agentId, workspaceId)
    .first<AgentRow>();

  if (!row) {
    return errorResponse(404, "Agent not found", `No agent with id ${agentId}`);
  }

  return c.json(rowToAgent(row));
});

/**
 * PATCH /v1/convai/agents/:agent_id
 * Partial update of an agent. Merges provided fields.
 */
app.patch("/v1/convai/agents/:agent_id", async (c) => {
  const agentId = c.req.param("agent_id");
  const workspaceId = c.get("workspace_id");

  const existing = await c.env.DB.prepare(
    "SELECT * FROM agents WHERE agent_id = ? AND workspace_id = ?",
  )
    .bind(agentId, workspaceId)
    .first<AgentRow>();

  if (!existing) {
    return errorResponse(404, "Agent not found", `No agent with id ${agentId}`);
  }

  const body = await c.req.json<{
    name?: string;
    tags?: string[];
    conversation_config?: Record<string, unknown>;
    platform_settings?: Record<string, unknown>;
    metadata?: Record<string, unknown>;
  }>();

  const now = new Date().toISOString();

  const updatedName = body.name ?? existing.name;
  const updatedTags = body.tags ?? safeParse<string[]>(existing.tags, []);
  const updatedConfig = body.conversation_config
    ? deepMerge(safeParse(existing.conversation_config, {}), body.conversation_config)
    : safeParse(existing.conversation_config, {});
  const updatedPlatform = body.platform_settings
    ? deepMerge(safeParse(existing.platform_settings, {}), body.platform_settings)
    : safeParse(existing.platform_settings, {});
  const updatedMeta = body.metadata
    ? deepMerge(safeParse(existing.metadata, {}), body.metadata)
    : safeParse(existing.metadata, {});

  await c.env.DB.prepare(
    `UPDATE agents
     SET name = ?, tags = ?, conversation_config = ?, platform_settings = ?, metadata = ?, updated_at = ?
     WHERE agent_id = ? AND workspace_id = ?`,
  )
    .bind(
      updatedName,
      JSON.stringify(updatedTags),
      JSON.stringify(updatedConfig),
      JSON.stringify(updatedPlatform),
      JSON.stringify(updatedMeta),
      now,
      agentId,
      workspaceId,
    )
    .run();

  const agent: AgentResponse = {
    agent_id: agentId,
    name: updatedName,
    tags: updatedTags,
    conversation_config: updatedConfig,
    platform_settings: updatedPlatform,
    metadata: updatedMeta,
    created_at: existing.created_at,
    updated_at: now,
  };

  return c.json(agent);
});

/**
 * DELETE /v1/convai/agents/:agent_id
 * Delete an agent and cascade-remove conversations, messages, knowledge docs.
 */
app.delete("/v1/convai/agents/:agent_id", async (c) => {
  const agentId = c.req.param("agent_id");
  const workspaceId = c.get("workspace_id");

  const existing = await c.env.DB.prepare(
    "SELECT agent_id FROM agents WHERE agent_id = ? AND workspace_id = ?",
  )
    .bind(agentId, workspaceId)
    .first<{ agent_id: string }>();

  if (!existing) {
    return errorResponse(404, "Agent not found", `No agent with id ${agentId}`);
  }

  // Delete R2 knowledge base objects for this agent
  const docs = await c.env.DB.prepare(
    "SELECT r2_key FROM knowledge_documents WHERE agent_id = ?",
  )
    .bind(agentId)
    .all<{ r2_key: string }>();

  if (docs.results.length > 0) {
    const deletePromises = docs.results.map((d) => c.env.STORAGE.delete(d.r2_key));
    await Promise.allSettled(deletePromises);
  }

  // Cascade delete via D1 (messages -> conversations -> knowledge_documents -> agent)
  await c.env.DB.batch([
    c.env.DB.prepare(
      "DELETE FROM messages WHERE conversation_id IN (SELECT conversation_id FROM conversations WHERE agent_id = ?)",
    ).bind(agentId),
    c.env.DB.prepare("DELETE FROM conversations WHERE agent_id = ?").bind(agentId),
    c.env.DB.prepare("DELETE FROM knowledge_documents WHERE agent_id = ?").bind(agentId),
    c.env.DB.prepare("DELETE FROM signed_url_tokens WHERE agent_id = ?").bind(agentId),
    c.env.DB.prepare("DELETE FROM agents WHERE agent_id = ? AND workspace_id = ?").bind(
      agentId,
      workspaceId,
    ),
  ]);

  return c.json({ status: "ok", agent_id: agentId });
});

/**
 * GET /v1/convai/agents
 * List agents with pagination, search, sort, and archive filter.
 */
app.get("/v1/convai/agents", async (c) => {
  const workspaceId = c.get("workspace_id");
  const page = Math.max(1, parseInt(c.req.query("page") ?? "1", 10));
  const pageSize = Math.min(100, Math.max(1, parseInt(c.req.query("page_size") ?? "20", 10)));
  const search = c.req.query("search") ?? "";
  const sort = c.req.query("sort") ?? "created_at";
  const sortDir = c.req.query("sort_direction") === "asc" ? "ASC" : "DESC";
  const includeArchived = c.req.query("include_archived") === "true";

  const allowedSorts = ["created_at", "updated_at", "name"];
  const safeSort = allowedSorts.includes(sort) ? sort : "created_at";

  let whereClause = "workspace_id = ?";
  const params: (string | number)[] = [workspaceId];

  if (!includeArchived) {
    whereClause += " AND is_archived = 0";
  }

  if (search) {
    whereClause += " AND name LIKE ?";
    params.push(`%${search}%`);
  }

  const offset = (page - 1) * pageSize;

  const countResult = await c.env.DB.prepare(
    `SELECT COUNT(*) as total FROM agents WHERE ${whereClause}`,
  )
    .bind(...params)
    .first<{ total: number }>();

  const total = countResult?.total ?? 0;

  const rows = await c.env.DB.prepare(
    `SELECT * FROM agents WHERE ${whereClause} ORDER BY ${safeSort} ${sortDir} LIMIT ? OFFSET ?`,
  )
    .bind(...params, pageSize, offset)
    .all<AgentRow>();

  const agents = rows.results.map(rowToAgent);

  return c.json({
    agents,
    page,
    page_size: pageSize,
    total,
    has_more: offset + pageSize < total,
  });
});

// ---------------------------------------------------------------------------
// CONVERSATIONS
// ---------------------------------------------------------------------------

/**
 * GET /v1/convai/conversations
 * List conversations with extensive filtering (ElevenLabs-compatible params).
 */
app.get("/v1/convai/conversations", async (c) => {
  const workspaceId = c.get("workspace_id");
  const page = Math.max(1, parseInt(c.req.query("page") ?? "1", 10));
  const pageSize = Math.min(100, Math.max(1, parseInt(c.req.query("page_size") ?? "20", 10)));
  const agentId = c.req.query("agent_id");
  const status = c.req.query("status");
  const userId = c.req.query("user_id");
  const callSuccessful = c.req.query("call_successful");
  const callStartAfter = c.req.query("call_start_after_unix");
  const callStartBefore = c.req.query("call_start_before_unix");
  const callDurationMin = c.req.query("call_duration_min_secs");
  const callDurationMax = c.req.query("call_duration_max_secs");
  const ratingMin = c.req.query("rating_min");
  const ratingMax = c.req.query("rating_max");
  const search = c.req.query("search");

  let whereClause = "c.workspace_id = ?";
  const params: (string | number)[] = [workspaceId];

  if (agentId) {
    whereClause += " AND c.agent_id = ?";
    params.push(agentId);
  }
  if (status) {
    whereClause += " AND c.status = ?";
    params.push(status);
  }
  if (userId) {
    whereClause += " AND c.user_id = ?";
    params.push(userId);
  }
  if (callSuccessful !== undefined && callSuccessful !== null) {
    whereClause += " AND c.call_successful = ?";
    params.push(callSuccessful === "true" ? 1 : 0);
  }
  if (callStartAfter) {
    const afterDate = new Date(parseInt(callStartAfter, 10) * 1000).toISOString();
    whereClause += " AND c.start_time >= ?";
    params.push(afterDate);
  }
  if (callStartBefore) {
    const beforeDate = new Date(parseInt(callStartBefore, 10) * 1000).toISOString();
    whereClause += " AND c.start_time <= ?";
    params.push(beforeDate);
  }
  if (callDurationMin) {
    whereClause += " AND c.duration_secs >= ?";
    params.push(parseFloat(callDurationMin));
  }
  if (callDurationMax) {
    whereClause += " AND c.duration_secs <= ?";
    params.push(parseFloat(callDurationMax));
  }
  if (ratingMin) {
    whereClause += " AND c.rating >= ?";
    params.push(parseFloat(ratingMin));
  }
  if (ratingMax) {
    whereClause += " AND c.rating <= ?";
    params.push(parseFloat(ratingMax));
  }
  if (search) {
    whereClause += " AND c.conversation_id IN (SELECT conversation_id FROM messages WHERE content LIKE ?)";
    params.push(`%${search}%`);
  }

  const offset = (page - 1) * pageSize;

  const countResult = await c.env.DB.prepare(
    `SELECT COUNT(*) as total FROM conversations c WHERE ${whereClause}`,
  )
    .bind(...params)
    .first<{ total: number }>();

  const total = countResult?.total ?? 0;

  const rows = await c.env.DB.prepare(
    `SELECT c.* FROM conversations c WHERE ${whereClause} ORDER BY c.start_time DESC LIMIT ? OFFSET ?`,
  )
    .bind(...params, pageSize, offset)
    .all<ConversationRow>();

  const conversations = rows.results.map(rowToConversation);

  return c.json({
    conversations,
    page,
    page_size: pageSize,
    total,
    has_more: offset + pageSize < total,
  });
});

/**
 * GET /v1/convai/conversations/:conversation_id
 * Get a single conversation with its full message transcript.
 */
app.get("/v1/convai/conversations/:conversation_id", async (c) => {
  const conversationId = c.req.param("conversation_id");
  const workspaceId = c.get("workspace_id");

  const convRow = await c.env.DB.prepare(
    "SELECT * FROM conversations WHERE conversation_id = ? AND workspace_id = ?",
  )
    .bind(conversationId, workspaceId)
    .first<ConversationRow>();

  if (!convRow) {
    return errorResponse(404, "Conversation not found", `No conversation with id ${conversationId}`);
  }

  const msgRows = await c.env.DB.prepare(
    "SELECT * FROM messages WHERE conversation_id = ? ORDER BY timestamp ASC",
  )
    .bind(conversationId)
    .all<MessageRow>();

  const conversation: ConversationDetailResponse = {
    ...rowToConversation(convRow),
    messages: msgRows.results.map(rowToMessage),
  };

  return c.json(conversation);
});

/**
 * DELETE /v1/convai/conversations/:conversation_id
 * Delete a conversation and its messages.
 */
app.delete("/v1/convai/conversations/:conversation_id", async (c) => {
  const conversationId = c.req.param("conversation_id");
  const workspaceId = c.get("workspace_id");

  const existing = await c.env.DB.prepare(
    "SELECT conversation_id FROM conversations WHERE conversation_id = ? AND workspace_id = ?",
  )
    .bind(conversationId, workspaceId)
    .first<{ conversation_id: string }>();

  if (!existing) {
    return errorResponse(404, "Conversation not found");
  }

  await c.env.DB.batch([
    c.env.DB.prepare("DELETE FROM messages WHERE conversation_id = ?").bind(conversationId),
    c.env.DB.prepare("DELETE FROM conversations WHERE conversation_id = ?").bind(conversationId),
  ]);

  return c.json({ status: "ok", conversation_id: conversationId });
});

// ---------------------------------------------------------------------------
// AUTH — Signed WebSocket URL
// ---------------------------------------------------------------------------

/**
 * POST /v1/convai/conversation/get-signed-url
 * Generate a JWT-signed WebSocket URL for ConvAI connections.
 * ElevenLabs uses GET but we also support POST for flexibility.
 */
app.post("/v1/convai/conversation/get-signed-url", async (c) => {
  const workspaceId = c.get("workspace_id");
  const body = await c.req.json<{
    agent_id: string;
    conversation_id?: string;
    environment?: string;
  }>();

  if (!body.agent_id) {
    return errorResponse(400, "agent_id is required");
  }

  // Verify agent exists and belongs to workspace
  const agent = await c.env.DB.prepare(
    "SELECT agent_id FROM agents WHERE agent_id = ? AND workspace_id = ?",
  )
    .bind(body.agent_id, workspaceId)
    .first<{ agent_id: string }>();

  if (!agent) {
    return errorResponse(404, "Agent not found", `No agent with id ${body.agent_id}`);
  }

  const tokenId = generateId("tok");
  const expiresAt = new Date(Date.now() + 5 * 60 * 1000); // 5 minute expiry

  // Store token record for tracking
  await c.env.DB.prepare(
    "INSERT INTO signed_url_tokens (token_id, agent_id, workspace_id, expires_at) VALUES (?, ?, ?, ?)",
  )
    .bind(tokenId, body.agent_id, workspaceId, expiresAt.toISOString())
    .run();

  // Generate JWT
  const secret = getJwtSecret(c.env);
  const jwt = await new jose.SignJWT({
    agent_id: body.agent_id,
    workspace_id: workspaceId,
    token_id: tokenId,
    ...(body.conversation_id ? { conversation_id: body.conversation_id } : {}),
    ...(body.environment ? { environment: body.environment } : {}),
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuer(c.env.JWT_ISSUER)
    .setAudience(c.env.JWT_AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(expiresAt)
    .setJti(tokenId)
    .sign(secret);

  // Build signed URL — the WebSocket server will validate this JWT
  const baseUrl = new URL(c.req.url);
  const wsScheme = baseUrl.protocol === "https:" ? "wss:" : "ws:";
  const signedUrl = `${wsScheme}//${baseUrl.host}/v1/convai/conversation?agent_id=${body.agent_id}&token=${jwt}`;

  return c.json({
    signed_url: signedUrl,
    token: jwt,
    expires_at: expiresAt.toISOString(),
  });
});

/**
 * GET /v1/convai/conversation/get-signed-url
 * ElevenLabs uses GET with query params. Support both.
 */
app.get("/v1/convai/conversation/get-signed-url", async (c) => {
  const workspaceId = c.get("workspace_id");
  const agentId = c.req.query("agent_id");
  const environment = c.req.query("environment");

  if (!agentId) {
    return errorResponse(400, "agent_id query parameter is required");
  }

  const agent = await c.env.DB.prepare(
    "SELECT agent_id FROM agents WHERE agent_id = ? AND workspace_id = ?",
  )
    .bind(agentId, workspaceId)
    .first<{ agent_id: string }>();

  if (!agent) {
    return errorResponse(404, "Agent not found", `No agent with id ${agentId}`);
  }

  const tokenId = generateId("tok");
  const expiresAt = new Date(Date.now() + 5 * 60 * 1000);

  await c.env.DB.prepare(
    "INSERT INTO signed_url_tokens (token_id, agent_id, workspace_id, expires_at) VALUES (?, ?, ?, ?)",
  )
    .bind(tokenId, agentId, workspaceId, expiresAt.toISOString())
    .run();

  const secret = getJwtSecret(c.env);
  const jwt = await new jose.SignJWT({
    agent_id: agentId,
    workspace_id: workspaceId,
    token_id: tokenId,
    ...(environment ? { environment } : {}),
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuer(c.env.JWT_ISSUER)
    .setAudience(c.env.JWT_AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(expiresAt)
    .setJti(tokenId)
    .sign(secret);

  const baseUrl = new URL(c.req.url);
  const wsScheme = baseUrl.protocol === "https:" ? "wss:" : "ws:";
  const signedUrl = `${wsScheme}//${baseUrl.host}/v1/convai/conversation?agent_id=${agentId}&token=${jwt}`;

  return c.json({
    signed_url: signedUrl,
    token: jwt,
    expires_at: expiresAt.toISOString(),
  });
});

/**
 * POST /v1/convai/conversation/verify-token
 * Utility endpoint to verify a JWT token's validity.
 */
app.post("/v1/convai/conversation/verify-token", async (c) => {
  const body = await c.req.json<{ token: string }>();

  if (!body.token) {
    return errorResponse(400, "token is required");
  }

  try {
    const secret = getJwtSecret(c.env);
    const { payload } = await jose.jwtVerify(body.token, secret, {
      issuer: c.env.JWT_ISSUER,
      audience: c.env.JWT_AUDIENCE,
    });

    return c.json({
      valid: true,
      agent_id: payload.agent_id,
      workspace_id: payload.workspace_id,
      expires_at: payload.exp ? new Date(payload.exp * 1000).toISOString() : null,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Token verification failed";
    return c.json({ valid: false, error: message }, 401);
  }
});

// ---------------------------------------------------------------------------
// TTS — Proxy to GPU backend
// ---------------------------------------------------------------------------

/**
 * POST /v1/text-to-speech/:voice_id
 * Standard (non-streaming) TTS. Proxies to the GPU TTS backend.
 */
app.post("/v1/text-to-speech/:voice_id", async (c) => {
  const voiceId = c.req.param("voice_id");
  const outputFormat = c.req.query("output_format") ?? "mp3_44100_128";
  const body = await c.req.json<{
    text: string;
    model_id?: string;
    voice_settings?: Record<string, unknown>;
    language_code?: string;
    seed?: number;
    previous_text?: string;
    next_text?: string;
    apply_text_normalization?: string;
  }>();

  if (!body.text) {
    return errorResponse(400, "text is required");
  }

  const backendUrl = `${c.env.TTS_BACKEND_URL}/v1/text-to-speech/${voiceId}?output_format=${outputFormat}`;

  try {
    const backendResponse = await fetch(backendUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "xi-api-key": c.req.header("xi-api-key") ?? "",
      },
      body: JSON.stringify({
        text: body.text,
        model_id: body.model_id ?? "eleven_multilingual_v2",
        voice_settings: body.voice_settings ?? {
          stability: 0.5,
          similarity_boost: 0.75,
          use_speaker_boost: true,
          style: 0.0,
          speed: 1.0,
        },
        language_code: body.language_code,
        seed: body.seed,
        previous_text: body.previous_text,
        next_text: body.next_text,
        apply_text_normalization: body.apply_text_normalization ?? "auto",
      }),
    });

    if (!backendResponse.ok) {
      const errorText = await backendResponse.text();
      return errorResponse(
        backendResponse.status,
        "TTS backend error",
        errorText,
      );
    }

    const audioData = await backendResponse.arrayBuffer();
    const contentType = getContentTypeForFormat(outputFormat);

    // Store audio in R2 for history
    const audioKey = `tts/${voiceId}/${Date.now()}.${getExtensionForFormat(outputFormat)}`;
    await c.env.STORAGE.put(audioKey, audioData, {
      httpMetadata: { contentType },
      customMetadata: {
        voice_id: voiceId,
        model_id: body.model_id ?? "eleven_multilingual_v2",
        text_length: String(body.text.length),
        output_format: outputFormat,
      },
    });

    return new Response(audioData, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Content-Length": String(audioData.byteLength),
        "X-Request-Id": generateId("req"),
        "X-Audio-R2-Key": audioKey,
      },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "TTS request failed";
    return errorResponse(502, "TTS backend unavailable", message);
  }
});

/**
 * POST /v1/text-to-speech/:voice_id/stream
 * Streaming TTS. Proxies the stream from the GPU backend.
 */
app.post("/v1/text-to-speech/:voice_id/stream", async (c) => {
  const voiceId = c.req.param("voice_id");
  const outputFormat = c.req.query("output_format") ?? "mp3_44100_128";
  const body = await c.req.json<{
    text: string;
    model_id?: string;
    voice_settings?: Record<string, unknown>;
    language_code?: string;
    seed?: number;
    previous_text?: string;
    next_text?: string;
    apply_text_normalization?: string;
  }>();

  if (!body.text) {
    return errorResponse(400, "text is required");
  }

  const backendUrl = `${c.env.TTS_BACKEND_URL}/v1/text-to-speech/${voiceId}/stream?output_format=${outputFormat}`;

  try {
    const backendResponse = await fetch(backendUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "xi-api-key": c.req.header("xi-api-key") ?? "",
      },
      body: JSON.stringify({
        text: body.text,
        model_id: body.model_id ?? "eleven_multilingual_v2",
        voice_settings: body.voice_settings ?? {
          stability: 0.5,
          similarity_boost: 0.75,
          use_speaker_boost: true,
          style: 0.0,
          speed: 1.0,
        },
        language_code: body.language_code,
        seed: body.seed,
        previous_text: body.previous_text,
        next_text: body.next_text,
        apply_text_normalization: body.apply_text_normalization ?? "auto",
      }),
    });

    if (!backendResponse.ok) {
      const errorText = await backendResponse.text();
      return errorResponse(backendResponse.status, "TTS backend error", errorText);
    }

    if (!backendResponse.body) {
      return errorResponse(502, "TTS backend returned no stream body");
    }

    const contentType = getContentTypeForFormat(outputFormat);

    return new Response(backendResponse.body, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Transfer-Encoding": "chunked",
        "X-Request-Id": generateId("req"),
      },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "TTS stream request failed";
    return errorResponse(502, "TTS backend unavailable", message);
  }
});

// ---------------------------------------------------------------------------
// KNOWLEDGE BASE
// ---------------------------------------------------------------------------

/**
 * POST /v1/convai/agents/:agent_id/knowledge
 * Upload a document to the agent's knowledge base (stored in R2).
 */
app.post("/v1/convai/agents/:agent_id/knowledge", async (c) => {
  const agentId = c.req.param("agent_id");
  const workspaceId = c.get("workspace_id");

  // Verify agent exists
  const agent = await c.env.DB.prepare(
    "SELECT agent_id FROM agents WHERE agent_id = ? AND workspace_id = ?",
  )
    .bind(agentId, workspaceId)
    .first<{ agent_id: string }>();

  if (!agent) {
    return errorResponse(404, "Agent not found");
  }

  const contentType = c.req.header("content-type") ?? "";

  // Handle multipart/form-data upload
  if (contentType.includes("multipart/form-data")) {
    const formData = await c.req.formData();
    const file = formData.get("file");

    if (!file || !(file instanceof File)) {
      return errorResponse(400, "file field is required in multipart upload");
    }

    const documentId = generateId("doc");
    const r2Key = `knowledge/${agentId}/${documentId}/${file.name}`;
    const fileBuffer = await file.arrayBuffer();

    // Upload to R2
    await c.env.STORAGE.put(r2Key, fileBuffer, {
      httpMetadata: { contentType: file.type || "application/octet-stream" },
      customMetadata: {
        agent_id: agentId,
        document_id: documentId,
        original_name: file.name,
      },
    });

    const now = new Date().toISOString();

    // Record in D1
    await c.env.DB.prepare(
      `INSERT INTO knowledge_documents
         (document_id, agent_id, workspace_id, name, mime_type, size_bytes, r2_key, status, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, 'uploaded', ?, ?)`,
    )
      .bind(
        documentId,
        agentId,
        workspaceId,
        file.name,
        file.type || "application/octet-stream",
        fileBuffer.byteLength,
        r2Key,
        now,
        now,
      )
      .run();

    return c.json(
      {
        document_id: documentId,
        agent_id: agentId,
        name: file.name,
        mime_type: file.type || "application/octet-stream",
        size_bytes: fileBuffer.byteLength,
        status: "uploaded",
        created_at: now,
      },
      201,
    );
  }

  // Handle JSON body upload (base64 encoded content or URL reference)
  const body = await c.req.json<{
    name: string;
    content?: string; // base64 encoded
    url?: string;
    mime_type?: string;
    metadata?: Record<string, unknown>;
  }>();

  if (!body.name) {
    return errorResponse(400, "name is required");
  }

  const documentId = generateId("doc");
  const r2Key = `knowledge/${agentId}/${documentId}/${body.name}`;
  const mimeType = body.mime_type ?? "application/octet-stream";
  let sizeBytes = 0;

  if (body.content) {
    // Decode base64 and upload
    const binaryString = atob(body.content);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
      bytes[i] = binaryString.charCodeAt(i);
    }
    sizeBytes = bytes.byteLength;

    await c.env.STORAGE.put(r2Key, bytes, {
      httpMetadata: { contentType: mimeType },
      customMetadata: { agent_id: agentId, document_id: documentId },
    });
  } else if (body.url) {
    // Fetch from URL and store in R2
    const fetchResponse = await fetch(body.url);
    if (!fetchResponse.ok) {
      return errorResponse(400, "Failed to fetch document from URL");
    }
    const data = await fetchResponse.arrayBuffer();
    sizeBytes = data.byteLength;

    await c.env.STORAGE.put(r2Key, data, {
      httpMetadata: { contentType: mimeType },
      customMetadata: { agent_id: agentId, document_id: documentId },
    });
  } else {
    return errorResponse(400, "Either content (base64) or url must be provided");
  }

  const now = new Date().toISOString();
  const docMetadata = body.metadata ?? {};

  await c.env.DB.prepare(
    `INSERT INTO knowledge_documents
       (document_id, agent_id, workspace_id, name, mime_type, size_bytes, r2_key, status, metadata, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, 'uploaded', ?, ?, ?)`,
  )
    .bind(
      documentId,
      agentId,
      workspaceId,
      body.name,
      mimeType,
      sizeBytes,
      r2Key,
      JSON.stringify(docMetadata),
      now,
      now,
    )
    .run();

  return c.json(
    {
      document_id: documentId,
      agent_id: agentId,
      name: body.name,
      mime_type: mimeType,
      size_bytes: sizeBytes,
      status: "uploaded",
      metadata: docMetadata,
      created_at: now,
    },
    201,
  );
});

/**
 * GET /v1/convai/agents/:agent_id/knowledge
 * List knowledge base documents for an agent.
 */
app.get("/v1/convai/agents/:agent_id/knowledge", async (c) => {
  const agentId = c.req.param("agent_id");
  const workspaceId = c.get("workspace_id");

  // Verify agent exists
  const agent = await c.env.DB.prepare(
    "SELECT agent_id FROM agents WHERE agent_id = ? AND workspace_id = ?",
  )
    .bind(agentId, workspaceId)
    .first<{ agent_id: string }>();

  if (!agent) {
    return errorResponse(404, "Agent not found");
  }

  const page = Math.max(1, parseInt(c.req.query("page") ?? "1", 10));
  const pageSize = Math.min(100, Math.max(1, parseInt(c.req.query("page_size") ?? "20", 10)));
  const offset = (page - 1) * pageSize;

  const countResult = await c.env.DB.prepare(
    "SELECT COUNT(*) as total FROM knowledge_documents WHERE agent_id = ? AND workspace_id = ?",
  )
    .bind(agentId, workspaceId)
    .first<{ total: number }>();

  const total = countResult?.total ?? 0;

  const rows = await c.env.DB.prepare(
    `SELECT * FROM knowledge_documents
     WHERE agent_id = ? AND workspace_id = ?
     ORDER BY created_at DESC LIMIT ? OFFSET ?`,
  )
    .bind(agentId, workspaceId, pageSize, offset)
    .all<KnowledgeDocRow>();

  const documents = rows.results.map((row) => ({
    document_id: row.document_id,
    agent_id: row.agent_id,
    name: row.name,
    mime_type: row.mime_type,
    size_bytes: row.size_bytes,
    status: row.status,
    metadata: safeParse(row.metadata, {}),
    created_at: row.created_at,
    updated_at: row.updated_at,
  }));

  return c.json({
    documents,
    page,
    page_size: pageSize,
    total,
    has_more: offset + pageSize < total,
  });
});

/**
 * DELETE /v1/convai/agents/:agent_id/knowledge/:document_id
 * Remove a knowledge base document.
 */
app.delete("/v1/convai/agents/:agent_id/knowledge/:document_id", async (c) => {
  const agentId = c.req.param("agent_id");
  const documentId = c.req.param("document_id");
  const workspaceId = c.get("workspace_id");

  const doc = await c.env.DB.prepare(
    "SELECT r2_key FROM knowledge_documents WHERE document_id = ? AND agent_id = ? AND workspace_id = ?",
  )
    .bind(documentId, agentId, workspaceId)
    .first<{ r2_key: string }>();

  if (!doc) {
    return errorResponse(404, "Document not found");
  }

  // Delete from R2 and D1
  await Promise.all([
    c.env.STORAGE.delete(doc.r2_key),
    c.env.DB.prepare("DELETE FROM knowledge_documents WHERE document_id = ?")
      .bind(documentId)
      .run(),
  ]);

  return c.json({ status: "ok", document_id: documentId });
});

// ---------------------------------------------------------------------------
// INTERNAL — Conversation creation (called by WebSocket handler or tests)
// ---------------------------------------------------------------------------

/**
 * POST /v1/convai/conversations/create
 * Create a conversation record (for internal use or testing).
 */
app.post("/v1/convai/conversations/create", async (c) => {
  const workspaceId = c.get("workspace_id");
  const body = await c.req.json<{
    agent_id: string;
    user_id?: string;
    metadata?: Record<string, unknown>;
  }>();

  if (!body.agent_id) {
    return errorResponse(400, "agent_id is required");
  }

  // Verify agent
  const agent = await c.env.DB.prepare(
    "SELECT agent_id FROM agents WHERE agent_id = ? AND workspace_id = ?",
  )
    .bind(body.agent_id, workspaceId)
    .first<{ agent_id: string }>();

  if (!agent) {
    return errorResponse(404, "Agent not found");
  }

  const conversationId = generateId("conv");
  const now = new Date().toISOString();

  await c.env.DB.prepare(
    `INSERT INTO conversations (conversation_id, agent_id, workspace_id, status, start_time, user_id, metadata)
     VALUES (?, ?, ?, 'active', ?, ?, ?)`,
  )
    .bind(
      conversationId,
      body.agent_id,
      workspaceId,
      now,
      body.user_id ?? null,
      JSON.stringify(body.metadata ?? {}),
    )
    .run();

  return c.json(
    {
      conversation_id: conversationId,
      agent_id: body.agent_id,
      status: "active",
      start_time: now,
      user_id: body.user_id ?? null,
    },
    201,
  );
});

/**
 * POST /v1/convai/conversations/:conversation_id/messages
 * Add a message to a conversation (for internal use or testing).
 */
app.post("/v1/convai/conversations/:conversation_id/messages", async (c) => {
  const conversationId = c.req.param("conversation_id");
  const workspaceId = c.get("workspace_id");

  const conv = await c.env.DB.prepare(
    "SELECT conversation_id FROM conversations WHERE conversation_id = ? AND workspace_id = ?",
  )
    .bind(conversationId, workspaceId)
    .first<{ conversation_id: string }>();

  if (!conv) {
    return errorResponse(404, "Conversation not found");
  }

  const body = await c.req.json<{
    role: string;
    content: string;
    metadata?: Record<string, unknown>;
  }>();

  if (!body.role || !body.content) {
    return errorResponse(400, "role and content are required");
  }

  const validRoles = ["user", "agent", "system", "tool_call", "tool_result"];
  if (!validRoles.includes(body.role)) {
    return errorResponse(400, `role must be one of: ${validRoles.join(", ")}`);
  }

  const messageId = generateId("msg");
  const now = new Date().toISOString();

  await c.env.DB.prepare(
    `INSERT INTO messages (message_id, conversation_id, role, content, timestamp, metadata)
     VALUES (?, ?, ?, ?, ?, ?)`,
  )
    .bind(messageId, conversationId, body.role, body.content, now, JSON.stringify(body.metadata ?? {}))
    .run();

  return c.json(
    {
      message_id: messageId,
      conversation_id: conversationId,
      role: body.role,
      content: body.content,
      timestamp: now,
    },
    201,
  );
});

/**
 * POST /v1/convai/conversations/:conversation_id/feedback
 * Submit feedback for a conversation (no auth required per ElevenLabs spec).
 */
app.post("/v1/convai/conversations/:conversation_id/feedback", async (c) => {
  const conversationId = c.req.param("conversation_id");

  const body = await c.req.json<{
    rating?: number;
    comment?: string;
  }>();

  const updates: string[] = [];
  const params: (string | number)[] = [];

  if (body.rating !== undefined) {
    updates.push("rating = ?");
    params.push(body.rating);
  }
  if (body.comment !== undefined) {
    updates.push("feedback_comment = ?");
    params.push(body.comment);
  }

  if (updates.length === 0) {
    return errorResponse(400, "At least rating or comment is required");
  }

  params.push(conversationId);

  const result = await c.env.DB.prepare(
    `UPDATE conversations SET ${updates.join(", ")} WHERE conversation_id = ?`,
  )
    .bind(...params)
    .run();

  if (!result.meta.changes || result.meta.changes === 0) {
    return errorResponse(404, "Conversation not found");
  }

  return c.json({ status: "ok", conversation_id: conversationId });
});

// ---------------------------------------------------------------------------
// ADMIN — API key management
// ---------------------------------------------------------------------------

/**
 * POST /internal/api-keys
 * Create a new API key. Requires MASTER_API_KEY.
 */
app.post("/internal/api-keys", async (c) => {
  const authKey = c.req.header("xi-api-key");
  if (!c.env.MASTER_API_KEY || authKey !== c.env.MASTER_API_KEY) {
    return errorResponse(401, "Master API key required for key management");
  }

  const body = await c.req.json<{
    workspace_id?: string;
    name?: string;
  }>();

  const workspaceId = body.workspace_id ?? "default";
  const name = body.name ?? "api-key";

  // Generate a new API key
  const rawKey = `xi_${generateId("key")}`;
  const hash = await hashApiKey(rawKey);
  const keyId = generateId("ak");

  await c.env.DB.prepare(
    "INSERT INTO api_keys (id, key_hash, workspace_id, name) VALUES (?, ?, ?, ?)",
  )
    .bind(keyId, hash, workspaceId, name)
    .run();

  return c.json(
    {
      id: keyId,
      api_key: rawKey, // Only returned once at creation time
      workspace_id: workspaceId,
      name,
      message: "Store this API key securely. It will not be shown again.",
    },
    201,
  );
});

/**
 * GET /internal/api-keys
 * List API keys (hashed, no raw keys). Requires MASTER_API_KEY.
 */
app.get("/internal/api-keys", async (c) => {
  const authKey = c.req.header("xi-api-key");
  if (!c.env.MASTER_API_KEY || authKey !== c.env.MASTER_API_KEY) {
    return errorResponse(401, "Master API key required");
  }

  const rows = await c.env.DB.prepare(
    "SELECT id, workspace_id, name, created_at, last_used_at, is_active FROM api_keys ORDER BY created_at DESC",
  ).all<Omit<ApiKeyRow, "key_hash" | "permissions">>();

  return c.json({ api_keys: rows.results });
});

/**
 * DELETE /internal/api-keys/:id
 * Revoke an API key. Requires MASTER_API_KEY.
 */
app.delete("/internal/api-keys/:id", async (c) => {
  const authKey = c.req.header("xi-api-key");
  if (!c.env.MASTER_API_KEY || authKey !== c.env.MASTER_API_KEY) {
    return errorResponse(401, "Master API key required");
  }

  const keyId = c.req.param("id");
  await c.env.DB.prepare("UPDATE api_keys SET is_active = 0 WHERE id = ?").bind(keyId).run();

  return c.json({ status: "ok", id: keyId });
});

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

/**
 * Build default conversation_config matching ElevenLabs schema.
 */
function buildDefaultConversationConfig(): Record<string, unknown> {
  return {
    asr: {
      quality: "high",
      provider: "elevenlabs",
      user_input_audio_format: "pcm_16000",
      keywords: [],
    },
    turn: {
      turn_timeout: 7.0,
      silence_end_call_timeout: -1,
      mode: "turn",
      turn_eagerness: "normal",
      spelling_patience: "auto",
    },
    tts: {
      model_id: "eleven_flash_v2",
      voice_id: "cjVigY5qzO86Huf0OWal",
      expressive_mode: true,
      agent_output_audio_format: "pcm_16000",
      optimize_streaming_latency: 3,
      stability: 0.5,
      speed: 1.0,
      similarity_boost: 0.75,
    },
    conversation: {
      text_only: false,
      max_duration_seconds: 600,
      client_events: [],
    },
    vad: {
      background_voice_detection: false,
    },
    agent: {
      first_message: "",
      language: "en",
      prompt: {
        prompt: "",
        llm: "gemini-2.5-flash",
        temperature: 0.0,
        max_tokens: -1,
        tool_ids: [],
        knowledge_base: [],
      },
    },
  };
}

/**
 * Deep merge two objects. Source values override target values.
 * Arrays are replaced, not merged.
 */
function deepMerge(
  target: Record<string, unknown>,
  source: Record<string, unknown>,
): Record<string, unknown> {
  const result = { ...target };

  for (const key of Object.keys(source)) {
    const sourceVal = source[key];
    const targetVal = result[key];

    if (
      sourceVal !== null &&
      typeof sourceVal === "object" &&
      !Array.isArray(sourceVal) &&
      targetVal !== null &&
      typeof targetVal === "object" &&
      !Array.isArray(targetVal)
    ) {
      result[key] = deepMerge(
        targetVal as Record<string, unknown>,
        sourceVal as Record<string, unknown>,
      );
    } else {
      result[key] = sourceVal;
    }
  }

  return result;
}

/**
 * Map output_format string to Content-Type header.
 */
function getContentTypeForFormat(format: string): string {
  if (format.startsWith("mp3")) return "audio/mpeg";
  if (format.startsWith("pcm")) return "audio/pcm";
  if (format.startsWith("ulaw")) return "audio/basic";
  if (format.startsWith("opus")) return "audio/opus";
  if (format.startsWith("aac")) return "audio/aac";
  if (format.startsWith("flac")) return "audio/flac";
  return "audio/mpeg";
}

/**
 * Map output_format to file extension.
 */
function getExtensionForFormat(format: string): string {
  if (format.startsWith("mp3")) return "mp3";
  if (format.startsWith("pcm")) return "pcm";
  if (format.startsWith("ulaw")) return "ulaw";
  if (format.startsWith("opus")) return "opus";
  if (format.startsWith("aac")) return "aac";
  if (format.startsWith("flac")) return "flac";
  return "mp3";
}

// ---------------------------------------------------------------------------
// 404 catch-all
// ---------------------------------------------------------------------------

app.notFound((c) =>
  c.json(
    {
      detail: {
        status: "not_found",
        message: `Route ${c.req.method} ${c.req.path} not found`,
        available_endpoints: [
          "GET  /health",
          "POST /v1/convai/agents/create",
          "GET  /v1/convai/agents",
          "GET  /v1/convai/agents/:id",
          "PATCH /v1/convai/agents/:id",
          "DELETE /v1/convai/agents/:id",
          "GET  /v1/convai/conversations",
          "GET  /v1/convai/conversations/:id",
          "DELETE /v1/convai/conversations/:id",
          "POST /v1/convai/conversation/get-signed-url",
          "GET  /v1/convai/conversation/get-signed-url",
          "POST /v1/text-to-speech/:voice_id",
          "POST /v1/text-to-speech/:voice_id/stream",
          "POST /v1/convai/agents/:id/knowledge",
          "GET  /v1/convai/agents/:id/knowledge",
          "DELETE /v1/convai/agents/:id/knowledge/:doc_id",
          "POST /v1/convai/conversations/create",
          "POST /v1/convai/conversations/:id/messages",
          "POST /v1/convai/conversations/:id/feedback",
          "POST /v1/convai/conversation/verify-token",
          "POST /internal/api-keys",
          "GET  /internal/api-keys",
          "DELETE /internal/api-keys/:id",
        ],
      },
    },
    404,
  ),
);

// ---------------------------------------------------------------------------
// Global error handler
// ---------------------------------------------------------------------------

app.onError((err, c) => {
  console.error("Unhandled error:", err.message, err.stack);
  return c.json(
    {
      detail: {
        status: "internal_error",
        message: err.message || "An unexpected error occurred",
      },
    },
    500,
  );
});

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

export default app;
