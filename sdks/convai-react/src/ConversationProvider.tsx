/**
 * ConversationProvider — React context provider for Echo ConvAI conversations.
 *
 * Wraps the WebSocket conversation client and provides connection state,
 * transcript, agent responses, audio permission handling, and error boundary
 * to all child components via React context.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ConversationStatus =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'speaking'
  | 'listening'
  | 'error';

export interface TranscriptEntry {
  role: 'user' | 'agent';
  text: string;
  timestamp: number;
  isFinal: boolean;
}

export interface SessionOptions {
  /** Override the default server URL */
  serverUrl?: string;
  /** Dynamic variables injected into agent prompt */
  dynamicVariables?: Record<string, string | number | boolean>;
  /** Override agent config */
  configOverride?: Record<string, unknown>;
  /** Custom LLM extra body */
  extraBody?: Record<string, unknown>;
  /** User identifier for session tracking */
  userId?: string;
  /** Text-only mode — no audio */
  textOnly?: boolean;
}

export interface ConversationContextValue {
  // Session lifecycle
  startSession: (agentId: string, options?: SessionOptions) => Promise<void>;
  endSession: () => void;
  sendMessage: (text: string) => void;

  // State
  status: ConversationStatus;
  isConnected: boolean;
  isSpeaking: boolean;
  isListening: boolean;

  // Transcript
  transcript: string;
  agentResponse: string;
  history: TranscriptEntry[];

  // Audio
  volume: number;
  isMuted: boolean;
  toggleMute: () => void;

  // Error
  error: string | null;

  // Internals (escape hatch)
  conversationId: string | null;
}

// ---------------------------------------------------------------------------
// Internal WebSocket message types (mirrors the echo-convai protocol)
// ---------------------------------------------------------------------------

interface ServerMessage {
  type: string;
  [key: string]: unknown;
}

interface AudioEvent {
  audio_base_64: string;
  event_id: number | string;
  alignment?: {
    chars: string[];
    char_start_times_ms: number[];
    char_durations_ms: number[];
  };
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const ConversationCtx = createContext<ConversationContextValue | null>(null);

export function useConversationContext(): ConversationContextValue {
  const ctx = useContext(ConversationCtx);
  if (!ctx) {
    throw new Error(
      'useConversationContext must be used inside <ConversationProvider>',
    );
  }
  return ctx;
}

// ---------------------------------------------------------------------------
// Audio helpers
// ---------------------------------------------------------------------------

const SAMPLE_RATE = 16_000;
const BUFFER_SIZE = 4000; // 250 ms at 16 kHz

async function requestMicPermission(): Promise<MediaStream> {
  return navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
      sampleRate: { ideal: SAMPLE_RATE },
    },
  });
}

function float32ToPcm16Base64(samples: Float32Array): string {
  const buf = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    const val = s < 0 ? s * 0x8000 : s * 0x7fff;
    view.setInt16(i * 2, val, true);
  }
  const bytes = new Uint8Array(buf);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

function base64ToPcm16Float32(b64: string): Float32Array {
  const raw = atob(b64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) {
    bytes[i] = raw.charCodeAt(i);
  }
  const view = new DataView(bytes.buffer);
  const samples = new Float32Array(bytes.length / 2);
  for (let i = 0; i < samples.length; i++) {
    samples[i] = view.getInt16(i * 2, true) / 32768;
  }
  return samples;
}

// ---------------------------------------------------------------------------
// Provider props
// ---------------------------------------------------------------------------

export interface ConversationProviderProps {
  children: React.ReactNode;
  /** Default server URL (wss://echo-convai.echo-op.com) */
  defaultServerUrl?: string;
  /** Called when agent sends a complete response */
  onAgentResponse?: (text: string) => void;
  /** Called when user transcript is finalized */
  onUserTranscript?: (text: string) => void;
  /** Called when session ends */
  onSessionEnd?: (conversationId: string | null) => void;
  /** Called on any error */
  onError?: (error: string) => void;
}

// ---------------------------------------------------------------------------
// Provider component
// ---------------------------------------------------------------------------

export function ConversationProvider({
  children,
  defaultServerUrl = 'wss://echo-convai.echo-op.com',
  onAgentResponse,
  onUserTranscript,
  onSessionEnd,
  onError,
}: ConversationProviderProps) {
  // ---- State ----
  const [status, setStatus] = useState<ConversationStatus>('idle');
  const [transcript, setTranscript] = useState('');
  const [agentResponse, setAgentResponse] = useState('');
  const [history, setHistory] = useState<TranscriptEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [volume, setVolume] = useState(0);
  const [isMuted, setIsMuted] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);

  // ---- Refs ----
  const wsRef = useRef<WebSocket | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const outputCtxRef = useRef<AudioContext | null>(null);
  const lastInterruptIdRef = useRef<number>(-1);
  const mutedRef = useRef(isMuted);
  const playQueueRef = useRef<Float32Array[]>([]);
  const isPlayingRef = useRef(false);

  // Keep ref in sync
  useEffect(() => {
    mutedRef.current = isMuted;
  }, [isMuted]);

  // ---- Audio output ----
  const playNextChunk = useCallback(() => {
    if (playQueueRef.current.length === 0) {
      isPlayingRef.current = false;
      setStatus((prev) => (prev === 'speaking' ? 'listening' : prev));
      return;
    }
    isPlayingRef.current = true;
    const chunk = playQueueRef.current.shift()!;
    if (!outputCtxRef.current) {
      outputCtxRef.current = new AudioContext({ sampleRate: SAMPLE_RATE });
    }
    const ctx = outputCtxRef.current;
    const buffer = ctx.createBuffer(1, chunk.length, SAMPLE_RATE);
    buffer.copyToChannel(chunk, 0);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    source.onended = () => playNextChunk();
    source.start();
  }, []);

  const enqueueAudio = useCallback(
    (b64: string) => {
      const samples = base64ToPcm16Float32(b64);
      playQueueRef.current.push(samples);
      if (!isPlayingRef.current) {
        setStatus('speaking');
        playNextChunk();
      }
    },
    [playNextChunk],
  );

  const flushAudioQueue = useCallback(() => {
    playQueueRef.current = [];
    isPlayingRef.current = false;
  }, []);

  // ---- WebSocket message handler ----
  const handleMessage = useCallback(
    (data: ServerMessage) => {
      switch (data.type) {
        case 'conversation_initiation_metadata': {
          const meta = data.conversation_initiation_metadata_event as {
            conversation_id?: string;
          } | undefined;
          if (meta?.conversation_id) {
            setConversationId(meta.conversation_id);
          }
          setStatus('listening');
          break;
        }

        case 'audio': {
          const evt = (data.audio_event as AudioEvent) ?? undefined;
          if (!evt) break;
          const eventId =
            typeof evt.event_id === 'string'
              ? parseInt(evt.event_id, 10)
              : evt.event_id;
          if (eventId <= lastInterruptIdRef.current) break; // stale
          enqueueAudio(evt.audio_base_64);
          break;
        }

        case 'agent_response': {
          const evt = data.agent_response_event as {
            agent_response?: string;
          } | undefined;
          const text = evt?.agent_response ?? '';
          setAgentResponse(text);
          setHistory((h) => [
            ...h,
            { role: 'agent', text, timestamp: Date.now(), isFinal: true },
          ]);
          onAgentResponse?.(text);
          break;
        }

        case 'agent_chat_response_part': {
          const part = data.text_response_part as {
            type?: string;
            text?: string;
          } | undefined;
          if (part?.type === 'delta' && part.text) {
            setAgentResponse((prev) => prev + part.text);
          } else if (part?.type === 'start') {
            setAgentResponse('');
          }
          break;
        }

        case 'user_transcript': {
          const evt = data.user_transcription_event as {
            user_transcript?: string;
          } | undefined;
          const text = evt?.user_transcript ?? '';
          setTranscript(text);
          setHistory((h) => [
            ...h,
            { role: 'user', text, timestamp: Date.now(), isFinal: true },
          ]);
          onUserTranscript?.(text);
          break;
        }

        case 'interruption': {
          const evt = data.interruption_event as {
            event_id?: number | string;
          } | undefined;
          if (evt?.event_id != null) {
            lastInterruptIdRef.current =
              typeof evt.event_id === 'string'
                ? parseInt(evt.event_id, 10)
                : evt.event_id;
          }
          flushAudioQueue();
          setStatus('listening');
          break;
        }

        case 'ping': {
          const evt = data.ping_event as { event_id?: number } | undefined;
          if (evt?.event_id != null && wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(
              JSON.stringify({ type: 'pong', event_id: evt.event_id }),
            );
          }
          break;
        }

        case 'error': {
          const evt = data.error_event as {
            message?: string;
            error_type?: string;
          } | undefined;
          const msg = evt?.message ?? 'Unknown error';
          setError(msg);
          onError?.(msg);
          if (evt?.error_type === 'max_duration_exceeded') {
            setStatus('idle');
          } else {
            setStatus('error');
          }
          break;
        }

        default:
          // client_tool_call, vad_score, etc. — ignored in this layer
          break;
      }
    },
    [enqueueAudio, flushAudioQueue, onAgentResponse, onUserTranscript, onError],
  );

  // ---- Mic capture setup ----
  const startMicCapture = useCallback(
    (ws: WebSocket) => {
      requestMicPermission()
        .then((stream) => {
          streamRef.current = stream;
          const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
          audioCtxRef.current = ctx;
          const source = ctx.createMediaStreamSource(stream);

          // Analyser for volume metering
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 2048;
          analyser.smoothingTimeConstant = 0.8;
          source.connect(analyser);

          // ScriptProcessor for raw PCM capture (widely supported)
          const processor = ctx.createScriptProcessor(BUFFER_SIZE, 1, 1);
          processorRef.current = processor;
          analyser.connect(processor);
          processor.connect(ctx.destination);

          // Volume polling
          const freqData = new Uint8Array(analyser.frequencyBinCount);
          const volumeInterval = setInterval(() => {
            analyser.getByteFrequencyData(freqData);
            let sum = 0;
            for (let i = 0; i < freqData.length; i++) sum += freqData[i];
            const avg = sum / freqData.length / 255;
            setVolume(avg);
          }, 100);

          processor.onaudioprocess = (e) => {
            if (ws.readyState !== WebSocket.OPEN) return;
            const input = e.inputBuffer.getChannelData(0);
            if (mutedRef.current) return;
            const b64 = float32ToPcm16Base64(input);
            ws.send(JSON.stringify({ user_audio_chunk: b64 }));
          };

          // Cleanup volume interval when context closes
          ctx.addEventListener('statechange', () => {
            if (ctx.state === 'closed') clearInterval(volumeInterval);
          });
        })
        .catch((err) => {
          const msg =
            err instanceof Error ? err.message : 'Microphone access denied';
          setError(msg);
          setStatus('error');
          onError?.(msg);
        });
    },
    [onError],
  );

  // ---- Cleanup helper ----
  const cleanup = useCallback(() => {
    if (processorRef.current) {
      processorRef.current.disconnect();
      processorRef.current = null;
    }
    if (audioCtxRef.current && audioCtxRef.current.state !== 'closed') {
      audioCtxRef.current.close().catch(() => {});
      audioCtxRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
    if (outputCtxRef.current && outputCtxRef.current.state !== 'closed') {
      outputCtxRef.current.close().catch(() => {});
      outputCtxRef.current = null;
    }
    flushAudioQueue();
  }, [flushAudioQueue]);

  // ---- startSession ----
  const startSession = useCallback(
    async (agentId: string, options?: SessionOptions) => {
      // Reset state
      setError(null);
      setTranscript('');
      setAgentResponse('');
      setHistory([]);
      setConversationId(null);
      lastInterruptIdRef.current = -1;
      flushAudioQueue();

      const serverUrl = options?.serverUrl ?? defaultServerUrl;
      const wsUrl = `${serverUrl}/v1/convai/conversation?agent_id=${encodeURIComponent(agentId)}&source=js_sdk&version=1.0.0`;

      setStatus('connecting');

      const ws = new WebSocket(wsUrl, ['convai']);
      wsRef.current = ws;

      ws.onopen = () => {
        // Send initiation message
        const initMsg: Record<string, unknown> = {
          type: 'conversation_initiation_client_data',
          source_info: { source: 'echo_convai_react', version: '1.0.0' },
        };
        if (options?.configOverride) {
          initMsg.conversation_config_override = options.configOverride;
        }
        if (options?.dynamicVariables) {
          initMsg.dynamic_variables = options.dynamicVariables;
        }
        if (options?.extraBody) {
          initMsg.custom_llm_extra_body = options.extraBody;
        }
        if (options?.userId) {
          initMsg.user_id = options.userId;
        }
        if (options?.textOnly) {
          initMsg.conversation_config_override = {
            ...(initMsg.conversation_config_override as Record<string, unknown> ?? {}),
            text_only: true,
          };
        }
        ws.send(JSON.stringify(initMsg));

        // Start mic capture unless text-only
        if (!options?.textOnly) {
          startMicCapture(ws);
        }
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data as string) as ServerMessage;
          handleMessage(data);
        } catch {
          // Ignore malformed messages
        }
      };

      ws.onerror = () => {
        // The close event will fire after with more details
      };

      ws.onclose = (event) => {
        cleanup();
        const prevId = conversationId;
        if (event.code === 1000) {
          setStatus('idle');
        } else {
          const msg = event.reason || `Connection closed (code ${event.code})`;
          setError(msg);
          setStatus('error');
          onError?.(msg);
        }
        onSessionEnd?.(prevId);
        wsRef.current = null;
      };
    },
    [
      defaultServerUrl,
      handleMessage,
      startMicCapture,
      cleanup,
      flushAudioQueue,
      conversationId,
      onError,
      onSessionEnd,
    ],
  );

  // ---- endSession ----
  const endSession = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.close(1000, 'client_ended');
    }
    cleanup();
    setStatus('idle');
  }, [cleanup]);

  // ---- sendMessage ----
  const sendMessage = useCallback((text: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({ type: 'user_message', text }),
      );
    }
  }, []);

  // ---- toggleMute ----
  const toggleMute = useCallback(() => {
    setIsMuted((prev) => !prev);
  }, []);

  // ---- Cleanup on unmount ----
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close(1000, 'unmount');
      }
      cleanup();
    };
  }, [cleanup]);

  // ---- Context value ----
  const value = useMemo<ConversationContextValue>(
    () => ({
      startSession,
      endSession,
      sendMessage,
      status,
      isConnected: status === 'connected' || status === 'speaking' || status === 'listening',
      isSpeaking: status === 'speaking',
      isListening: status === 'listening',
      transcript,
      agentResponse,
      history,
      volume,
      isMuted,
      toggleMute,
      error,
      conversationId,
    }),
    [
      startSession,
      endSession,
      sendMessage,
      status,
      transcript,
      agentResponse,
      history,
      volume,
      isMuted,
      toggleMute,
      error,
      conversationId,
    ],
  );

  return (
    <ConversationCtx.Provider value={value}>
      {children}
    </ConversationCtx.Provider>
  );
}
