/**
 * widget.ts — Core widget class for Echo ConvAI embeddable widget.
 *
 * Supports three variants:
 * - 'compact': Circle button that starts/stops voice conversation
 * - 'full': Full chat panel with transcript, text input, and voice
 * - 'expandable': Compact button that expands into full panel on click
 *
 * All rendering happens inside Shadow DOM to isolate styles.
 */

import { generateStyles, DEFAULT_THEME, type WidgetTheme } from './styles';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type WidgetVariant = 'compact' | 'full' | 'expandable';

export type WidgetStatus =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'speaking'
  | 'listening'
  | 'error';

export interface WidgetConfig {
  agentId: string;
  serverUrl?: string;
  variant?: WidgetVariant;
  styles?: Partial<WidgetTheme>;
  position?: { bottom?: number; right?: number; left?: number; top?: number };
  avatar?: string;
  title?: string;
  greeting?: string;
  textOnly?: boolean;
  userId?: string;
  dynamicVariables?: Record<string, string | number | boolean>;
  onStatusChange?: (status: WidgetStatus) => void;
  onAgentResponse?: (text: string) => void;
  onUserTranscript?: (text: string) => void;
  onError?: (error: string) => void;
}

interface TranscriptEntry {
  role: 'user' | 'agent';
  text: string;
  isStreaming: boolean;
}

interface ServerMessage {
  type: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// SVG Icons
// ---------------------------------------------------------------------------

const ICON_MIC = `<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 1C10.34 1 9 2.34 9 4V12C9 13.66 10.34 15 12 15C13.66 15 15 13.66 15 12V4C15 2.34 13.66 1 12 1Z" fill="currentColor"/><path d="M17 12C17 14.76 14.76 17 12 17C9.24 17 7 14.76 7 12H5C5 15.53 7.61 18.43 11 18.92V22H13V18.92C16.39 18.43 19 15.53 19 12H17Z" fill="currentColor"/></svg>`;
const ICON_MIC_OFF = `<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 1C10.34 1 9 2.34 9 4V12C9 13.66 10.34 15 12 15C13.66 15 15 13.66 15 12V4C15 2.34 13.66 1 12 1Z" fill="currentColor" opacity="0.4"/><path d="M17 12C17 14.76 14.76 17 12 17C9.24 17 7 14.76 7 12H5C5 15.53 7.61 18.43 11 18.92V22H13V18.92C16.39 18.43 19 15.53 19 12H17Z" fill="currentColor" opacity="0.4"/><line x1="3" y1="3" x2="21" y2="21" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
const ICON_STOP = `<svg width="24" height="24" viewBox="0 0 24 24" fill="none"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>`;
const ICON_CLOSE = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M18 6L6 18M6 6L18 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
const ICON_SEND = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M2 21L23 12L2 3V10L17 12L2 14V21Z" fill="currentColor"/></svg>`;
const ICON_CHAT = `<svg width="28" height="28" viewBox="0 0 24 24" fill="none"><path d="M20 2H4C2.9 2 2 2.9 2 4V22L6 18H20C21.1 18 22 17.1 22 16V4C22 2.9 21.1 2 20 2Z" fill="currentColor"/></svg>`;

// ---------------------------------------------------------------------------
// Audio helpers
// ---------------------------------------------------------------------------

const SAMPLE_RATE = 16_000;
const BUFFER_SIZE = 4000;

function float32ToPcm16Base64(samples: Float32Array): string {
  const buf = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  const bytes = new Uint8Array(buf);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function base64ToPcm16Float32(b64: string): Float32Array {
  const raw = atob(b64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  const view = new DataView(bytes.buffer);
  const samples = new Float32Array(bytes.length / 2);
  for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
  return samples;
}

// ---------------------------------------------------------------------------
// Widget Class
// ---------------------------------------------------------------------------

export class ConvAIWidget {
  private config: Required<
    Pick<WidgetConfig, 'agentId' | 'serverUrl' | 'variant' | 'title' | 'greeting'>
  > & WidgetConfig;
  private theme: WidgetTheme;
  private host: HTMLElement | null = null;
  private shadow: ShadowRoot | null = null;
  private container: HTMLElement | null = null;

  // Connection
  private ws: WebSocket | null = null;
  private status: WidgetStatus = 'idle';
  private conversationId: string | null = null;
  private lastInterruptId = -1;

  // Audio capture
  private mediaStream: MediaStream | null = null;
  private audioCtx: AudioContext | null = null;
  private processor: ScriptProcessorNode | null = null;
  private isMuted = false;

  // Audio playback
  private outputCtx: AudioContext | null = null;
  private playQueue: Float32Array[] = [];
  private isPlaying = false;

  // Transcript
  private transcript: TranscriptEntry[] = [];
  private currentAgentText = '';
  private isAgentStreaming = false;

  // UI state
  private isExpanded = false;

  constructor(config: WidgetConfig) {
    this.config = {
      serverUrl: 'wss://echo-convai.echo-op.com',
      variant: 'expandable',
      title: 'Echo AI',
      greeting: 'Hello! How can I help you today?',
      ...config,
    };
    this.theme = { ...DEFAULT_THEME, ...config.styles };
  }

  // ---- Public API ----

  mount(target?: HTMLElement): void {
    const parent = target ?? document.body;

    this.host = document.createElement('div');
    this.host.id = 'echo-convai-widget';
    parent.appendChild(this.host);

    this.shadow = this.host.attachShadow({ mode: 'open' });

    const styleEl = document.createElement('style');
    styleEl.textContent = generateStyles(this.theme);
    this.shadow.appendChild(styleEl);

    this.container = document.createElement('div');
    this.shadow.appendChild(this.container);

    this.render();
  }

  unmount(): void {
    this.disconnect();
    if (this.host?.parentNode) {
      this.host.parentNode.removeChild(this.host);
    }
    this.host = null;
    this.shadow = null;
    this.container = null;
  }

  connect(): void {
    if (this.ws) return;
    this.setStatus('connecting');

    const url = `${this.config.serverUrl}/v1/convai/conversation?agent_id=${encodeURIComponent(this.config.agentId)}&source=js_widget&version=1.0.0`;
    this.ws = new WebSocket(url, ['convai']);

    this.ws.onopen = () => {
      const initMsg: Record<string, unknown> = {
        type: 'conversation_initiation_client_data',
        source_info: { source: 'echo_convai_widget', version: '1.0.0' },
      };
      if (this.config.textOnly) {
        initMsg.conversation_config_override = { text_only: true };
      }
      if (this.config.dynamicVariables) {
        initMsg.dynamic_variables = this.config.dynamicVariables;
      }
      if (this.config.userId) {
        initMsg.user_id = this.config.userId;
      }
      this.ws!.send(JSON.stringify(initMsg));

      if (!this.config.textOnly) {
        this.startMicCapture();
      }
    };

    this.ws.onmessage = (event) => {
      try {
        this.handleMessage(JSON.parse(event.data as string) as ServerMessage);
      } catch { /* ignore */ }
    };

    this.ws.onerror = () => {};

    this.ws.onclose = (event) => {
      this.cleanupAudio();
      this.ws = null;
      if (event.code === 1000) {
        this.setStatus('idle');
      } else {
        const msg = event.reason || `Disconnected (code ${event.code})`;
        this.setStatus('error');
        this.config.onError?.(msg);
      }
    };
  }

  disconnect(): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.close(1000, 'client_ended');
    }
    this.cleanupAudio();
    this.ws = null;
    this.setStatus('idle');
    this.currentAgentText = '';
    this.isAgentStreaming = false;
  }

  sendText(text: string): void {
    if (!text.trim()) return;
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'user_message', text }));
      this.transcript.push({ role: 'user', text, isStreaming: false });
      this.renderTranscript();
    }
  }

  toggleMute(): void {
    this.isMuted = !this.isMuted;
    this.render();
  }

  // ---- Status ----

  private setStatus(s: WidgetStatus): void {
    this.status = s;
    this.config.onStatusChange?.(s);
    this.render();
  }

  private get isActive(): boolean {
    return (
      this.status === 'connected' ||
      this.status === 'speaking' ||
      this.status === 'listening'
    );
  }

  // ---- WebSocket message handler ----

  private handleMessage(data: ServerMessage): void {
    switch (data.type) {
      case 'conversation_initiation_metadata': {
        const meta = data.conversation_initiation_metadata_event as Record<string, unknown> | undefined;
        if (meta?.conversation_id) {
          this.conversationId = meta.conversation_id as string;
        }
        this.setStatus('listening');
        break;
      }

      case 'audio': {
        const evt = data.audio_event as { audio_base_64?: string; event_id?: number | string } | undefined;
        if (!evt?.audio_base_64) break;
        const eventId = typeof evt.event_id === 'string' ? parseInt(evt.event_id, 10) : (evt.event_id ?? 0);
        if (eventId <= this.lastInterruptId) break;
        this.enqueueAudio(evt.audio_base_64);
        break;
      }

      case 'agent_response': {
        const evt = data.agent_response_event as { agent_response?: string } | undefined;
        const text = evt?.agent_response ?? '';
        this.currentAgentText = text;
        this.isAgentStreaming = false;
        this.transcript.push({ role: 'agent', text, isStreaming: false });
        this.renderTranscript();
        this.config.onAgentResponse?.(text);
        break;
      }

      case 'agent_chat_response_part': {
        const part = data.text_response_part as { type?: string; text?: string } | undefined;
        if (part?.type === 'start') {
          this.currentAgentText = '';
          this.isAgentStreaming = true;
        } else if (part?.type === 'delta' && part.text) {
          this.currentAgentText += part.text;
          this.isAgentStreaming = true;
        } else if (part?.type === 'stop') {
          this.isAgentStreaming = false;
          if (this.currentAgentText) {
            this.transcript.push({ role: 'agent', text: this.currentAgentText, isStreaming: false });
          }
        }
        this.renderTranscript();
        break;
      }

      case 'user_transcript': {
        const evt = data.user_transcription_event as { user_transcript?: string } | undefined;
        const text = evt?.user_transcript ?? '';
        this.transcript.push({ role: 'user', text, isStreaming: false });
        this.renderTranscript();
        this.config.onUserTranscript?.(text);
        break;
      }

      case 'interruption': {
        const evt = data.interruption_event as { event_id?: number | string } | undefined;
        if (evt?.event_id != null) {
          this.lastInterruptId = typeof evt.event_id === 'string'
            ? parseInt(evt.event_id, 10)
            : evt.event_id;
        }
        this.flushAudioQueue();
        this.setStatus('listening');
        break;
      }

      case 'ping': {
        const evt = data.ping_event as { event_id?: number } | undefined;
        if (evt?.event_id != null && this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'pong', event_id: evt.event_id }));
        }
        break;
      }

      case 'error': {
        const evt = data.error_event as { message?: string; error_type?: string } | undefined;
        const msg = evt?.message ?? 'Unknown error';
        this.config.onError?.(msg);
        if (evt?.error_type === 'max_duration_exceeded') {
          this.setStatus('idle');
        } else {
          this.setStatus('error');
        }
        break;
      }

      default:
        break;
    }
  }

  // ---- Audio capture ----

  private startMicCapture(): void {
    navigator.mediaDevices
      .getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
          sampleRate: { ideal: SAMPLE_RATE },
        },
      })
      .then((stream) => {
        this.mediaStream = stream;
        this.audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
        const source = this.audioCtx.createMediaStreamSource(stream);
        const analyser = this.audioCtx.createAnalyser();
        analyser.fftSize = 2048;
        source.connect(analyser);

        this.processor = this.audioCtx.createScriptProcessor(BUFFER_SIZE, 1, 1);
        analyser.connect(this.processor);
        this.processor.connect(this.audioCtx.destination);

        this.processor.onaudioprocess = (e) => {
          if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
          if (this.isMuted) return;
          const input = e.inputBuffer.getChannelData(0);
          const b64 = float32ToPcm16Base64(input);
          this.ws.send(JSON.stringify({ user_audio_chunk: b64 }));
        };
      })
      .catch((err) => {
        const msg = err instanceof Error ? err.message : 'Microphone access denied';
        this.setStatus('error');
        this.config.onError?.(msg);
      });
  }

  // ---- Audio playback ----

  private enqueueAudio(b64: string): void {
    const samples = base64ToPcm16Float32(b64);
    this.playQueue.push(samples);
    if (!this.isPlaying) {
      this.setStatus('speaking');
      this.playNextChunk();
    }
  }

  private playNextChunk(): void {
    if (this.playQueue.length === 0) {
      this.isPlaying = false;
      if (this.status === 'speaking') this.setStatus('listening');
      return;
    }
    this.isPlaying = true;
    const chunk = this.playQueue.shift()!;
    if (!this.outputCtx) {
      this.outputCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
    }
    const buffer = this.outputCtx.createBuffer(1, chunk.length, SAMPLE_RATE);
    buffer.copyToChannel(chunk, 0);
    const source = this.outputCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.outputCtx.destination);
    source.onended = () => this.playNextChunk();
    source.start();
  }

  private flushAudioQueue(): void {
    this.playQueue = [];
    this.isPlaying = false;
  }

  private cleanupAudio(): void {
    if (this.processor) {
      this.processor.disconnect();
      this.processor = null;
    }
    if (this.audioCtx && this.audioCtx.state !== 'closed') {
      this.audioCtx.close().catch(() => {});
      this.audioCtx = null;
    }
    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach((t) => t.stop());
      this.mediaStream = null;
    }
    if (this.outputCtx && this.outputCtx.state !== 'closed') {
      this.outputCtx.close().catch(() => {});
      this.outputCtx = null;
    }
    this.flushAudioQueue();
  }

  // ---- Rendering ----

  private render(): void {
    if (!this.container) return;
    switch (this.config.variant) {
      case 'compact':
        this.renderCompact();
        break;
      case 'full':
        this.renderFull();
        break;
      case 'expandable':
      default:
        this.renderExpandable();
        break;
    }

    // Apply position overrides
    if (this.config.position) {
      const root = this.container.firstElementChild as HTMLElement | null;
      if (root) {
        const pos = this.config.position;
        if (pos.bottom != null) root.style.bottom = `${pos.bottom}px`;
        if (pos.right != null) root.style.right = `${pos.right}px`;
        if (pos.left != null) { root.style.left = `${pos.left}px`; root.style.right = 'auto'; }
        if (pos.top != null) { root.style.top = `${pos.top}px`; root.style.bottom = 'auto'; }
      }
    }
  }

  private renderCompact(): void {
    if (!this.container) return;
    const statusClass = this.status === 'listening' ? 'listening' : this.status === 'speaking' ? 'speaking' : '';
    const badgeClass = this.isActive ? 'connected' : this.status === 'connecting' ? 'connecting' : this.status === 'error' ? 'error' : '';

    this.container.innerHTML = `
      <div class="ecw-compact">
        <button class="ecw-compact-btn ${statusClass}" aria-label="${this.isActive ? 'End conversation' : 'Start conversation'}">
          <span class="ecw-icon">${this.isActive ? ICON_STOP : ICON_MIC}</span>
          <span class="ecw-wave-container">
            <span class="ecw-wave-bar"></span>
            <span class="ecw-wave-bar"></span>
            <span class="ecw-wave-bar"></span>
            <span class="ecw-wave-bar"></span>
            <span class="ecw-wave-bar"></span>
          </span>
          ${badgeClass ? `<span class="ecw-status-badge ${badgeClass}"></span>` : ''}
        </button>
      </div>
    `;

    this.container.querySelector('.ecw-compact-btn')?.addEventListener('click', () => {
      if (this.isActive || this.status === 'connecting') {
        this.disconnect();
      } else {
        this.connect();
      }
    });
  }

  private renderFull(): void {
    if (!this.container) return;
    const statusLabel =
      this.status === 'listening' ? 'Listening...' :
      this.status === 'speaking' ? 'Speaking...' :
      this.status === 'connecting' ? 'Connecting...' :
      this.status === 'error' ? 'Error' :
      this.isActive ? 'Connected' : 'Offline';

    const avatar = this.config.avatar
      ? `<img src="${this.config.avatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;" />`
      : ICON_CHAT.replace('width="28"', 'width="20"').replace('height="28"', 'height="20"');

    this.container.innerHTML = `
      <div class="ecw-full">
        <div class="ecw-header">
          <div class="ecw-header-left">
            <div class="ecw-avatar">${avatar}</div>
            <div>
              <div class="ecw-header-title">${this.escapeHtml(this.config.title)}</div>
              <div class="ecw-header-status">${statusLabel}</div>
            </div>
          </div>
          <button class="ecw-close-btn" aria-label="Close">${ICON_CLOSE}</button>
        </div>
        <div class="ecw-transcript" id="ecw-transcript-area"></div>
        <div class="ecw-input-bar">
          <input class="ecw-text-input" type="text" placeholder="Type a message..." />
          ${!this.config.textOnly ? `
            <button class="ecw-mic-btn ${this.isMuted ? 'muted' : ''} ${this.status === 'listening' ? 'listening' : ''}" aria-label="${this.isMuted ? 'Unmute' : 'Mute'}">
              ${this.isMuted ? ICON_MIC_OFF : ICON_MIC}
            </button>
          ` : ''}
          <button class="ecw-send-btn" aria-label="Send">${ICON_SEND}</button>
        </div>
      </div>
    `;

    this.renderTranscript();
    this.bindFullListeners();
  }

  private renderExpandable(): void {
    if (!this.container) return;

    if (this.isExpanded) {
      // Show full panel + compact toggle
      this.renderFull();
      // Override close to collapse instead of unmount
      const closeBtn = this.container.querySelector('.ecw-close-btn');
      if (closeBtn) {
        closeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          this.isExpanded = false;
          this.render();
        });
      }
    } else {
      // Show compact button
      const statusClass = this.status === 'listening' ? 'listening' : this.status === 'speaking' ? 'speaking' : '';
      const badgeClass = this.isActive ? 'connected' : this.status === 'connecting' ? 'connecting' : this.status === 'error' ? 'error' : '';

      this.container.innerHTML = `
        <div class="ecw-expandable">
          <button class="ecw-compact-btn ${statusClass}" aria-label="Open conversation">
            <span class="ecw-icon">${this.isActive ? ICON_STOP : ICON_CHAT}</span>
            <span class="ecw-wave-container">
              <span class="ecw-wave-bar"></span>
              <span class="ecw-wave-bar"></span>
              <span class="ecw-wave-bar"></span>
              <span class="ecw-wave-bar"></span>
              <span class="ecw-wave-bar"></span>
            </span>
            ${badgeClass ? `<span class="ecw-status-badge ${badgeClass}"></span>` : ''}
          </button>
        </div>
      `;

      this.container.querySelector('.ecw-compact-btn')?.addEventListener('click', () => {
        this.isExpanded = true;
        if (!this.isActive && this.status !== 'connecting') {
          this.connect();
        }
        this.render();
      });
    }
  }

  private renderTranscript(): void {
    if (!this.shadow) return;
    const area = this.shadow.getElementById('ecw-transcript-area');
    if (!area) return;

    let html = '';

    if (this.transcript.length === 0 && !this.isAgentStreaming) {
      if (this.config.greeting && this.isActive) {
        html = `<div class="ecw-msg agent">${this.escapeHtml(this.config.greeting)}</div>`;
      } else if (!this.isActive) {
        html = `
          <div class="ecw-empty">
            <div class="ecw-empty-icon">${ICON_CHAT}</div>
            <div class="ecw-empty-text">Start a conversation</div>
          </div>
        `;
      }
    } else {
      for (const entry of this.transcript) {
        html += `<div class="ecw-msg ${entry.role}">${this.escapeHtml(entry.text)}</div>`;
      }
    }

    // Streaming agent text
    if (this.isAgentStreaming && this.currentAgentText) {
      html += `<div class="ecw-msg agent streaming">${this.escapeHtml(this.currentAgentText)}</div>`;
    }

    area.innerHTML = html;
    area.scrollTop = area.scrollHeight;
  }

  private bindFullListeners(): void {
    if (!this.container) return;

    // Close button
    const closeBtn = this.container.querySelector('.ecw-close-btn');
    closeBtn?.addEventListener('click', () => {
      if (this.config.variant === 'expandable') {
        this.isExpanded = false;
        this.render();
      } else {
        this.disconnect();
      }
    });

    // Text input
    const textInput = this.container.querySelector('.ecw-text-input') as HTMLInputElement | null;
    const sendBtn = this.container.querySelector('.ecw-send-btn');

    const doSend = () => {
      if (!textInput) return;
      const text = textInput.value.trim();
      if (!text) return;
      if (!this.isActive) {
        this.connect();
        // Queue message to send after connection
        const waitSend = setInterval(() => {
          if (this.isActive) {
            this.sendText(text);
            clearInterval(waitSend);
          }
        }, 200);
        setTimeout(() => clearInterval(waitSend), 10000);
      } else {
        this.sendText(text);
      }
      textInput.value = '';
    };

    textInput?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        doSend();
      }
    });
    sendBtn?.addEventListener('click', doSend);

    // Mic button
    const micBtn = this.container.querySelector('.ecw-mic-btn');
    micBtn?.addEventListener('click', () => this.toggleMute());

    // Auto-connect if not connected
    if (!this.isActive && this.status !== 'connecting') {
      this.connect();
    }
  }

  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}
