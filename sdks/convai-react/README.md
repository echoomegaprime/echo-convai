# @echo-prime/convai-react

React SDK for **Echo ConvAI** — drop-in conversational AI components and hooks for React applications.

## Installation

```bash
npm install @echo-prime/convai-react
```

**Peer dependencies:** React 18+

## Quick Start

```tsx
import {
  ConversationProvider,
  ConversationButton,
} from '@echo-prime/convai-react';

function App() {
  return (
    <ConversationProvider defaultServerUrl="wss://echo-convai.echo-op.com">
      <ConversationButton agentId="your-agent-id" size={72} />
    </ConversationProvider>
  );
}
```

## API Reference

### `<ConversationProvider>`

Root context provider. Wrap your app (or the conversation area) with this.

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `defaultServerUrl` | `string` | `wss://echo-convai.echo-op.com` | WebSocket server URL |
| `onAgentResponse` | `(text: string) => void` | — | Called on each complete agent response |
| `onUserTranscript` | `(text: string) => void` | — | Called when user speech is transcribed |
| `onSessionEnd` | `(id: string \| null) => void` | — | Called when the session ends |
| `onError` | `(error: string) => void` | — | Called on any error |

### `useConversation()`

Main hook — provides everything you need.

```tsx
const {
  startSession,   // (agentId, options?) => Promise<void>
  endSession,      // () => void
  sendMessage,     // (text) => void

  status,          // 'idle' | 'connecting' | 'connected' | 'speaking' | 'listening' | 'error'
  isConnected,     // boolean
  isSpeaking,      // boolean
  isListening,     // boolean

  transcript,      // string — latest user transcript
  agentResponse,   // string — latest/streaming agent response
  history,         // TranscriptEntry[] — full conversation history

  volume,          // number (0-1) — mic input volume
  isMuted,         // boolean
  toggleMute,      // () => void

  error,           // string | null
  conversationId,  // string | null
} = useConversation();
```

#### Session Options

```tsx
startSession('agent_id', {
  serverUrl: 'wss://custom-server.example.com',
  textOnly: true,           // No audio, text chat only
  userId: 'user_123',       // Track user across sessions
  dynamicVariables: {        // Injected into agent prompt
    userName: 'Alice',
    language: 'en',
  },
  configOverride: { ... },   // Override agent configuration
  extraBody: { ... },        // Custom LLM extra body
});
```

### `useAudio()`

Audio-only hook for building custom volume meters and mute controls.

```tsx
const { volume, isMuted, toggleMute } = useAudio();
```

### `<ConversationButton>`

Ready-made button with visual feedback.

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `agentId` | `string` | **required** | Agent to connect to |
| `sessionOptions` | `SessionOptions` | — | Options passed to `startSession` |
| `size` | `number` | `64` | Button diameter in pixels |
| `primaryColor` | `string` | `#6366F1` | Active state color |
| `idleColor` | `string` | `#374151` | Idle state color |
| `errorColor` | `string` | `#EF4444` | Error state color |
| `label` | `string` | `Tap to talk` | Text below button when idle |
| `showStatus` | `boolean` | `true` | Show status text below button |

**Visual states:**
- **Idle:** Microphone icon, dark background
- **Listening:** Stop icon, primary color, pulsing ring animation
- **Speaking:** Animated waveform bars

## Text-Only Mode

For chat interfaces without voice:

```tsx
const { startSession, sendMessage, agentResponse } = useConversation();

// Start in text-only mode (no mic permission needed)
await startSession('agent_id', { textOnly: true });

// Send text messages
sendMessage('Hello, how are you?');
```

## Protocol

This SDK communicates with Echo ConvAI servers over WebSocket using the ConvAI protocol. Audio is transmitted as base64-encoded 16-bit PCM at 16 kHz mono.

## License

MIT
