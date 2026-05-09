# @echo-prime/convai-client

TypeScript SDK for **Echo ConvAI** — a drop-in replacement for `@elevenlabs/client` that connects to the Echo ConvAI WebSocket gateway.

Works in both **browser** and **Node.js** (18+) environments.

## Installation

```bash
npm install @echo-prime/convai-client
```

For Node.js < 21, also install the `ws` package:

```bash
npm install ws
```

## Quick Start

### Text-Only Chat

```typescript
import { Conversation } from '@echo-prime/convai-client';

const conversation = new Conversation({ textOnly: true });

conversation.on('agent_response', ({ text }) => {
  console.log('Agent:', text);
});

conversation.on('error', ({ message }) => {
  console.error('Error:', message);
});

await conversation.connect('agent_abc123');

conversation.sendMessage('Hello! Tell me about yourself.');

// End session when done
conversation.endSession();
```

### Voice Conversation (Browser)

```typescript
import {
  Conversation,
  AudioCaptureManager,
  AudioPlaybackManager,
} from '@echo-prime/convai-client';

const conversation = new Conversation();
const capture = new AudioCaptureManager({ sampleRate: 16000 });
const playback = new AudioPlaybackManager({ sampleRate: 16000 });

// Play agent audio
conversation.on('audio', ({ audio }) => {
  playback.enqueue(audio);
});

// Handle interruptions
conversation.on('interruption', () => {
  playback.interrupt();
});

// Connect
await conversation.connect('agent_abc123');
await playback.start();

// Start capturing and sending microphone audio
await capture.start((pcmChunk, volume) => {
  conversation.sendAudio(pcmChunk);
});

// Cleanup
function cleanup() {
  capture.stop();
  playback.stop();
  conversation.endSession();
}
```

### Using startSession() (ElevenLabs-Compatible)

```typescript
import { startSession } from '@echo-prime/convai-client';

const conversation = await startSession({
  agentId: 'agent_abc123',
  textOnly: true,
  onAgentResponse: (text) => console.log('Agent:', text),
  onTranscript: (text) => console.log('User:', text),
  onError: (err) => console.error('Error:', err.message),
  onStatusChange: (status) => console.log('Status:', status),
});

conversation.sendMessage('What is the weather today?');
```

### Client Tools

```typescript
import { Conversation } from '@echo-prime/convai-client';

const conversation = new Conversation({ textOnly: true });

// Register tool handlers before connecting
conversation.registerToolHandler('get_weather', async (params) => {
  const city = params.city as string;
  // Your logic here
  return JSON.stringify({ temperature: 72, condition: 'sunny', city });
});

conversation.registerToolHandler('search_database', async (params) => {
  const query = params.query as string;
  return JSON.stringify({ results: [`Result for: ${query}`] });
});

await conversation.connect('agent_with_tools');
```

### REST API Client

```typescript
import { ConvAIApiClient } from '@echo-prime/convai-client';

const api = new ConvAIApiClient({
  baseUrl: 'https://echo-convai-gateway.bmcii1976.workers.dev',
  apiKey: 'your-api-key',
});

// Create an agent
const agent = await api.createAgent({
  name: 'Customer Support',
  prompt: 'You are a helpful customer support agent.',
  first_message: 'Hello! How can I help you today?',
  voice_id: 'keDMh3sQlEXKM4EQxvvi',
  language: 'en',
});

// List agents
const agents = await api.listAgents();

// Get signed URL for authenticated agents
const { signed_url } = await api.getSignedUrl(agent.agent_id);

// Text-to-speech
const audioBuffer = await api.textToSpeech('voice_id_here', 'Hello world!', {
  model_id: 'eleven_turbo_v2',
  output_format: 'mp3_44100_128',
});

// List conversations
const conversations = await api.listConversations(agent.agent_id);
```

## API Reference

### Conversation

The main class for WebSocket conversations.

#### Constructor

```typescript
new Conversation(options?: ConversationOptions)
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `serverUrl` | `string` | `wss://echo-convai-gateway...` | WebSocket server URL |
| `textOnly` | `boolean` | `false` | Text-only mode (no audio) |
| `authorization` | `string` | — | Auth token for private agents |
| `signedUrl` | `string` | — | Pre-signed WebSocket URL |
| `autoReconnect` | `boolean` | `true` | Auto-reconnect on disconnect |
| `maxReconnectAttempts` | `number` | `5` | Max reconnect attempts |
| `reconnectBaseDelay` | `number` | `1000` | Base delay (ms) between retries |
| `overridePrompt` | `string` | — | Override agent prompt |
| `overrideFirstMessage` | `string` | — | Override first message |
| `overrideVoiceId` | `string` | — | Override TTS voice |
| `ttsSpeed` | `number` | — | TTS speed multiplier |
| `ttsStability` | `number` | — | TTS stability (0-1) |
| `ttsSimilarityBoost` | `number` | — | TTS similarity boost (0-1) |
| `userId` | `string` | — | User ID for tracking |
| `dynamicVariables` | `Record<string, string \| number \| boolean>` | — | Template variables |
| `customLlmExtraBody` | `Record<string, unknown>` | — | Custom LLM parameters |

#### Methods

| Method | Description |
|--------|-------------|
| `connect(agentId?, options?)` | Establish WebSocket connection. Returns conversation ID. |
| `endSession()` | Gracefully close the session. |
| `sendAudio(chunk: ArrayBuffer)` | Send PCM audio data. |
| `sendMessage(text: string)` | Send a text message. |
| `sendContextualUpdate(text: string)` | Inject context without interrupting. |
| `sendMultimodalMessage(text?, fileId?)` | Send text + file reference. |
| `sendUserActivity()` | Ping to prevent timeout. |
| `sendFeedback(score, eventId?)` | Send like/dislike feedback. |
| `sendMCPToolApproval(toolCallId, approved)` | Approve/reject MCP tool call. |
| `registerToolHandler(name, handler)` | Register a client tool. |
| `unregisterToolHandler(name)` | Remove a client tool. |
| `sendToolResult(toolCallId, result, isError?)` | Manually send tool result. |
| `on(event, handler)` | Subscribe to events. Returns unsubscribe fn. |
| `off(event, handler)` | Unsubscribe from events. |
| `removeAllListeners(event?)` | Remove all listeners. |

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `status` | `ConversationStatus` | `'disconnected' \| 'connecting' \| 'connected' \| 'reconnecting' \| 'error'` |
| `mode` | `ConversationMode` | `'listening' \| 'speaking'` |
| `conversationId` | `string \| null` | Server-assigned conversation ID |
| `agentId` | `string \| null` | Connected agent ID |
| `isConnected` | `boolean` | Shorthand for `status === 'connected'` |
| `isSpeaking` | `boolean` | Shorthand for `mode === 'speaking'` |
| `isListening` | `boolean` | Shorthand for `mode === 'listening'` |

#### Events

| Event | Payload | Description |
|-------|---------|-------------|
| `connected` | `{ conversation_id, agent_output_audio_format?, ... }` | Session established |
| `disconnected` | `{ reason: 'agent' \| 'user' \| 'error' \| 'timeout', code? }` | Session ended |
| `transcript` | `{ text, is_final, event_id? }` | User speech transcription |
| `agent_response` | `{ text, event_id? }` | Complete agent text response |
| `agent_response_part` | `{ type: 'start' \| 'delta' \| 'stop', text? }` | Streaming text chunk |
| `agent_response_correction` | `{ original, corrected }` | Response correction |
| `audio` | `{ audio: ArrayBuffer, event_id, alignment? }` | Agent audio chunk |
| `interruption` | `{ event_id }` | Agent was interrupted |
| `error` | `{ type, message, code?, details? }` | Error occurred |
| `tool_call` | `{ tool_call_id, tool_name, parameters }` | Client tool invocation |
| `vad_score` | `{ score: number }` | Voice activity detection score |
| `status_change` | `{ status, previous }` | Connection status changed |
| `mode_change` | `{ mode, previous }` | Speaking/listening mode changed |
| `ping` | `{ event_id, ping_ms? }` | Server keepalive ping |
| `guardrail_triggered` | `{ guardrail_id, message }` | Safety guardrail fired |

### AudioCaptureManager

Captures microphone audio via Web Audio API AudioWorklet.

```typescript
const capture = new AudioCaptureManager({
  sampleRate: 16000,         // Target sample rate (default: 16000)
  echoCancellation: true,    // Echo cancellation (default: true)
  noiseSuppression: true,    // Noise suppression (default: true)
  autoGainControl: true,     // Auto gain control (default: true)
  channelCount: 1,           // Channel count (default: 1)
});

await capture.start((pcmChunk, volume) => {
  conversation.sendAudio(pcmChunk);
  console.log('Volume:', volume);
});

capture.setMuted(true);                    // Mute/unmute
const freq = capture.getByteFrequencyData(); // For visualization
const vol = capture.getVolume();             // Current volume level

capture.stop(); // Release resources
```

### AudioPlaybackManager

Plays audio chunks via Web Audio API AudioWorklet.

```typescript
const playback = new AudioPlaybackManager({
  sampleRate: 16000,          // Sample rate (default: 16000)
  encoding: 'pcm',           // 'pcm' or 'ulaw' (default: 'pcm')
  outputDeviceId: undefined,  // Specific output device
});

await playback.start(() => {
  console.log('Playback finished');
});

playback.enqueue(audioBuffer);  // Queue audio for playback
playback.interrupt();           // Flush buffer (on interruption)
playback.setVolume(0.8);        // Set volume (0-1)

const freq = playback.getByteFrequencyData(); // For visualization
const vol = playback.getVolume();              // Current output level

playback.stop(); // Release resources
```

### ConvAIApiClient

REST API client for agent management and TTS.

```typescript
const api = new ConvAIApiClient({
  baseUrl: 'https://echo-convai-gateway.bmcii1976.workers.dev',
  apiKey: 'your-key',
  timeout: 30000,
});
```

| Method | Description |
|--------|-------------|
| `createAgent(config)` | Create a new agent |
| `getAgent(id)` | Get agent by ID |
| `updateAgent(id, config)` | Update an agent |
| `deleteAgent(id)` | Delete an agent |
| `listAgents()` | List all agents |
| `getSignedUrl(agentId)` | Get pre-signed WebSocket URL |
| `listConversations(agentId?, limit?, offset?)` | List conversations |
| `getConversation(id)` | Get conversation by ID |
| `sendConversationFeedback(id, feedback)` | Send feedback |
| `textToSpeech(voiceId, text, options?)` | TTS (returns ArrayBuffer) |
| `textToSpeechStream(voiceId, text, options?)` | TTS streaming |
| `health()` | Gateway health check |

### Audio Utilities

```typescript
import {
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
  parseAudioFormat,
} from '@echo-prime/convai-client';
```

## Architecture

```
@echo-prime/convai-client
  src/
    index.ts          — Public API exports
    types.ts          — All TypeScript types and constants
    conversation.ts   — WebSocket conversation client
    audio.ts          — Browser audio capture/playback + PCM utilities
    api.ts            — REST API client
```

## Protocol Compatibility

This SDK implements the same WebSocket protocol as the ElevenLabs ConvAI system:

- `conversation_initiation_client_data` on connect
- `conversation_initiation_metadata` from server
- `user_audio_chunk` for audio input (base64 PCM)
- `user_message` for text input
- `ping`/`pong` keepalive
- `client_tool_call`/`client_tool_result` for client-side tools
- `interruption` handling with event ID tracking
- `contextual_update` for mid-conversation context injection
- WebSocket subprotocol `"convai"` with optional `"bearer.{token}"` auth

## License

MIT
