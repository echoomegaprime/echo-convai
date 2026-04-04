# Echo ConvAI

**Production conversational AI platform by Echo Prime Technologies.**
Drop-in replacement for ElevenLabs Conversational AI with edge-accelerated gateway and GPU inference.

[![Workers](https://img.shields.io/badge/Cloudflare-Workers-F38020)](https://echo-convai-api.bmcii1976.workers.dev)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Lines](https://img.shields.io/badge/lines-18%2C858-brightgreen.svg)]()

## Architecture

```
+-------------------------------------------------------------------+
|                        Client Layer                                |
|  +----------------+ +----------------+ +------------------------+ |
|  | TypeScript     | | React SDK      | | Embeddable Widget      | |
|  | Client SDK     | | useConv hook   | | <script> drop-in       | |
|  +-------+--------+ +-------+--------+ +-----------+------------+ |
|          |                   |                      |              |
|  +-------+-------------------+----------------------+              |
|  |          Python SDK (pip install echo-convai)                   |
|  +---------------------+--------------------------------------+   |
+------------------------|------------------------------------------+
                         | WebSocket + REST
+------------------------|------------------------------------------+
|              Cloudflare Edge (Global)                              |
|  +---------------------+-------------------------------------+    |
|  |  echo-convai-gateway (Durable Objects)                     |    |
|  |  - WebSocket lifecycle management                          |    |
|  |  - Per-session Durable Object isolation                    |    |
|  |  - Signed URL / Bearer token auth                          |    |
|  |  - Ping/pong keepalive, max duration enforcement           |    |
|  |  - Full ElevenLabs protocol: 10 client + 22 server msgs    |    |
|  +---------------------+-------------------------------------+    |
|  +---------------------+-------------------------------------+    |
|  |  echo-convai-api (Hono + D1 + R2)                          |    |
|  |  - 22 REST endpoints (agent CRUD, conversations, knowledge)|    |
|  |  - D1 database (6 tables, indexed)                         |    |
|  |  - R2 storage (knowledge base, audio archive)              |    |
|  |  - JWT signed URLs, xi-api-key auth                        |    |
|  +---------------------+-------------------------------------+    |
+------------------------|------------------------------------------+
                         | cloudflared tunnel
+------------------------|------------------------------------------+
|              BRAVO GPU Node (RTX 4060)                            |
|  +---------------------+-------------------------------------+    |
|  |  Orchestrator (port 8800)                                  |    |
|  |  - Pipeline: Audio -> ASR -> Turn-Taking -> LLM -> TTS    |    |
|  |  - Multi-LLM: Claude, GPT, Gemini, local models           |    |
|  |  - Turn-taking: Silero VAD + learned classifier            |    |
|  |  - Session state machine, interruption handling            |    |
|  +----------+-----------------------------------+-------------+    |
|  +----------+----------+          +--------------+------------+    |
|  | ASR (port 8801)     |          | TTS (port 8802)           |    |
|  | faster-whisper       |          | F5-TTS / CosyVoice       |    |
|  | large-v3-turbo       |          | ElevenLabs-compatible API |    |
|  | Streaming WS         |          | Emotion tags, voice clone |    |
|  | <200ms latency       |          | Streaming synthesis       |    |
|  +---------------------+          +---------------------------+    |
+-------------------------------------------------------------------+
```

## Features

### Edge Gateway
- **Durable Objects** - one per conversation, hibernation API for cost efficiency
- **Full protocol** - all 32 ElevenLabs WebSocket message types implemented
- **Auth** - JWT signed URLs (5-min expiry), bearer token subprotocol, xi-api-key header
- **Keepalive** - automatic ping/pong every 25s, configurable max duration

### REST API (22 endpoints)
- Agent CRUD with deep-merge PATCH updates
- Conversation tracking with messages, analysis, feedback
- Knowledge base upload/management via R2
- Signed URL generation for WebSocket auth
- Internal API key management

### GPU Services
- **ASR** - faster-whisper large-v3-turbo, streaming WebSocket, <200ms latency, keyword boosting
- **TTS** - F5-TTS/CosyVoice, emotion tags [laughs] [whispers] [excited], voice cloning
- **Orchestrator** - full pipeline coordinator, multi-LLM routing with cascade failover, turn-taking

### SDKs
- **TypeScript** - `@echo-prime/convai-client` (2,630 lines)
- **React** - `@echo-prime/convai-react` with `useConversation` hook (1,172 lines)
- **Widget** - `@echo-prime/convai-widget` embeddable `<script>` tag (1,203 lines)
- **Python** - `echo-convai` pip package (1,478 lines)

## Quick Start

### TypeScript
```typescript
import { Conversation } from '@echo-prime/convai-client';

const conversation = new Conversation({
  serverUrl: 'wss://echo-convai-gateway.bmcii1976.workers.dev',
  agentId: 'agt_xxx',
  apiKey: 'xi_echo_xxx',
});

conversation.on('agent_response', (text) => console.log('Agent:', text));
conversation.on('audio', (chunk) => playAudio(chunk));

await conversation.connect();
```

### React
```tsx
import { ConversationProvider, useConversation } from '@echo-prime/convai-react';

function App() {
  return (
    <ConversationProvider serverUrl="wss://echo-convai-gateway..." apiKey="xi_echo_...">
      <ChatUI />
    </ConversationProvider>
  );
}

function ChatUI() {
  const { startSession, endSession, status, transcript } = useConversation();
  return <button onClick={() => startSession('agt_xxx')}>Talk</button>;
}
```

### Embeddable Widget
```html
<script src="https://cdn.echo-ept.com/convai-widget.js"></script>
<script>
  EchoConvAI.init({
    agentId: 'agt_xxx',
    serverUrl: 'wss://echo-convai-gateway.bmcii1976.workers.dev',
  });
</script>
```

### Python
```python
from echo_convai import Conversation

async with Conversation(agent_id="agt_xxx", server_url="wss://...") as conv:
    conv.on_agent_response = lambda text: print(f"Agent: {text}")
    conv.on_audio = lambda chunk: play(chunk)
    await conv.run()
```

### REST API
```bash
# Create agent
curl -X POST https://echo-convai-api.bmcii1976.workers.dev/v1/convai/agents/create \
  -H "xi-api-key: xi_echo_xxx" \
  -H "Content-Type: application/json" \
  -d '{"name":"My Agent","conversation_config":{"agent":{"prompt":{"prompt":"You are helpful."}}}}'

# Get signed URL for WebSocket
curl https://echo-convai-api.bmcii1976.workers.dev/v1/convai/conversation/get-signed-url?agent_id=agt_xxx \
  -H "xi-api-key: xi_echo_xxx"
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/convai/agents/create` | Create agent |
| GET | `/v1/convai/agents` | List agents |
| GET | `/v1/convai/agents/:id` | Get agent |
| PATCH | `/v1/convai/agents/:id` | Update agent |
| DELETE | `/v1/convai/agents/:id` | Delete agent |
| GET | `/v1/convai/conversation/get-signed-url` | Get WebSocket auth URL |
| GET | `/v1/convai/conversations` | List conversations |
| GET | `/v1/convai/conversations/:id` | Get conversation details |
| DELETE | `/v1/convai/conversations/:id` | Delete conversation |
| POST | `/v1/convai/conversations/:id/feedback` | Submit feedback |
| POST | `/v1/convai/agents/:id/knowledge` | Upload knowledge doc |
| GET | `/v1/convai/agents/:id/knowledge` | List knowledge docs |
| POST | `/v1/text-to-speech/:voice_id` | Text-to-speech |
| POST | `/v1/text-to-speech/:voice_id/stream` | Streaming TTS |

## WebSocket Protocol

32 message types (ElevenLabs-compatible):

**Client to Server (10)**

| Type | Description |
|------|-------------|
| `user_audio_chunk` | PCM audio (base64) |
| `user_text_input` | Text message |
| `pong` | Keepalive response |
| `user_contextual_update` | Inject context |
| `client_tool_result` | Tool call result |
| `user_feedback` | Feedback submission |
| `conversation_initiation_client_data` | Session config |
| `user_interruption` | Interrupt agent |
| `user_activity` | Activity signal |
| `recording_start` | Start recording |

**Server to Client (22)**

| Type | Description |
|------|-------------|
| `conversation_initiation_metadata` | Session metadata |
| `agent_response` | Agent text (streaming) |
| `audio` | Agent audio (base64 PCM) |
| `user_transcript` | User speech transcript |
| `interruption` | Agent was interrupted |
| `ping` | Keepalive ping |
| `agent_response_correction` | Revised response |
| `internal_turn_probability` | Turn prediction score |
| `internal_vad_score` | Voice activity score |
| `internal_tentative_agent_response` | Partial response |
| `client_tool_call` | Tool invocation |
| `conversation_status_update` | Status change |

## Tech Stack

- **Edge**: Cloudflare Workers, Durable Objects, D1, R2
- **API Framework**: Hono (TypeScript)
- **ASR**: faster-whisper (CTranslate2) + Silero VAD
- **TTS**: F5-TTS / CosyVoice with emotion tags
- **LLM**: Multi-provider (Claude, GPT-4, Gemini, local)
- **Auth**: JWT (jose), SHA-256 key hashing
- **SDKs**: TypeScript, React, Python, Embeddable Widget

## Deployment

### Cloudflare Workers
```bash
cd workers/echo-convai-api && npm install && npx wrangler deploy
cd workers/echo-convai-gateway && npm install && npx wrangler deploy
```

### GPU Services (BRAVO node)
```bash
pip install -r requirements.txt
python services/asr_service.py    # Port 8801
python services/tts_service.py    # Port 8802
python services/orchestrator.py   # Port 8800
```

### Tunnel
```bash
cloudflared tunnel --url http://localhost:8800 --name echo-convai-bravo
```

## Project Stats

| Component | Lines | Language |
|-----------|-------|----------|
| GPU Services | 3,939 | Python |
| Core Libraries | 3,747 | Python |
| Cloudflare Workers | 3,343 | TypeScript |
| TS Client SDK | 2,630 | TypeScript |
| Python SDK | 1,478 | Python |
| Widget | 1,203 | TypeScript |
| React SDK | 1,172 | TypeScript/TSX |
| Analysis Docs | 1,346 | Markdown |
| **Total** | **18,858** | **Multi-language** |

## License

MIT - Echo Prime Technologies 2026
