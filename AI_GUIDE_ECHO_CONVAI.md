# AI Guide — Echo ConvAI
## Complete ElevenLabs Conversational AI Reverse Engineering & Replication
## Date: 2026-04-04 | Status: Edge LIVE, GPU Pending

---

## WHAT THIS IS

Echo ConvAI is a **complete, production-grade replication** of ElevenLabs' Conversational AI platform. It was built by reverse engineering ElevenLabs' public APIs, SDKs (Python + JS), WebSocket protocol, and TTS behavior, then building a drop-in compatible replacement from scratch.

**It is NOT a wrapper around ElevenLabs.** It is a standalone system that runs on our own infrastructure with our own TTS model (CosyVoice 2), our own ASR (faster-whisper), and our own edge gateway (Cloudflare Workers + Durable Objects).

---

## ARCHITECTURE OVERVIEW

```
Client SDKs (TS, React, Widget, Python)
    │ WebSocket + REST
    ▼
┌──────────────────────────────────────────────┐
│  CLOUDFLARE EDGE (Global, Always On)         │
│                                              │
│  echo-convai-api (Hono, 1703 lines)          │
│  - 22 REST endpoints                         │
│  - D1 database (7 tables)                    │
│  - R2 storage (knowledge base, audio)        │
│  - JWT signed URL generation                 │
│  - xi-api-key auth (ElevenLabs-compatible)   │
│                                              │
│  echo-convai-gateway (1603 lines)            │
│  - Durable Objects (1 per conversation)      │
│  - 32 WebSocket message types                │
│  - JWT + bearer + signed URL auth            │
│  - Ping/pong keepalive                       │
│  - Max duration enforcement                  │
│  - Hibernation API for cost efficiency       │
└──────────┬───────────────────────────────────┘
           │ cloudflared tunnel
           ▼
┌──────────────────────────────────────────────┐
│  BRAVO GPU NODE (RTX 4060, local)            │
│                                              │
│  Orchestrator (port 8800, 2111 lines)        │
│  - Pipeline: Audio → ASR → LLM → TTS        │
│  - Multi-LLM: Claude, GPT, Gemini, local    │
│  - Turn-taking: Silero VAD + classifier      │
│  - Session state machine                     │
│  - Interruption handling                     │
│                                              │
│  ASR Service (port 8801, 1034 lines)         │
│  - faster-whisper large-v3-turbo             │
│  - Streaming WebSocket                       │
│  - <200ms latency                            │
│  - Keyword boosting                          │
│                                              │
│  TTS Service (port 8802, 1453 lines)         │
│  - CosyVoice 2 (0.5B params, Apache 2.0)    │
│  - 18 emotion tags mapped to instruct_text   │
│  - Voice cloning via zero-shot inference     │
│  - ElevenLabs proxy backend (fallback)       │
│  - 24kHz output, streaming synthesis         │
└──────────────────────────────────────────────┘
```

---

## FILE MAP & LINE COUNTS

### Cloudflare Workers (3,306 lines TypeScript)
| File | Lines | Purpose |
|------|-------|---------|
| `workers/echo-convai-api/src/index.ts` | 1,703 | REST API — 22 endpoints, Hono framework, D1/R2 bindings |
| `workers/echo-convai-gateway/src/index.ts` | 392 | Gateway entry — WebSocket upgrade, JWT auth, DO routing |
| `workers/echo-convai-gateway/src/conversation-session.ts` | 746 | Durable Object — full conversation lifecycle |
| `workers/echo-convai-gateway/src/types.ts` | 465 | Complete TypeScript types for all 32 WS message types |

### GPU Services (4,598 lines Python)
| File | Lines | Purpose |
|------|-------|---------|
| `services/orchestrator.py` | 2,111 | Pipeline coordinator, multi-LLM, turn-taking, VAD |
| `services/tts_service.py` | 1,453 | TTS engine — CosyVoice2 + ElevenLabs proxy + emotion tags |
| `services/asr_service.py` | 1,034 | ASR — faster-whisper, streaming WS, keyword boosting |

### SDKs (4,259 lines)
| File | Lines | Language | Purpose |
|------|-------|----------|---------|
| `sdks/convai-client/src/conversation.ts` | 901 | TypeScript | Main Conversation class |
| `sdks/convai-client/src/types.ts` | 518 | TypeScript | Full type definitions |
| `sdks/convai-client/src/api.ts` | 344 | TypeScript | REST API client |
| `sdks/convai-client/src/index.ts` | 117 | TypeScript | Public exports |
| `sdks/convai-react/src/` | 225 | TypeScript | React hooks (useConversation, useAudio) |
| `sdks/convai-widget/src/` | 1,203 | TypeScript | Embeddable widget (Shadow DOM) |
| `sdks/echo-convai-python/` | 1,951 | Python | Full Python SDK with demo |

### RE Analysis Docs (3,068 lines)
| File | Lines | Purpose |
|------|-------|---------|
| `OPENAPI_DEEP_ANALYSIS.md` | 1,257 | Full API schema analysis |
| `SDK_ANALYSIS.md` | 989 | Python SDK reverse engineering |
| `JS_SDK_ANALYSIS.md` | 738 | JavaScript SDK reverse engineering |
| `RE_FINDINGS.md` | 84 | Live capture findings (TTS timing, emotion tags, WS protocol) |

### TOTAL: ~15,231 lines of source code + 3,068 lines of RE docs

---

## DEPLOYED INFRASTRUCTURE

### Live Endpoints
| Service | URL | Status |
|---------|-----|--------|
| API Worker | `https://echo-convai-api.bmcii1976.workers.dev` | LIVE |
| Gateway Worker | `https://echo-convai-gateway.bmcii1976.workers.dev` | LIVE |

### D1 Database: `echo-convai` (6da5a226-9269-4e34-9ca2-c44db6cdea24)
| Table | Purpose |
|-------|---------|
| `agents` | Agent configs (prompt, voice, TTS settings) |
| `conversations` | Conversation records with status/duration |
| `messages` | Per-conversation message history |
| `api_keys` | Hashed API keys for auth |
| `knowledge_documents` | R2-backed knowledge base docs |
| `signed_url_tokens` | JWT token tracking for signed URLs |
| `_cf_KV` | Internal Cloudflare KV |

### R2 Bucket: `echo-convai-storage`
- Knowledge base documents
- Audio recordings archive

### API Key
- Stored in OmniSync: `ECHO_CONVAI_API_KEY`
- Format: `xi_echo_...` (ElevenLabs-compatible prefix)
- Auth: `xi-api-key` header on all requests

---

## AUTHENTICATION FLOW

Three auth methods (matching ElevenLabs exactly):

1. **xi-api-key header** — Direct API key in `xi-api-key` header
2. **Signed URL** — GET `/v1/convai/conversation/get-signed-url?agent_id=X` → returns `wss://...?token=JWT`
3. **Bearer subprotocol** — WebSocket subprotocol `bearer.{api_key}`

JWT tokens:
- Algorithm: HS256
- Secret: `JWT_SECRET` env var (default: `echo-convai-default-jwt-secret-change-me`)
- Expiry: 5 minutes
- Payload: `{ agent_id, workspace_id, token_id, iat, exp, jti }`
- Gateway verifies both JWT (3-part) and legacy HMAC (2-part) tokens

---

## WEBSOCKET PROTOCOL (32 message types)

### Client → Server (10 types)
| Type | Description |
|------|-------------|
| `conversation_initiation_client_data` | First message — session config, overrides |
| `user_audio_chunk` | PCM audio (base64) for ASR |
| `user_message` | Text message input |
| `pong` | Keepalive response to ping |
| `contextual_update` | Inject context without interrupting |
| `client_tool_result` | Result from client-side tool execution |
| `mcp_tool_approval_result` | MCP tool approval |
| `feedback` | In-session feedback |
| `user_activity` | Keep-alive activity signal |
| `multimodal_message` | Text + file combined |

### Server → Client (22 types)
| Type | Description |
|------|-------------|
| `conversation_initiation_metadata` | Session metadata (conv_id, formats) |
| `agent_response` | Complete agent text response |
| `agent_chat_response_part` | Streaming text (start/delta/stop) |
| `audio` | Agent TTS audio (base64 PCM) |
| `user_transcript` | User speech transcription |
| `interruption` | Agent was interrupted |
| `ping` | Keepalive (client must respond with pong) |
| `agent_response_correction` | Revised response |
| `internal_tentative_agent_response` | Partial/tentative response |
| `client_tool_call` | Request client to execute a tool |
| `mcp_tool_call` | MCP tool invocation |
| `mcp_connection_status` | MCP server connection status |
| `agent_tool_request` | Server-side tool request (logging) |
| `agent_tool_response` | Server-side tool response (logging) |
| `vad_score` | Voice activity detection score |
| `asr_initiation_metadata` | ASR config |
| `guardrail_triggered` | Guardrail was triggered |
| `error` | Error message |

---

## TTS ENGINE — EMOTION TAG MAPPING

CosyVoice 2 maps 18 ElevenLabs-style emotion tags to `instruct_text` commands:

| Tag | CosyVoice Instruction |
|-----|----------------------|
| `[excited]` | Speak with excitement and high energy at a faster pace |
| `[angry]` | Speak angrily with intensity and a forceful tone |
| `[sad]` | Speak sadly with a slow, melancholic, sorrowful tone |
| `[whispers]` | Whisper softly and quietly with a breathy intimate tone |
| `[laughs]` | Speak while laughing with joy and amusement |
| `[crying]` | Speak while crying with a shaky emotional voice |
| `[sarcastic]` | Speak with dry sarcasm and slight mockery |
| `[sympathetic]` | Speak with warm sympathy and gentle concern |
| `[curious]` | Speak with curiosity and wonder, slight upward inflection |
| `[fearful]` | Speak with fear and anxiety in a trembling voice |
| `[disgusted]` | Speak with disgust and revulsion |
| `[surprised]` | Speak with genuine surprise and amazement |
| `[neutral]` | Speak in a calm neutral professional tone |
| `[confident]` | Speak with strong confidence and authority |
| `[gentle]` | Speak gently and softly with warmth and care |
| `[urgent]` | Speak with urgency and importance at a faster pace |
| `[cheerful]` | Speak cheerfully with a bright happy tone |
| `[serious]` | Speak in a serious somber tone with gravity |

---

## RE FINDINGS — KEY DISCOVERIES

From live reverse engineering of ElevenLabs (April 4, 2026):

- **TTS Architecture**: Autoregressive (linear time vs text length), NOT diffusion
- **TTFB**: ~700-800ms constant (model startup overhead)
- **Generation Rate**: ~24-36 chars/sec after TTFB
- **Seed Determinism**: NOT deterministic (stochastic sampling)
- **ConvAI Ping Interval**: ~1.8s (very aggressive, not 10s)
- **Default LLM**: gemini-2.5-flash (not Claude/GPT)
- **WebRTC via LiveKit**: NOW DEFAULT for voice (WebSocket is legacy)
- **English ConvAI**: MUST use turbo or flash v2 (v3 not allowed)
- **Turn Modes**: "silence" or "turn" only (NOT "smooth")
- **Hidden Features**: Coaching system, PII redaction (50+ entity types), batch calling (10K recipients), workflow graph builder

---

## DEPLOYMENT COMMANDS

### Deploy Workers
```bash
cd C:/ECHO_OMEGA_PRIME/SYSTEMS/echo-convai/workers/echo-convai-api && npx wrangler deploy
cd C:/ECHO_OMEGA_PRIME/SYSTEMS/echo-convai/workers/echo-convai-gateway && npx wrangler deploy
```

### Start GPU Services (locally on BRAVO)
```bash
C:\Python311\python.exe C:\echo\convai\asr_service.py      # Port 8801
C:\Python311\python.exe C:\echo\convai\tts_service.py      # Port 8802
C:\Python311\python.exe C:\echo\convai\orchestrator.py     # Port 8800
```

### Download CosyVoice2 Model
```bash
C:\Python311\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('FunAudioLLM/CosyVoice2-0.5B', local_dir=r'C:\echo\pretrained_models\CosyVoice2-0.5B')"
```

### Tunnel
```bash
cloudflared tunnel --url http://localhost:8800 --name echo-convai-bravo
# Then set on gateway: npx wrangler secret put BRAVO_TUNNEL_URL
```

---

## TEST STATUS (2026-04-04)

### PASSED (20/20)
- API health, list agents, create agent, get agent, PATCH agent (deep merge)
- Signed URL generation (JWT, 5min expiry), list conversations
- Conversation detail with messages, submit feedback, delete agent
- Auth rejection (bad key returns 401)
- Gateway health, reject unauth WebSocket (401)
- JWT signed URL auth on WebSocket
- WebSocket connect to Durable Object
- `conversation_initiation_metadata` received
- `agent_response` with emotion tags received
- `user_message` → `user_transcript` working
- Ping/pong keepalive working

### PENDING (needs real internet + GPU services)
- CosyVoice2 model download (blocked by hotspot)
- GPU services startup (ASR, TTS, Orchestrator)
- cloudflared tunnel
- End-to-end voice conversation

---

## REMAINING TODOs (OmniSync #157-160)

1. **Download CosyVoice2-0.5B** — needs real internet (hotspot blocks HuggingFace)
2. **Start GPU services** — ASR (8801), TTS (8802), Orchestrator (8800)
3. **cloudflared tunnel** — connect edge gateway to BRAVO orchestrator
4. **End-to-end test** — mic → ASR → turn-taking → LLM → TTS → speaker

---

## FUTURE — CUSTOM TTS MODEL TRAINING

When 60GB VRAM cluster is ready (ALPHA 4060 + BRAVO 3070 + DELTA):
- Fine-tune CosyVoice 2 on Emilia dataset (100K+ hours)
- Train custom voice embeddings for Echo Prime voices
- Target: Match or exceed ElevenLabs v3 quality at zero API cost
