// ============================================================================
// Echo ConvAI Gateway — Worker Entry Point
// ElevenLabs-compatible WebSocket gateway with Durable Object sessions
// ============================================================================

import { ConversationSession } from "./conversation-session";
import type { Env } from "./types";
import { WS_SUBPROTOCOL, WS_CLOSE_POLICY_VIOLATION } from "./types";

export { ConversationSession };

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    // Health check
    if (url.pathname === "/health" || url.pathname === "/") {
      return Response.json({
        status: "healthy",
        service: "echo-convai-gateway",
        version: "1.0.0",
        timestamp: new Date().toISOString(),
      });
    }

    // Signed URL endpoint — returns a pre-authenticated WebSocket URL
    if (url.pathname === "/v1/convai/conversation/get-signed-url" && request.method === "GET") {
      return handleGetSignedUrl(request, url, env);
    }

    // WebSocket conversation endpoint
    if (url.pathname === "/v1/convai/conversation") {
      return handleConversationUpgrade(request, url, env);
    }

    // Conversation token endpoint (for WebRTC clients)
    if (url.pathname === "/v1/convai/conversation/token" && request.method === "GET") {
      return handleGetConversationToken(request, url, env);
    }

    // Post-session feedback
    const feedbackMatch = url.pathname.match(/^\/v1\/convai\/conversations\/([^/]+)\/feedback$/);
    if (feedbackMatch && request.method === "POST") {
      return handlePostFeedback(request, feedbackMatch[1], env);
    }

    return Response.json({ error: "Not found" }, { status: 404 });
  },
};

// ---------------------------------------------------------------------------
// Signed URL generation
// ---------------------------------------------------------------------------

async function handleGetSignedUrl(request: Request, url: URL, env: Env): Promise<Response> {
  const agentId = url.searchParams.get("agent_id");
  if (!agentId) {
    return Response.json({ error: "agent_id is required" }, { status: 400 });
  }

  // Validate API key
  const apiKey = request.headers.get("xi-api-key");
  if (!apiKey) {
    return Response.json({ error: "xi-api-key header required" }, { status: 401 });
  }
  if (env.ELEVENLABS_API_KEY && apiKey !== env.ELEVENLABS_API_KEY) {
    return Response.json({ error: "Invalid API key" }, { status: 401 });
  }

  // Verify agent exists
  const agent = await lookupAgent(env.DB, agentId);
  if (!agent) {
    return Response.json({ error: "Agent not found" }, { status: 404 });
  }

  // Generate signed token
  const token = await generateSignedToken(agentId, env);
  const baseUrl = url.origin.replace("http://", "wss://").replace("https://", "wss://");
  const signedUrl = `${baseUrl}/v1/convai/conversation?agent_id=${agentId}&token=${token}`;

  return Response.json({ signed_url: signedUrl });
}

// ---------------------------------------------------------------------------
// Conversation token (for WebRTC path compatibility)
// ---------------------------------------------------------------------------

async function handleGetConversationToken(request: Request, url: URL, env: Env): Promise<Response> {
  const agentId = url.searchParams.get("agent_id");
  if (!agentId) {
    return Response.json({ error: "agent_id is required" }, { status: 400 });
  }

  const agent = await lookupAgent(env.DB, agentId);
  if (!agent) {
    return Response.json({ error: "Agent not found" }, { status: 404 });
  }

  const token = await generateSignedToken(agentId, env);
  return Response.json({ token });
}

// ---------------------------------------------------------------------------
// WebSocket conversation upgrade
// ---------------------------------------------------------------------------

async function handleConversationUpgrade(request: Request, url: URL, env: Env): Promise<Response> {
  // Must be a WebSocket upgrade
  const upgradeHeader = request.headers.get("Upgrade");
  if (!upgradeHeader || upgradeHeader.toLowerCase() !== "websocket") {
    return Response.json(
      { error: "Expected WebSocket upgrade" },
      { status: 426, headers: { "Upgrade": "websocket" } }
    );
  }

  const agentId = url.searchParams.get("agent_id");
  if (!agentId) {
    return Response.json({ error: "agent_id query parameter is required" }, { status: 400 });
  }

  // Authenticate: API key header, bearer subprotocol, or signed URL token
  const authenticated = await authenticateRequest(request, url, agentId, env);
  if (!authenticated.ok) {
    return Response.json({ error: authenticated.error }, { status: 401 });
  }

  // Verify agent exists in D1
  const agent = await lookupAgent(env.DB, agentId);
  if (!agent) {
    return Response.json({ error: `Agent '${agentId}' not found` }, { status: 404 });
  }

  // Route to a Durable Object instance keyed by a unique conversation ID
  const conversationId = crypto.randomUUID();
  const doId = env.CONVERSATION_SESSION.idFromName(conversationId);
  const stub = env.CONVERSATION_SESSION.get(doId);

  // Forward the request to the Durable Object, passing agent config as headers
  const doRequest = new Request(request.url, {
    headers: new Headers({
      ...Object.fromEntries(request.headers.entries()),
      "Upgrade": "websocket",
      "X-Conversation-Id": conversationId,
      "X-Agent-Config": JSON.stringify(agent),
      "X-Bravo-Tunnel-Url": env.BRAVO_TUNNEL_URL || "",
      "X-User-Id": authenticated.userId || "",
    }),
  });

  return stub.fetch(doRequest);
}

// ---------------------------------------------------------------------------
// Post-session feedback
// ---------------------------------------------------------------------------

async function handlePostFeedback(request: Request, conversationId: string, env: Env): Promise<Response> {
  try {
    const body = (await request.json()) as Record<string, unknown>;
    const feedback = body.feedback as string | undefined;
    const rating = body.rating as number | undefined;
    const comment = body.comment as string | undefined;

    await env.DB.prepare(
      `INSERT INTO conversation_feedback (conversation_id, feedback, rating, comment, created_at)
       VALUES (?, ?, ?, ?, datetime('now'))`
    )
      .bind(conversationId, feedback ?? null, rating ?? null, comment ?? null)
      .run();

    return Response.json({ status: "ok" });
  } catch (err) {
    return Response.json({ error: "Failed to store feedback" }, { status: 500 });
  }
}

// ---------------------------------------------------------------------------
// Authentication helpers
// ---------------------------------------------------------------------------

interface AuthResult {
  ok: boolean;
  error?: string;
  userId?: string;
}

async function authenticateRequest(
  request: Request,
  url: URL,
  agentId: string,
  env: Env
): Promise<AuthResult> {
  // 1. Check for signed URL token
  const token = url.searchParams.get("token");
  if (token) {
    const valid = await verifySignedToken(token, agentId, env);
    if (valid) {
      return { ok: true };
    }
    return { ok: false, error: "Invalid signed URL token" };
  }

  // 2. Check xi-api-key header
  const apiKey = request.headers.get("xi-api-key");
  if (apiKey) {
    if (!env.ELEVENLABS_API_KEY || apiKey === env.ELEVENLABS_API_KEY) {
      return { ok: true };
    }
    return { ok: false, error: "Invalid API key" };
  }

  // 3. Check WebSocket subprotocol for bearer token
  const protocols = request.headers.get("Sec-WebSocket-Protocol") || "";
  const protocolList = protocols.split(",").map((p) => p.trim());
  const bearerProtocol = protocolList.find((p) => p.startsWith("bearer."));
  if (bearerProtocol) {
    const bearerToken = bearerProtocol.slice(7); // Remove "bearer." prefix
    if (!env.ELEVENLABS_API_KEY || bearerToken === env.ELEVENLABS_API_KEY) {
      return { ok: true };
    }
    return { ok: false, error: "Invalid bearer token" };
  }

  // 4. Check if agent requires auth — if not, allow unauthenticated
  try {
    const agent = await lookupAgent(env.DB, agentId);
    if (agent && !agent.requires_auth) {
      return { ok: true };
    }
  } catch {
    // Fall through to auth required
  }

  return { ok: false, error: "Authentication required. Provide xi-api-key header, bearer subprotocol, or signed URL token." };
}

async function generateSignedToken(agentId: string, env: Env): Promise<string> {
  const payload = {
    agent_id: agentId,
    exp: Date.now() + 300_000, // 5 minutes
    nonce: crypto.randomUUID(),
  };
  const payloadStr = JSON.stringify(payload);
  const payloadB64 = btoa(payloadStr);

  const secret = env.CONVAI_SIGNING_SECRET || "echo-convai-default-secret";
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(payloadB64));
  const sigB64 = btoa(String.fromCharCode(...new Uint8Array(signature)));

  return `${payloadB64}.${sigB64}`;
}

async function verifySignedToken(token: string, agentId: string, env: Env): Promise<boolean> {
  try {
    const parts = token.split(".");
    if (parts.length !== 2) return false;

    const [payloadB64, sigB64] = parts;
    const secret = env.CONVAI_SIGNING_SECRET || "echo-convai-default-secret";
    const encoder = new TextEncoder();
    const key = await crypto.subtle.importKey(
      "raw",
      encoder.encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["verify"]
    );

    const sigBytes = Uint8Array.from(atob(sigB64), (c) => c.charCodeAt(0));
    const valid = await crypto.subtle.verify("HMAC", key, sigBytes, encoder.encode(payloadB64));
    if (!valid) return false;

    const payload = JSON.parse(atob(payloadB64));
    if (payload.agent_id !== agentId) return false;
    if (payload.exp < Date.now()) return false;

    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// D1 agent lookup
// ---------------------------------------------------------------------------

async function lookupAgent(db: D1Database, agentId: string): Promise<import("./types").AgentConfig | null> {
  try {
    const result = await db
      .prepare("SELECT * FROM agents WHERE agent_id = ? LIMIT 1")
      .bind(agentId)
      .first();

    if (!result) return null;

    return {
      agent_id: result.agent_id as string,
      name: result.name as string,
      prompt: result.prompt as string,
      first_message: result.first_message as string | null,
      language: (result.language as string) || "en",
      voice_id: result.voice_id as string,
      model_id: (result.model_id as string) || "eleven_turbo_v2_5",
      max_duration_seconds: (result.max_duration_seconds as number) || 600,
      requires_auth: Boolean(result.requires_auth),
      tts_speed: (result.tts_speed as number) || 1.0,
      tts_stability: (result.tts_stability as number) || 0.5,
      tts_similarity_boost: (result.tts_similarity_boost as number) || 0.75,
      tools: result.tools as string | null,
      created_at: result.created_at as string,
      updated_at: result.updated_at as string,
    };
  } catch {
    return null;
  }
}
