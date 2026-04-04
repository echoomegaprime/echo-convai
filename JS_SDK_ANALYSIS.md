# ElevenLabs JavaScript SDK Reverse Engineering Analysis

**Packages analyzed:**
- `@elevenlabs/client` v1.1.1 (589 KB packed, 2.8 MB unpacked, 149 files)
- `@elevenlabs/react` v1.0.2 (53.7 KB packed, 237 KB unpacked, 69 files)

**Date:** 2026-04-04

---

## 1. Architecture Overview

### Class Hierarchy
```
BaseConnection (abstract)
  ├── WebSocketConnection  — raw WebSocket to wss://api.elevenlabs.io
  └── WebRTCConnection     — LiveKit Room via livekit-client SDK

BaseConversation (abstract)
  ├── VoiceConversation    — full audio I/O (mic + speaker)
  └── TextConversation     — text-only, no audio pipeline

Conversation (namespace)
  └── startSession(options) — auto-selects Voice or Text based on textOnly flag
```

### Connection Selection Logic (ConnectionFactory.js)
```
1. signedUrl + connectionType="webrtc"  → ERROR (not supported)
2. Explicit connectionType provided     → use it
3. conversationToken provided           → webrtc
4. signedUrl provided                   → websocket
5. textOnly=true                        → websocket
6. Default (voice)                      → webrtc
```

**Key insight:** WebRTC (LiveKit) is now the DEFAULT for voice. WebSocket is legacy/fallback.

---

## 2. WebSocket Connection (Legacy Path)

### URL Construction
```
Origin:   wss://api.elevenlabs.io  (default, configurable)
Pathname: /v1/convai/conversation?agent_id={agentId}
Params:   &source=js_sdk&version=1.1.1
Optional: &environment={environment}

Full URL for signed URL mode:
  {signedUrl}&source=js_sdk&version=1.1.1

Protocol: "convai"
Auth:     WebSocket subprotocol "bearer.{authorization}" (added to protocols array)
```

### Connection Flow
1. Open WebSocket with URL + protocols `["convai", "bearer.{token}"]`
2. On `open`: send `conversation_initiation_client_data` message (overrides)
3. Wait for first `message` event with `type: "conversation_initiation_metadata"`
4. Extract from metadata: `conversation_id`, `agent_output_audio_format`, `user_input_audio_format`
5. Default input format: `pcm_16000` (if not specified by server)
6. Construct WebSocketConnection instance with socket + formats

### Error Handling
- Socket `error` → delayed disconnect (allows close event to fire first with better info)
- Socket `close` → code 1000 = "agent" disconnect, else "error" disconnect
- Close before metadata → `SessionConnectionError` with closeCode/closeReason

### Message Transport
- Send: `JSON.stringify(message)` directly on socket
- Receive: `JSON.parse(event.data)` → validate with `isValidSocketEvent` → dispatch

### Audio Data Transport (WebSocket mode)
- Input: `{ user_audio_chunk: base64EncodedPCM }` — sent as JSON message
- Output: audio events contain `audio_event.audio_base_64` — decoded and played through AudioWorklet

---

## 3. WebRTC Connection (LiveKit, Primary Path)

### LiveKit Configuration
```
Default LiveKit URL:  wss://livekit.rtc.elevenlabs.io
API Origin:           https://api.elevenlabs.io

Regional LiveKit URLs:
  us:            wss://livekit.rtc.elevenlabs.io
  eu-residency:  wss://livekit.rtc.eu.residency.elevenlabs.io
  in-residency:  wss://livekit.rtc.in.residency.elevenlabs.io
  global:        wss://livekit.rtc.elevenlabs.io

Regional API Origins:
  us:            wss://api.elevenlabs.io
  eu-residency:  wss://api.eu.residency.elevenlabs.io
  in-residency:  wss://api.in.residency.elevenlabs.io
  global:        wss://api.elevenlabs.io
```

### Token Acquisition Flow
```
Option A: Direct conversationToken provided → use as-is
Option B: agentId provided → fetch token from API:
  GET {origin}/v1/convai/conversation/token?agent_id={agentId}&source=js_sdk&version=1.1.1
  Optional: &environment={environment}
  Response: { token: "..." }
```

### Connection Flow
1. Create `Room` instance (from `livekit-client`)
2. Create WebRTCConnection with room
3. Set up room event listeners (Connected, Disconnected, DataReceived, TrackSubscribed, etc.)
4. On `SignalConnected` event: enable microphone (`setMicrophoneEnabled(true)`)
5. `room.connect(livekitUrl, conversationToken)`
6. Wait for `Connected` event
7. Extract conversationId from `room.name` (regex: `/(conv_[a-zA-Z0-9]+)/`)
8. Send `conversation_initiation_client_data` overrides via `publishData`

### Audio Format
```
Input format:  pcm_48000 (hardcoded for WebRTC)
Output format: pcm_48000 (hardcoded for WebRTC)
```

### Audio Tracks
- **Input:** LiveKit microphone track (enabled on SignalConnected)
  - Audio constraints: echoCancellation, noiseSuppression, autoGainControl, channelCount=1
- **Output:** Remote agent audio track received via `TrackSubscribed` event
  - Attached to hidden `<audio>` element appended to `document.body`
  - Supports `setSinkId` for output device selection
  - Audio capture chain set up for onAudio callback

### Data Messages (via LiveKit DataChannel)
- Send: `room.localParticipant.publishData(encoded, { reliable: true })`
- Receive: `RoomEvent.DataReceived` → JSON parse → dispatch
- Audio messages (type "audio") are FILTERED OUT — audio flows through WebRTC tracks

### Mode Detection
- `ActiveSpeakersChanged` event → check if speaker identity starts with "agent"
  - Agent speaking → mode "speaking"
  - No speakers → mode "listening"

### Agent Disconnect Detection
- `ParticipantDisconnected` → if identity starts with "agent" → disconnect
- `ConnectionStateChanged` → if Disconnected → disconnect

### Microphone Muting (WebRTC)
- Primary: `track.mute()` / `track.unmute()` (LiveKit built-in)
- Fallback: `localParticipant.setMicrophoneEnabled(!isMuted)`

### Output Audio Capture (for onAudio callback)
1. Create AudioContext
2. Create AnalyserNode (fftSize=2048, smoothingTimeConstant=0.8)
3. Create MediaStream from remote track's mediaStreamTrack
4. Create MediaStreamSource → connect to AnalyserNode
5. Load rawAudioProcessor worklet
6. Create AudioWorkletNode → configure with output format
7. Process audio chunks, convert to base64, emit as audio events
8. Volume threshold: 0.01 (skip silence)

---

## 4. Audio Processing Pipeline

### Input Audio (MediaDeviceInput — WebSocket path)

**Microphone constraints:**
```javascript
{
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: { ideal: 1 },
  voiceIsolation: true,  // added at getUserMedia call
  sampleRate: { ideal: targetSampleRate }
}
```

**iOS headphone detection:** Searches for devices with labels containing "airpod", "headphone", "earphone" (avoids "bluetooth" to prevent triggering bluetooth speakers).

**Audio chain:**
```
getUserMedia → MediaStreamSource → AnalyserNode → AudioWorkletNode(rawAudioProcessor)
```

**Resampling:** If browser doesn't support AudioContext sampleRate constraint, loads libsamplerate WASM module from CDN:
```
https://cdn.jsdelivr.net/npm/@alexanderolsen/libsamplerate-js@2.1.2/dist/libsamplerate.worklet.js
```

### rawAudioProcessor (AudioWorklet)

**Buffer size:** `sampleRate / 10` samples (100ms chunks)

**Encoding pipeline:**
1. Collect Float32 samples into buffer
2. When buffer full:
   - If muted: zero-fill
   - If PCM: convert to Int16 (sample * 32768/32767)
   - If ulaw: encode using wavefile-style mu-law encoding
3. Calculate RMS volume: `sqrt(sum(x^2) / n)`
4. Post message: `[encodedArray, maxVolume]`

**mu-law encoding constants:**
- BIAS: 0x84
- CLIP: 32635
- Uses 256-entry encodeTable for fast lookup

### Output Audio (MediaDeviceOutput — WebSocket path)

**Audio chain:**
```
AudioWorkletNode(audioConcatProcessor) → GainNode → AnalyserNode → MediaStreamDestination → <audio> element
```

**Output device selection:** Uses `<audio>.setSinkId(deviceId)` for output device routing.

### audioConcatProcessor (AudioWorklet)

**Playback buffer:** Queue of Int16/Uint8 arrays

**Operations:**
- `buffer`: Push new audio chunk to queue
- `interrupt`: Set interrupted flag
- `clearInterrupted`: Clear interrupted flag + flush buffers

**Decoding:**
- PCM: Int16 → Float32 (divide by 32768)
- ulaw: Decode using decodeTable, then to Float32

**Playback status:** Posts `{ type: "process", finished: boolean }` — used by VoiceConversation to track speaking/listening mode.

**Interrupt behavior:**
- Sets `interrupted = true` on output
- Sends "interrupt" to worklet to flush queued buffers
- Exponential gain ramp to 0.0001 over `resetDuration` ms (default 2000ms)
- After fade: restore volume, clear interrupted flag

### scribeAudioProcessor (for Scribe/STT)

**Buffer size:** 4096 samples (fixed)
**Resampling:** Linear interpolation when input/output sample rates differ
**Output:** PCM16 Int16Array, sent as transferable ArrayBuffer

---

## 5. WebSocket Message Schemas

### Client → Server (Outgoing)

```typescript
// Initial configuration
{
  type: "conversation_initiation_client_data",
  conversation_config_override?: {
    agent: { prompt?: { prompt?: string }, first_message?: string, language?: string },
    tts: { voice_id?: string, speed?: number, stability?: number, similarity_boost?: number },
    conversation: { text_only?: boolean }
  },
  custom_llm_extra_body?: unknown,
  dynamic_variables?: Record<string, string | number | boolean>,
  user_id?: string,
  source_info: { source: "js_sdk", version: "1.1.1" },
  tool_mock_config?: {
    mocking_strategy: "none" | "all" | "selected",
    mocked_tool_names?: string[],
    fallback_strategy: "raise_error" | "call_real_tool"
  }
}

// Audio data (WebSocket mode only)
{ user_audio_chunk: string }  // base64 encoded PCM/ulaw

// Pong (heartbeat response)
{ type: "pong", event_id: number }

// Feedback
{ type: "feedback", score: "like" | "dislike", event_id: number }

// Client tool result
{
  type: "client_tool_result",
  tool_call_id: string,
  result: string,
  is_error: boolean
}

// Contextual update (inject context mid-conversation)
{ type: "contextual_update", text: string }

// User text message
{ type: "user_message", text: string }

// User activity signal
{ type: "user_activity" }

// MCP tool approval
{ type: "mcp_tool_approval_result", tool_call_id: string, is_approved: boolean }

// Multimodal message
{
  type: "multimodal_message",
  text?: { type: "user_message", text: string },
  file?: { type: "file_input", file_id: string }
}
```

### Server → Client (Incoming)

```typescript
// All events have a `type` field. Known types:

"conversation_initiation_metadata"  // First message, contains config
"audio"                             // Audio chunk: { audio_event: { audio_base_64, event_id, alignment? } }
"agent_response"                    // Agent text: { agent_response_event: { agent_response, event_id } }
"user_transcript"                   // User STT: { user_transcription_event: { user_transcript, event_id } }
"interruption"                      // Agent interrupted: { interruption_event: { event_id } }
"internal_tentative_agent_response" // Debug: { tentative_agent_response_internal_event: { tentative_agent_response } }
"ping"                              // Heartbeat: { ping_event: { event_id, ping_ms? } }
"client_tool_call"                  // Tool invocation: { client_tool_call: { tool_name, parameters, tool_call_id } }
"vad_score"                         // VAD: { vad_score_event: { vad_score } }
"mcp_tool_call"                     // MCP tool: { mcp_tool_call: {...} }
"mcp_connection_status"             // MCP status: { mcp_connection_status: {...} }
"agent_tool_request"                // Server tool request: { agent_tool_request: {...} }
"agent_tool_response"               // Server tool response: { agent_tool_response: { tool_name, ... } }
"asr_initiation_metadata"           // ASR config: { asr_initiation_metadata_event: {...} }
"agent_chat_response_part"          // Streaming text: { text_response_part: {...} }
"guardrail_triggered"               // Safety guardrail fired
"error"                             // Error: { error_event: { error_type, message, reason, code, debug_message, details } }
```

### Error Types
- `max_duration_exceeded` — triggers session end
- All others — logged as errors, do NOT end session

### Special: end_call Tool
When `agent_tool_response.tool_name === "end_call"`, the session is ended with reason "agent".

---

## 6. Constants and Configuration

### Sample Rates
```
WebSocket default input:  pcm_16000 (negotiated from server)
WebSocket output:         negotiated from server (agent_output_audio_format)
WebRTC fixed:             pcm_48000 (both directions)
Scribe target:            16000 (fixed for STT)
```

### Audio Worklet Buffer Sizes
```
rawAudioProcessor:     sampleRate / 10 samples (100ms chunks)
audioConcatProcessor:  no fixed buffer size (queue-based)
scribeAudioProcessor:  4096 samples
```

### Audio Analysis
```
AnalyserNode fftSize:           2048
smoothingTimeConstant:          0.8 (WebRTC output analyser)
Volume threshold (WebRTC):      0.01
```

### Timing
```
Android connection delay:       3000ms (default)
iOS connection delay:           0ms (default)
Default connection delay:       0ms
Interrupt fade duration:        2000ms (exponential ramp to 0.0001)
```

### External Dependencies
```
LiveKit:         livekit-client (Room, RoomEvent, Track, ConnectionState, createLocalAudioTrack)
libsamplerate:   https://cdn.jsdelivr.net/npm/@alexanderolsen/libsamplerate-js@2.1.2/dist/libsamplerate.worklet.js
Types:           @elevenlabs/types (all message type definitions)
```

### Supported Audio Formats
```
PCM:  "pcm" — signed 16-bit linear PCM
ulaw: "ulaw" — mu-law encoded 8-bit
Format string pattern: "{format}_{sampleRate}" e.g. "pcm_16000", "ulaw_8000"
```

### Scribe Audio Formats (STT)
```
PCM_8000, PCM_16000, PCM_22050, PCM_24000, PCM_44100, PCM_48000
ULAW_8000
```

### Scribe VAD Parameters
```
vadSilenceThresholdSecs:  0.3 - 3.0
vadThreshold:             0.1 - 0.9
minSpeechDurationMs:      50 - 2000
minSilenceDurationMs:     50 - 2000
commitStrategy:           "manual" | "vad"
```

---

## 7. Authentication Flows

### Public Agent (no auth)
```
WebSocket: wss://api.elevenlabs.io/v1/convai/conversation?agent_id={id}
WebRTC:    GET https://api.elevenlabs.io/v1/convai/conversation/token?agent_id={id}
           → { token: "..." } → room.connect(livekitUrl, token)
```

### Signed URL (WebSocket only)
```
WebSocket: {signedUrl}&source=js_sdk&version=1.1.1
Protocols: ["convai"]
```

### Authorization Header (WebSocket)
```
WebSocket: standard URL
Protocols: ["convai", "bearer.{authorization}"]
```
Note: Auth is passed as a WebSocket subprotocol, not an HTTP header.

### Conversation Token (WebRTC only)
```
WebRTC: Direct token provided → room.connect(livekitUrl, conversationToken)
```

### Scribe Authentication
```
WebSocket: wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id={id}&token={token}
```

### Post-Session Feedback
```
POST https://api.elevenlabs.io/v1/convai/conversations/{conversationId}/feedback
Body: { feedback: "like"|"dislike" } or { rating: number, comment: string }
```

---

## 8. Feature Detection

```javascript
// iOS detection
["iPad Simulator", "iPhone Simulator", "iPod Simulator", "iPad", "iPhone", "iPod"]
  .includes(navigator.platform)
|| (navigator.userAgent.includes("Mac") && "ontouchend" in document)

// Android detection
/android/i.test(navigator.userAgent)

// Sample rate constraint support
navigator.mediaDevices.getSupportedConstraints().sampleRate

// Output device selection support
"setSinkId" in HTMLAudioElement.prototype

// Wake Lock support
"wakeLock" in navigator

// AudioWorklet loading strategy (CSP compatibility):
// 1. Try Blob URL
// 2. If fails (Safari iframe CORS): try base64 data: URL
// 3. If fails: throw error suggesting self-hosting
```

---

## 9. React SDK (@elevenlabs/react)

### Provider Architecture
```
ConversationProvider (root)
  ├── ConversationContext          — raw conversation instance + startSession/endSession
  ├── ConversationControlsProvider — stable action references (sendMessage, setVolume, etc.)
  ├── ConversationStatusProvider   — status + error message state
  ├── ConversationInputProvider    — isMuted + setMuted (controlled/uncontrolled)
  ├── ConversationModeProvider     — mode (speaking/listening) + convenience booleans
  ├── ConversationFeedbackProvider — canSendFeedback + sendFeedback
  └── ConversationClientToolsProvider — client tool registration
```

### ConversationProvider Props
```typescript
{
  children: ReactNode,
  isMuted?: boolean,           // controlled mute state
  onMutedChange?: (muted: boolean) => void,
  // ...plus all HookOptions (session config + callbacks)
}
```

### useConversation Hook (convenience, combines all)
```typescript
const {
  // Controls
  startSession,     // (options?) => void
  endSession,       // () => void
  sendUserMessage,  // (text) => void
  sendMultimodalMessage,  // ({text?, fileId?}) => void
  sendContextualUpdate,   // (text) => void
  sendUserActivity,       // () => void
  sendMCPToolApprovalResult, // (toolCallId, isApproved) => void
  setVolume,        // ({volume}) => void
  changeInputDevice,   // (config) => Promise<void>
  changeOutputDevice,  // (config) => Promise<void>
  getInputByteFrequencyData,  // () => Uint8Array
  getOutputByteFrequencyData, // () => Uint8Array
  getInputVolume,    // () => number
  getOutputVolume,   // () => number
  getId,             // () => string

  // State
  status,           // "disconnected" | "connecting" | "connected" | "error"
  message,          // string | undefined (error message)
  isMuted,          // boolean
  setMuted,         // (muted: boolean) => void
  mode,             // "speaking" | "listening"
  isSpeaking,       // boolean
  isListening,      // boolean
  canSendFeedback,  // boolean
  sendFeedback,     // (like: boolean) => void
} = useConversation(options?)
```

### Granular Hooks (for optimized re-renders)
```typescript
useConversationControls()   → stable action references (never re-render)
useConversationStatus()     → { status, message }
useConversationInput()      → { isMuted, setMuted }
useConversationMode()       → { mode, isSpeaking, isListening }
useConversationFeedback()   → { canSendFeedback, sendFeedback }
useRawConversation()        → BaseConversation | null (escape hatch)
useRawConversationRef()     → RefObject<BaseConversation | null>
useRegisterCallbacks(cbs)   → register event listeners
useConversationClientTool(name, handler) → register a client tool
```

### State Machine (Conversation Lifecycle)
```
disconnected → connecting → connected → disconnecting → disconnected
                    ↓
                  error (on socket/connection error during session)

Status transitions:
  startSession() called → "connecting"
  Connection established → "connected"
  endSession() called   → "disconnecting" → "disconnected"
  Error during session  → "error" (with message)
  Note: "disconnecting" is transient — React provider skips it

Mode transitions:
  Default / disconnect     → "listening"
  Agent audio playing      → "speaking"
  Interruption received    → "listening"
  audioConcatProcessor finished → "listening"
  ActiveSpeakers has agent → "speaking" (WebRTC)
  No active speakers       → "listening" (WebRTC)
```

### Callback Composition (ListenerMap)
The provider uses a `ListenerMap` that allows multiple listeners per callback key. When `compose()` is called, it creates a single function per key that delegates to all registered listeners. This allows:
- Provider-level callbacks (from props)
- Hook-level callbacks (from useConversation)
- Component-level callbacks (from useRegisterCallbacks)

All fire in registration order.

### Client Tool Registration
Tools can be registered via:
1. `clientTools` prop on startSession options
2. `useConversationClientTool(name, handler)` hook (per-component)

Hook-registered tools are kept in a `clientToolsRegistry` Map that survives across sessions. Conflicts between option-provided and hook-registered tools throw errors.

### Server Location Resolution
```typescript
const resolvedServerLocation = parseLocation(options?.serverLocation || defaults?.serverLocation);
const origin = getOriginForLocation(resolvedServerLocation);
const calculatedLivekitUrl = getLivekitUrlForLocation(resolvedServerLocation);
// These are merged into session options
```

---

## 10. Scribe (Real-Time STT) Module

### WebSocket URL
```
wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id={id}&token={token}
Optional params: commit_strategy, audio_format, vad_silence_threshold_secs,
                 vad_threshold, min_speech_duration_ms, min_silence_duration_ms,
                 language_code, include_timestamps
```

### Client → Server Messages
```json
{
  "message_type": "input_audio_chunk",
  "audio_base_64": "...",
  "commit": false,
  "sample_rate": 16000,
  "previous_text": "optional context"
}
```

### Server → Client Messages
```
session_started, partial_transcript, committed_transcript,
committed_transcript_with_timestamps, auth_error, error,
quota_exceeded, commit_throttled, transcriber_error,
unaccepted_terms, rate_limited, input_error, queue_overflow,
resource_exhausted, session_time_limit_exceeded,
chunk_size_exceeded, insufficient_audio_activity
```

### Microphone Streaming (Scribe)
- Target sample rate: 16000 Hz
- Uses ScribeAudioProcessor worklet
- Linear interpolation resampling for browsers that don't support sampleRate constraint
- Buffer: 4096 samples per chunk
- Output: PCM16 Int16Array, converted to base64 for WebSocket

---

## 11. Key Implementation Details for Reverse Engineering

### What We Need for echo-convai (Node.js/Server-Side)

**WebSocket mode (most relevant for server-side):**
1. Connect to `wss://api.elevenlabs.io/v1/convai/conversation?agent_id={id}`
2. Send overrides on open
3. Wait for `conversation_initiation_metadata` (get formats + conversation_id)
4. Send audio as `{ user_audio_chunk: base64 }` JSON messages
5. Receive audio as `{ type: "audio", audio_event: { audio_base_64, event_id } }`
6. Handle ping/pong keepalive
7. Handle interruptions (stop playing, skip old audio)
8. Handle client tool calls (invoke tool, send result back)
9. Close with code 1000

**WebRTC mode (more complex, browser-oriented):**
1. Fetch conversation token from API
2. Use LiveKit SDK to connect to room
3. Audio flows through WebRTC tracks (not data channel)
4. Data messages (text, tools, etc.) flow through LiveKit DataChannel
5. Mode detection via ActiveSpeakersChanged event

### Audio Encoding for Server-Side
```python
# PCM16 encoding (what rawAudioProcessor does)
sample = max(-1.0, min(1.0, float32_sample))
pcm16 = int(sample * 32768) if sample < 0 else int(sample * 32767)

# Base64 encoding
audio_b64 = base64.b64encode(pcm16_bytes).decode()

# Send format
message = json.dumps({"user_audio_chunk": audio_b64})
```

### Interrupt Handling Logic
```
On interruption event:
  1. Record lastInterruptTimestamp = event.interruption_event.event_id
  2. Switch mode to "listening"
  3. Flush output audio buffer
  4. For subsequent audio events: skip if event_id <= lastInterruptTimestamp
```

### Feedback System
```
- currentEventId tracks latest audio event_id
- lastFeedbackEventId tracks last feedback sent
- canSendFeedback = (currentEventId !== lastFeedbackEventId)
- Reset on disconnect
```

### Wake Lock
- Requested on session start (keeps screen awake)
- Re-acquired on visibility change (tab switching releases it)
- Released on session end

---

## 12. File Index

### @elevenlabs/client Source Files
| File | Purpose | Lines |
|------|---------|-------|
| `BaseConversation.js` | Abstract conversation class, message dispatch | 411 |
| `VoiceConversation.js` | Voice conversation with audio I/O | 227 |
| `TextConversation.js` | Text-only conversation | 54 |
| `utils/WebRTCConnection.js` | LiveKit-based WebRTC connection | 497 |
| `utils/WebSocketConnection.js` | Raw WebSocket connection | 158 |
| `utils/BaseConnection.js` | Abstract connection base class | 73 |
| `utils/ConnectionFactory.js` | Connection type auto-selection | 37 |
| `utils/input.js` | MediaDeviceInput (mic capture) | 178 |
| `utils/output.js` | MediaDeviceOutput (speaker playback) | 135 |
| `utils/rawAudioProcessor.generated.js` | Input audio worklet (PCM/ulaw encode) | 129 |
| `utils/audioConcatProcessor.generated.js` | Output audio worklet (PCM/ulaw decode) | 109 |
| `utils/scribeAudioProcessor.generated.js` | Scribe STT audio worklet | 111 |
| `utils/overrides.js` | Construct override config message | 47 |
| `utils/events.js` | Event type validation | 4 |
| `utils/audio.js` | Base64 ↔ ArrayBuffer conversion | 16 |
| `utils/compatibility.js` | iOS/Android detection | 16 |
| `utils/location.js` | Regional URL mapping | 31 |
| `utils/errors.js` | SessionConnectionError class | 11 |
| `utils/createWorkletModuleLoader.js` | Worklet loading with CSP fallbacks | 43 |
| `utils/addLibsamplerateModule.js` | libsamplerate CDN loader | 7 |
| `utils/applyDelay.js` | Platform-specific connection delay | 18 |
| `utils/mergeOptions.js` | Deep merge with function composition | 37 |
| `utils/postOverallFeedback.js` | POST feedback to API | 19 |
| `utils/attachInputToConnection.js` | Wire mic → WebSocket | 16 |
| `utils/attachConnectionToOutput.js` | Wire WebSocket → speaker | 11 |
| `platform/VoiceSessionSetup.js` | Platform strategy (web default) | 74 |
| `scribe/connection.js` | Scribe WebSocket connection | 362 |
| `scribe/scribe.js` | ScribeRealtime class | 193 |
| `sourceInfo.js` | SDK source/version info | 9 |
| `version.js` | Package version "1.1.1" | 3 |

### @elevenlabs/react Source Files
| File | Purpose | Lines |
|------|---------|-------|
| `ConversationProvider.js` | Root provider, session management | 126 |
| `ConversationContext.js` | React context + hooks | 61 |
| `ConversationControls.js` | Stable action references | 119 |
| `ConversationStatus.js` | Status state management | 44 |
| `ConversationInput.js` | Mic mute state (controlled/uncontrolled) | 55 |
| `ConversationMode.js` | Speaking/listening mode state | 40 |
| `ConversationFeedback.js` | Feedback state + actions | 44 |
| `ConversationClientTools.js` | Client tool registration | 87 |
| `useConversation.js` | Convenience hook combining all | 76 |
| `ListenerMap.js` | Multi-listener callback composition | 63 |
| `useStableCallbacks.js` | Ref-stable callback wrapper | ~30 |
| `types.d.ts` | Type definitions | 9 |
