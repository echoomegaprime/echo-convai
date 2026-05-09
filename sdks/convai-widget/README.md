# @echo-prime/convai-widget

Embeddable conversational AI widget for any website. Drop in two lines of code to add voice + text AI conversations. Zero dependencies, Shadow DOM isolation, three layout variants.

## Installation

### Script Tag (recommended for most sites)

```html
<script src="https://cdn.echo-ept.com/convai-widget.js"></script>
<script>
  EchoConvAI.init({
    agentId: 'your-agent-id',
    serverUrl: 'wss://echo-convai.echo-op.com',
  });
</script>
```

### npm

```bash
npm install @echo-prime/convai-widget
```

```js
import { init } from '@echo-prime/convai-widget';

const widget = init({
  agentId: 'your-agent-id',
  serverUrl: 'wss://echo-convai.echo-op.com',
});
```

### Data Attributes (auto-init)

```html
<script
  src="https://cdn.echo-ept.com/convai-widget.js"
  data-echo-agent-id="your-agent-id"
  data-echo-variant="expandable"
  data-echo-title="AI Assistant"
></script>
```

## Variants

### `compact` — Circle button

A floating circle button. Click to start/stop voice conversation. Shows waveform animation when the agent is speaking, pulse animation when listening.

### `full` — Chat panel

A full chat panel with:
- Agent avatar and status header
- Scrollable transcript area
- Text input field
- Microphone toggle button
- Send button

### `expandable` — Button that opens into panel (default)

A compact button that expands into the full chat panel when clicked. Collapses back to button when the close button is pressed. Best for most use cases.

## Configuration

```js
EchoConvAI.init({
  // Required
  agentId: 'your-agent-id',

  // Connection
  serverUrl: 'wss://echo-convai.echo-op.com',  // default

  // Layout
  variant: 'expandable',  // 'compact' | 'full' | 'expandable'
  position: { bottom: 24, right: 24 },  // pixel offsets

  // Appearance
  title: 'Echo AI',
  greeting: 'Hello! How can I help?',
  avatar: 'https://example.com/avatar.png',  // URL or omit for default icon
  styles: {
    primaryColor: '#6366F1',
    backgroundColor: '#1F2937',
    textColor: '#F9FAFB',
    mutedTextColor: '#9CA3AF',
    borderRadius: 16,
    fontFamily: 'system-ui, sans-serif',
  },

  // Behavior
  textOnly: false,     // Disable voice, text chat only
  userId: 'user_123',  // User tracking

  // Agent config
  dynamicVariables: {
    userName: 'Alice',
    language: 'en',
  },

  // Callbacks
  onStatusChange: (status) => console.log('Status:', status),
  onAgentResponse: (text) => console.log('Agent:', text),
  onUserTranscript: (text) => console.log('User:', text),
  onError: (error) => console.error('Error:', error),
});
```

## Programmatic Control

```js
const widget = EchoConvAI.init({ agentId: 'xxx' });

// Send text message
widget.sendText('Hello!');

// Toggle microphone
widget.toggleMute();

// Connect/disconnect
widget.connect();
widget.disconnect();

// Remove from page
widget.unmount();

// Destroy all instances
EchoConvAI.destroy();
```

## Status Values

| Status | Description |
|--------|-------------|
| `idle` | Not connected |
| `connecting` | WebSocket connecting |
| `connected` | Connected, waiting for activity |
| `listening` | Listening for user speech |
| `speaking` | Agent is speaking |
| `error` | Connection or mic error |

## Shadow DOM

All widget styles are fully encapsulated in Shadow DOM. The widget will not affect your page's styles and your page's styles will not affect the widget.

## Async Loading

For async script loading, you can queue init calls before the script loads:

```html
<script>
  window.EchoConvAI = { _queue: [{ agentId: 'xxx' }] };
</script>
<script src="https://cdn.echo-ept.com/convai-widget.js" async></script>
```

## Browser Support

- Chrome/Edge 66+
- Firefox 76+
- Safari 14.1+

Requires: WebSocket, Shadow DOM, AudioContext, getUserMedia (for voice).

## License

MIT
