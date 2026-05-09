# User Guide — Echo ConvAI
## Voice AI Conversations — Your Own ElevenLabs Alternative
## Echo Prime Technologies | 2026

---

## What Is Echo ConvAI?

Echo ConvAI is a **complete voice conversation AI platform** — you create AI agents that can talk to users via voice or text in real time. Think of it as your own private ElevenLabs Conversational AI, running on your own infrastructure with zero per-character API costs.

---

## Quick Start (5 Minutes)

### 1. Create an Agent

```bash
curl -X POST https://echo-convai-api.bmcii1976.workers.dev/v1/convai/agents/create \
  -H "xi-api-key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Voice Agent",
    "conversation_config": {
      "agent": {
        "prompt": {"prompt": "You are a friendly voice assistant. Use [excited] when happy."},
        "first_message": "[excited] Hey there! How can I help you today?",
        "language": "en"
      },
      "tts": {
        "model_id": "eleven_v3",
        "voice_id": "keDMh3sQlEXKM4EQxvvi"
      }
    }
  }'
```

### 2. Get a Signed URL

```bash
curl "https://echo-convai-api.bmcii1976.workers.dev/v1/convai/conversation/get-signed-url?agent_id=YOUR_AGENT_ID" \
  -H "xi-api-key: YOUR_API_KEY"
```

### 3. Connect via WebSocket

```javascript
const ws = new WebSocket(signed_url);
ws.onopen = () => {
  ws.send(JSON.stringify({
    type: 'conversation_initiation_client_data',
    conversation_config_override: {}
  }));
};
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.type === 'agent_response') {
    console.log('Agent says:', data.agent_response_event.agent_response);
  }
};
```

---

## API Endpoints

| Method | Endpoint | What It Does |
|--------|----------|-------------|
| `POST` | `/v1/convai/agents/create` | Create a new voice agent |
| `GET` | `/v1/convai/agents` | List all your agents |
| `GET` | `/v1/convai/agents/:id` | Get agent details |
| `PATCH` | `/v1/convai/agents/:id` | Update agent (deep merge) |
| `DELETE` | `/v1/convai/agents/:id` | Delete an agent |
| `GET` | `/v1/convai/conversation/get-signed-url` | Get WebSocket auth URL |
| `GET` | `/v1/convai/conversations` | List past conversations |
| `GET` | `/v1/convai/conversations/:id` | Get conversation with messages |
| `DELETE` | `/v1/convai/conversations/:id` | Delete a conversation |
| `POST` | `/v1/convai/conversations/:id/feedback` | Submit feedback |
| `POST` | `/v1/convai/agents/:id/knowledge` | Upload knowledge doc |
| `GET` | `/v1/convai/agents/:id/knowledge` | List knowledge docs |

---

## Emotion Tags

Add emotion tags in square brackets to any text to control how the agent speaks:

| Tag | Effect |
|-----|--------|
| `[excited]` | High energy, faster speech |
| `[angry]` | Forceful, intense tone |
| `[sad]` | Slow, melancholic |
| `[whispers]` | Soft, breathy whisper |
| `[laughs]` | Laughing while speaking |
| `[sarcastic]` | Dry sarcasm |
| `[curious]` | Wondering, upward inflection |
| `[cheerful]` | Bright, happy |
| `[serious]` | Somber, grave |
| `[urgent]` | Fast, important |
| `[gentle]` | Soft, warm |
| `[confident]` | Strong, authoritative |

**Example prompt**: `"You are Echo. Use [excited] when greeting users. Use [whispers] for secrets. Use [laughs] when amused."`

---

## Using the SDKs

### TypeScript
```typescript
import { Conversation } from '@echo-prime/convai-client';

const conv = new Conversation({
  serverUrl: 'wss://echo-convai-gateway.bmcii1976.workers.dev',
  agentId: 'agt_xxx',
  apiKey: 'xi_echo_xxx',
});
conv.on('agent_response', (text) => console.log('Agent:', text));
conv.on('audio', (chunk) => playAudio(chunk));
await conv.connect();
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
  EchoConvAI.init({ agentId: 'agt_xxx', serverUrl: 'wss://echo-convai-gateway...' });
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

---

## Authentication

Three ways to authenticate:

1. **API Key Header**: Add `xi-api-key: YOUR_KEY` to all requests
2. **Signed URL**: Request a pre-authenticated WebSocket URL (expires in 5 min)
3. **Bearer Subprotocol**: Connect WebSocket with subprotocol `bearer.YOUR_KEY`

---

## Architecture Overview

```
You (browser/app)
  ↓ WebSocket
Cloudflare Edge (global, always on, free tier)
  ↓ cloudflared tunnel
BRAVO GPU (your machine, RTX 4060)
  → ASR: faster-whisper (speech-to-text)
  → LLM: Claude/GPT/Gemini (thinking)
  → TTS: CosyVoice 2 (text-to-speech with emotions)
  ↓ audio stream
You (hear the agent's voice)
```

---

## Costs

| Component | Cost |
|-----------|------|
| Cloudflare Workers (API + Gateway) | FREE (free tier) |
| D1 Database | FREE (free tier, 5GB) |
| R2 Storage | FREE (free tier, 10GB) |
| CosyVoice 2 TTS | FREE (Apache 2.0, runs on your GPU) |
| faster-whisper ASR | FREE (MIT, runs on your GPU) |
| LLM (Claude/GPT) | API costs per token |

**Total infrastructure cost: $0/month** (excluding LLM API usage)
