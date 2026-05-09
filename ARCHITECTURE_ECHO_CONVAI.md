# Architecture — Echo ConvAI
## System Design & Technical Decisions
## 2026-04-04

---

## System Overview

Echo ConvAI is a 3-tier architecture:

```
┌─────────────────────────────────────────────────────────────────────┐
│ TIER 1: CLIENT LAYER                                                │
│                                                                     │
│ TypeScript SDK    React SDK       Widget          Python SDK         │
│ (1,880 lines)     (225 lines)     (1,203 lines)   (1,951 lines)    │
│ Conversation      useConversation Shadow DOM       async/await       │
│ class             useAudio hook   auto-init        context manager   │
│                                                                     │
│ All SDKs speak the same WebSocket protocol                         │
│ All support: audio streaming, text mode, tool calls, emotions      │
└───────────────────────┬─────────────────────────────────────────────┘
                        │ wss:// WebSocket + https:// REST
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│ TIER 2: CLOUDFLARE EDGE (Global, Zero-Cost)                         │
│                                                                     │
│ ┌─────────────────────────────┐ ┌─────────────────────────────────┐ │
│ │ echo-convai-api             │ │ echo-convai-gateway             │ │
│ │ Hono + D1 + R2              │ │ Durable Objects                 │ │
│ │ 1,703 lines TypeScript      │ │ 1,603 lines TypeScript          │ │
│ │                             │ │                                 │ │
│ │ 22 REST endpoints:          │ │ ConversationSession DO:         │ │
│ │ - Agent CRUD                │ │ - 1 instance per conversation   │ │
│ │ - Conversation tracking     │ │ - WebSocket lifecycle mgmt      │ │
│ │ - Knowledge upload (R2)     │ │ - Ping/pong keepalive           │ │
│ │ - Signed URL generation     │ │ - Max duration enforcement      │ │
│ │ - Feedback collection       │ │ - Hibernation API (cost)        │ │
│ │ - API key auth              │ │ - Audio forwarding              │ │
│ │                             │ │ - Text message routing          │ │
│ │ D1 Tables:                  │ │                                 │ │
│ │ - agents                    │ │ Auth:                           │ │
│ │ - conversations             │ │ - JWT signed URL tokens         │ │
│ │ - messages                  │ │ - xi-api-key header             │ │
│ │ - api_keys                  │ │ - Bearer subprotocol            │ │
│ │ - knowledge_documents       │ │ - Agent-level auth toggle       │ │
│ │ - signed_url_tokens         │ │                                 │ │
│ └─────────────────────────────┘ └──────────────┬──────────────────┘ │
└────────────────────────────────────────────────┼────────────────────┘
                                                 │ cloudflared tunnel
                                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│ TIER 3: GPU COMPUTE (BRAVO, RTX 4060)                               │
│                                                                     │
│ ┌─────────────────────────────────────────────────────────────────┐ │
│ │ Orchestrator (port 8800, 2,111 lines)                           │ │
│ │                                                                 │ │
│ │ Pipeline:                                                       │ │
│ │ Audio In → VAD → ASR → Turn-Taking → LLM → TTS → Audio Out    │ │
│ │                                                                 │ │
│ │ - Multi-LLM routing (Claude, GPT-4, Gemini, local)             │ │
│ │ - Cascade failover between LLM providers                       │ │
│ │ - Turn-taking: Silero VAD + learned classifier                 │ │
│ │ - Interruption detection and handling                          │ │
│ │ - Session state machine                                        │ │
│ │ - Context window management                                    │ │
│ │ - Tool call orchestration                                      │ │
│ └──────────────┬─────────────────────────┬───────────────────────┘ │
│ ┌──────────────▼──────────────┐ ┌────────▼───────────────────────┐ │
│ │ ASR Service (port 8801)     │ │ TTS Service (port 8802)        │ │
│ │ 1,034 lines Python          │ │ 1,453 lines Python             │ │
│ │                             │ │                                │ │
│ │ - faster-whisper             │ │ - CosyVoice 2 (0.5B)          │ │
│ │ - large-v3-turbo model       │ │ - 18 emotion tags              │ │
│ │ - CTranslate2 backend       │ │ - Voice cloning (zero-shot)    │ │
│ │ - Silero VAD integration     │ │ - ElevenLabs proxy (fallback)  │ │
│ │ - Streaming WebSocket        │ │ - 24kHz output                 │ │
│ │ - <200ms latency             │ │ - Streaming synthesis          │ │
│ │ - Keyword boosting           │ │ - Prosody vector control       │ │
│ │ - Multi-language             │ │                                │ │
│ └─────────────────────────────┘ └────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Key Design Decisions

### 1. Durable Objects for WebSocket Sessions
**Decision**: One Durable Object instance per conversation session.
**Why**: Cloudflare Durable Objects provide single-threaded, stateful execution with WebSocket support and automatic hibernation. Each conversation gets its own isolated instance with consistent state, no race conditions, and cost-efficient hibernation when idle.
**Alternative rejected**: KV + stateless workers — would require external state management and couldn't maintain WebSocket connections.

### 2. JWT for Signed URLs (not HMAC)
**Decision**: Full JWT tokens (HS256) for signed WebSocket URLs.
**Why**: Interoperability with the API worker's JWT signing, standard token format, built-in expiry claims. The gateway accepts both JWT (3-part) and legacy HMAC (2-part) tokens.
**Secret**: Shared between API and Gateway workers via `JWT_SECRET` env var.

### 3. CosyVoice 2 for TTS (not F5-TTS)
**Decision**: CosyVoice 2 (0.5B, Alibaba) as primary TTS engine.
**Why**: 
- Native instruction-following for emotions (instruct_text parameter)
- Apache 2.0 license (free commercial use)
- 0.5B params fits in 6-8GB VRAM on RTX 4060
- 9 language support
- Voice cloning via zero-shot inference
**Alternative rejected**: F5-TTS — poor quality ("IT SUCKS" — Commander). ElevenLabs proxy kept as fallback.

### 4. ElevenLabs-Compatible Protocol
**Decision**: Exact replication of ElevenLabs WebSocket message types and REST API patterns.
**Why**: Drop-in replacement. Any ElevenLabs SDK or client can connect to Echo ConvAI without code changes. Just change the server URL.

### 5. Edge + GPU Split
**Decision**: Cloudflare Workers handle auth/routing/state, BRAVO GPU handles ML inference.
**Why**: 
- Edge layer is always-on, globally distributed, free tier
- GPU layer only runs when needed (saves power)
- cloudflared tunnel connects them securely
- If GPU goes down, edge still serves API responses and queues requests

### 6. D1 for Agent Config (JSON blob, not columns)
**Decision**: Store `conversation_config` as a JSON text column rather than normalized columns.
**Why**: ElevenLabs' agent config is deeply nested with many optional fields. A JSON blob allows arbitrary nesting and easy PATCH operations (deep merge). Avoids schema migrations when adding new config options.

---

## Data Flow — Voice Conversation

```
1. Client connects: wss://gateway?agent_id=X&token=JWT
2. Gateway validates JWT, looks up agent in D1
3. Gateway creates Durable Object (ConversationSession)
4. DO sends: conversation_initiation_metadata
5. DO sends: agent_response (first_message from agent config)
6. DO sends: audio (TTS of first_message, via BRAVO tunnel)

7. Client sends: user_audio_chunk (PCM base64)
8. Gateway forwards to BRAVO orchestrator
9. Orchestrator → ASR service (port 8801)
10. ASR returns transcript
11. Orchestrator → LLM (Claude/GPT/Gemini)
12. LLM returns response text
13. Orchestrator → TTS service (port 8802)
14. TTS returns audio chunks (streaming)
15. Orchestrator sends back: user_transcript + agent_response + audio

16. DO keeps sending ping every 10s
17. Client responds with pong
18. Max duration enforced, session cleaned up on timeout
```

---

## Security Model

| Layer | Mechanism |
|-------|-----------|
| API Auth | SHA-256 hashed API keys in D1, constant-time comparison |
| WebSocket Auth | JWT signed URLs (5-min expiry), bearer subprotocol, direct key |
| Token Signing | HMAC-SHA256, shared secret between API and Gateway |
| D1 Access | Worker bindings only (no external access) |
| R2 Access | Worker bindings only (no public bucket) |
| Tunnel | cloudflared encrypted tunnel (no open ports on BRAVO) |
| Agent Auth | Per-agent `requires_auth` toggle |

---

## Performance Characteristics

| Metric | Target | Current |
|--------|--------|---------|
| API TTFB | <50ms | ~30ms (Cloudflare edge) |
| WebSocket connect | <200ms | ~150ms (DFW colo) |
| ASR latency | <200ms | TBD (model not yet loaded) |
| TTS TTFB | <500ms | TBD (CosyVoice 2 not yet loaded) |
| Total voice round-trip | <1.5s | TBD |
| Max concurrent sessions | 100+ | Durable Object scaling |
| Max session duration | 60 min | Configurable |

---

## Cost Model

| Resource | Free Tier Limit | Our Usage |
|----------|----------------|-----------|
| Workers requests | 100K/day | ~1K/day |
| Durable Objects | 1M requests/month | ~10K/month |
| D1 reads | 5M/day | ~5K/day |
| D1 writes | 100K/day | ~1K/day |
| D1 storage | 5GB | ~10MB |
| R2 storage | 10GB/month | ~100MB |
| R2 operations | 1M Class A, 10M Class B | ~1K/day |

**Total cloud cost: $0/month on free tier.**
GPU costs are electricity only (BRAVO runs locally).
