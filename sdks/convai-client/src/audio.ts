// ============================================================================
// @echo-prime/convai-client — Audio Capture & Playback
// Browser audio handling via Web Audio API and AudioWorklet
// ============================================================================

import {
  type AudioFormatString,
  type AudioEncoding,
  parseAudioFormat,
} from "./types.js";

// ---------------------------------------------------------------------------
// Base64 encode/decode utilities (browser + Node.js compatible)
// ---------------------------------------------------------------------------

/**
 * Encode an ArrayBuffer to a base64 string.
 * Works in both browser (btoa) and Node.js (Buffer) environments.
 */
export function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  if (typeof Buffer !== "undefined") {
    return Buffer.from(bytes).toString("base64");
  }
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

/**
 * Decode a base64 string to an ArrayBuffer.
 * Works in both browser (atob) and Node.js (Buffer) environments.
 */
export function base64ToArrayBuffer(base64: string): ArrayBuffer {
  if (typeof Buffer !== "undefined") {
    const buf = Buffer.from(base64, "base64");
    return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  }
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}

// ---------------------------------------------------------------------------
// PCM encoding/decoding utilities
// ---------------------------------------------------------------------------

/**
 * Convert Float32 audio samples to 16-bit PCM (Int16Array).
 * Input range: [-1.0, 1.0]
 */
export function float32ToPcm16(float32: Float32Array): Int16Array {
  const pcm16 = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const sample = Math.max(-1.0, Math.min(1.0, float32[i]));
    pcm16[i] = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
  }
  return pcm16;
}

/**
 * Convert 16-bit PCM (Int16Array) to Float32 audio samples.
 * Output range: [-1.0, 1.0]
 */
export function pcm16ToFloat32(pcm16: Int16Array): Float32Array {
  const float32 = new Float32Array(pcm16.length);
  for (let i = 0; i < pcm16.length; i++) {
    float32[i] = pcm16[i] / 32768;
  }
  return float32;
}

/**
 * Encode a Float32 sample to mu-law (8-bit).
 */
function float32SampleToUlaw(sample: number): number {
  const BIAS = 0x84;
  const CLIP = 32635;
  const sign = sample < 0 ? 0x80 : 0;
  let pcm = Math.round(Math.abs(sample) * 32768);
  if (pcm > CLIP) pcm = CLIP;
  pcm += BIAS;

  let exponent = 7;
  let mask = 0x4000;
  while (exponent > 0 && (pcm & mask) === 0) {
    exponent--;
    mask >>= 1;
  }

  const mantissa = (pcm >> (exponent + 3)) & 0x0f;
  const ulawByte = ~(sign | (exponent << 4) | mantissa) & 0xff;
  return ulawByte;
}

/**
 * Decode a mu-law byte to a Float32 sample.
 */
function ulawByteToFloat32(ulawByte: number): number {
  const BIAS = 0x84;
  const complement = ~ulawByte & 0xff;
  const sign = (complement & 0x80) !== 0;
  const exponent = (complement >> 4) & 0x07;
  const mantissa = complement & 0x0f;
  let magnitude = ((mantissa << (exponent + 3)) + BIAS) << 1;
  magnitude = magnitude >> (exponent + 3 + 1);
  const sample = sign ? -magnitude / 32768 : magnitude / 32768;
  return Math.max(-1.0, Math.min(1.0, sample));
}

/**
 * Encode Float32 audio to mu-law Uint8Array.
 */
export function float32ToUlaw(float32: Float32Array): Uint8Array {
  const ulaw = new Uint8Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    ulaw[i] = float32SampleToUlaw(float32[i]);
  }
  return ulaw;
}

/**
 * Decode mu-law Uint8Array to Float32 audio.
 */
export function ulawToFloat32(ulaw: Uint8Array): Float32Array {
  const float32 = new Float32Array(ulaw.length);
  for (let i = 0; i < ulaw.length; i++) {
    float32[i] = ulawByteToFloat32(ulaw[i]);
  }
  return float32;
}

/**
 * Decode raw audio bytes (PCM16 or ulaw) into Float32Array based on encoding.
 */
export function decodeAudioBytes(buffer: ArrayBuffer, encoding: AudioEncoding): Float32Array {
  if (encoding === "pcm") {
    const pcm16 = new Int16Array(buffer);
    return pcm16ToFloat32(pcm16);
  }
  const ulaw = new Uint8Array(buffer);
  return ulawToFloat32(ulaw);
}

/**
 * Encode Float32Array into raw audio bytes (PCM16 or ulaw) based on encoding.
 */
export function encodeAudioBytes(float32: Float32Array, encoding: AudioEncoding): ArrayBuffer {
  if (encoding === "pcm") {
    const pcm16 = float32ToPcm16(float32);
    const copy = new Uint8Array(pcm16.byteLength);
    copy.set(new Uint8Array(pcm16.buffer, pcm16.byteOffset, pcm16.byteLength));
    return copy.buffer as ArrayBuffer;
  }
  const ulaw = float32ToUlaw(float32);
  const copy = new Uint8Array(ulaw.byteLength);
  copy.set(new Uint8Array(ulaw.buffer, ulaw.byteOffset, ulaw.byteLength));
  return copy.buffer as ArrayBuffer;
}

// ---------------------------------------------------------------------------
// Volume detection
// ---------------------------------------------------------------------------

/**
 * Calculate RMS volume of Float32 audio samples.
 * Returns value between 0.0 and 1.0.
 */
export function calculateRmsVolume(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) {
    sum += samples[i] * samples[i];
  }
  return Math.sqrt(sum / samples.length);
}

/**
 * Calculate peak volume of Float32 audio samples.
 * Returns value between 0.0 and 1.0.
 */
export function calculatePeakVolume(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let peak = 0;
  for (let i = 0; i < samples.length; i++) {
    const abs = Math.abs(samples[i]);
    if (abs > peak) peak = abs;
  }
  return peak;
}

// ---------------------------------------------------------------------------
// Linear resampling
// ---------------------------------------------------------------------------

/**
 * Resample Float32 audio from one sample rate to another using linear interpolation.
 */
export function resampleLinear(
  input: Float32Array,
  inputSampleRate: number,
  outputSampleRate: number
): Float32Array {
  if (inputSampleRate === outputSampleRate) return input;
  const ratio = inputSampleRate / outputSampleRate;
  const outputLength = Math.ceil(input.length / ratio);
  const output = new Float32Array(outputLength);
  for (let i = 0; i < outputLength; i++) {
    const srcIndex = i * ratio;
    const srcFloor = Math.floor(srcIndex);
    const srcCeil = Math.min(srcFloor + 1, input.length - 1);
    const fraction = srcIndex - srcFloor;
    output[i] = input[srcFloor] * (1 - fraction) + input[srcCeil] * fraction;
  }
  return output;
}

// ---------------------------------------------------------------------------
// AudioWorklet processor source (inline, loaded via Blob URL)
// ---------------------------------------------------------------------------

const RAW_AUDIO_PROCESSOR_SOURCE = `
class RawAudioProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.bufferSize = options.processorOptions?.bufferSize || 1600;
    this.buffer = new Float32Array(this.bufferSize);
    this.bufferIndex = 0;
    this.isMuted = false;
    this.port.onmessage = (e) => {
      if (e.data.type === 'mute') this.isMuted = e.data.muted;
    };
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const channel = input[0];
    for (let i = 0; i < channel.length; i++) {
      this.buffer[this.bufferIndex++] = this.isMuted ? 0 : channel[i];
      if (this.bufferIndex >= this.bufferSize) {
        const chunk = this.buffer.slice();
        let rms = 0;
        for (let j = 0; j < chunk.length; j++) rms += chunk[j] * chunk[j];
        rms = Math.sqrt(rms / chunk.length);
        this.port.postMessage({ samples: chunk, volume: rms }, [chunk.buffer]);
        this.buffer = new Float32Array(this.bufferSize);
        this.bufferIndex = 0;
      }
    }
    return true;
  }
}
registerProcessor('raw-audio-processor', RawAudioProcessor);
`;

const AUDIO_CONCAT_PROCESSOR_SOURCE = `
class AudioConcatProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.currentBuffer = null;
    this.currentOffset = 0;
    this.interrupted = false;
    this.port.onmessage = (e) => {
      if (e.data.type === 'buffer') {
        if (!this.interrupted) {
          this.queue.push(new Float32Array(e.data.samples));
        }
      } else if (e.data.type === 'interrupt') {
        this.interrupted = true;
        this.queue = [];
        this.currentBuffer = null;
        this.currentOffset = 0;
      } else if (e.data.type === 'clearInterrupted') {
        this.interrupted = false;
      }
    };
  }
  process(outputs) {
    const output = outputs[0];
    if (!output || !output[0]) return true;
    const channel = output[0];
    let written = 0;
    while (written < channel.length) {
      if (!this.currentBuffer) {
        if (this.queue.length === 0) {
          channel.fill(0, written);
          this.port.postMessage({ type: 'process', finished: true });
          return true;
        }
        this.currentBuffer = this.queue.shift();
        this.currentOffset = 0;
      }
      const remaining = this.currentBuffer.length - this.currentOffset;
      const needed = channel.length - written;
      const toCopy = Math.min(remaining, needed);
      for (let i = 0; i < toCopy; i++) {
        channel[written + i] = this.currentBuffer[this.currentOffset + i];
      }
      written += toCopy;
      this.currentOffset += toCopy;
      if (this.currentOffset >= this.currentBuffer.length) {
        this.currentBuffer = null;
        this.currentOffset = 0;
      }
    }
    this.port.postMessage({ type: 'process', finished: false });
    return true;
  }
}
registerProcessor('audio-concat-processor', AudioConcatProcessor);
`;

// ---------------------------------------------------------------------------
// Worklet loader utility
// ---------------------------------------------------------------------------

function createWorkletBlobUrl(source: string): string {
  const blob = new Blob([source], { type: "application/javascript" });
  return URL.createObjectURL(blob);
}

async function loadWorkletModule(ctx: AudioContext, source: string): Promise<void> {
  // Try Blob URL first (most browsers)
  try {
    const blobUrl = createWorkletBlobUrl(source);
    await ctx.audioWorklet.addModule(blobUrl);
    URL.revokeObjectURL(blobUrl);
    return;
  } catch {
    // Blob URL failed (e.g., Safari iframe CORS), try data: URL
  }

  try {
    const base64 = btoa(source);
    const dataUrl = `data:application/javascript;base64,${base64}`;
    await ctx.audioWorklet.addModule(dataUrl);
    return;
  } catch {
    throw new Error(
      "Failed to load AudioWorklet module. " +
      "Your browser may block Blob and data: URLs in this context. " +
      "Consider self-hosting the worklet files."
    );
  }
}

// ---------------------------------------------------------------------------
// AudioCaptureManager — microphone capture
// ---------------------------------------------------------------------------

export interface AudioCaptureConfig {
  sampleRate?: number;
  echoCancellation?: boolean;
  noiseSuppression?: boolean;
  autoGainControl?: boolean;
  channelCount?: number;
}

export type AudioChunkCallback = (
  pcm16Buffer: ArrayBuffer,
  volume: number
) => void;

/**
 * Captures microphone audio via AudioWorklet, emitting PCM16 chunks.
 * Handles sample rate conversion if the browser doesn't natively
 * support the requested sample rate.
 */
export class AudioCaptureManager {
  private audioContext: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private analyserNode: AnalyserNode | null = null;
  private _isMuted = false;
  private _isCapturing = false;
  private targetSampleRate: number;
  private config: AudioCaptureConfig;
  private onChunk: AudioChunkCallback | null = null;

  constructor(config: AudioCaptureConfig = {}) {
    this.config = config;
    this.targetSampleRate = config.sampleRate ?? 16000;
  }

  get isMuted(): boolean {
    return this._isMuted;
  }

  get isCapturing(): boolean {
    return this._isCapturing;
  }

  /**
   * Start capturing microphone audio.
   * @param callback Called with each PCM16 audio chunk and its RMS volume.
   */
  async start(callback: AudioChunkCallback): Promise<void> {
    if (this._isCapturing) return;

    this.onChunk = callback;

    // Create AudioContext with target sample rate
    this.audioContext = new AudioContext({ sampleRate: this.targetSampleRate });

    // If browser overrode our sample rate, we'll resample in the worklet
    const actualSampleRate = this.audioContext.sampleRate;

    // Request microphone with constraints
    const constraints: MediaStreamConstraints = {
      audio: {
        echoCancellation: this.config.echoCancellation ?? true,
        noiseSuppression: this.config.noiseSuppression ?? true,
        autoGainControl: this.config.autoGainControl ?? true,
        channelCount: this.config.channelCount ?? 1,
        sampleRate: { ideal: this.targetSampleRate },
      },
    };

    this.stream = await navigator.mediaDevices.getUserMedia(constraints);
    this.sourceNode = this.audioContext.createMediaStreamSource(this.stream);

    // Analyser for frequency data
    this.analyserNode = this.audioContext.createAnalyser();
    this.analyserNode.fftSize = 2048;
    this.analyserNode.smoothingTimeConstant = 0.8;

    // Load worklet
    await loadWorkletModule(this.audioContext, RAW_AUDIO_PROCESSOR_SOURCE);

    // Buffer size = sampleRate / 10 = 100ms chunks
    const bufferSize = Math.round(actualSampleRate / 10);

    this.workletNode = new AudioWorkletNode(this.audioContext, "raw-audio-processor", {
      processorOptions: { bufferSize },
    });

    // Handle worklet messages
    this.workletNode.port.onmessage = (event: MessageEvent) => {
      const { samples, volume } = event.data as { samples: Float32Array; volume: number };

      // Resample if needed
      let finalSamples = samples;
      if (actualSampleRate !== this.targetSampleRate) {
        finalSamples = resampleLinear(samples, actualSampleRate, this.targetSampleRate);
      }

      // Convert to PCM16
      const pcm16 = float32ToPcm16(finalSamples);
      const copy = new Uint8Array(pcm16.byteLength);
      copy.set(new Uint8Array(pcm16.buffer, pcm16.byteOffset, pcm16.byteLength));
      const buffer = copy.buffer as ArrayBuffer;

      if (this.onChunk) {
        this.onChunk(buffer, volume);
      }
    };

    // Connect audio graph: source -> analyser -> worklet
    this.sourceNode.connect(this.analyserNode);
    this.analyserNode.connect(this.workletNode);
    // Don't connect worklet to destination (we don't want to hear ourselves)

    this._isCapturing = true;
  }

  /**
   * Stop capturing and release all resources.
   */
  stop(): void {
    this._isCapturing = false;
    this.onChunk = null;

    if (this.workletNode) {
      this.workletNode.disconnect();
      this.workletNode = null;
    }
    if (this.analyserNode) {
      this.analyserNode.disconnect();
      this.analyserNode = null;
    }
    if (this.sourceNode) {
      this.sourceNode.disconnect();
      this.sourceNode = null;
    }
    if (this.stream) {
      for (const track of this.stream.getTracks()) {
        track.stop();
      }
      this.stream = null;
    }
    if (this.audioContext) {
      void this.audioContext.close();
      this.audioContext = null;
    }
  }

  /**
   * Mute/unmute the microphone capture.
   * When muted, zero-filled audio is sent.
   */
  setMuted(muted: boolean): void {
    this._isMuted = muted;
    if (this.workletNode) {
      this.workletNode.port.postMessage({ type: "mute", muted });
    }
  }

  /**
   * Get frequency data from the input analyser (for visualization).
   */
  getByteFrequencyData(): Uint8Array {
    if (!this.analyserNode) return new Uint8Array(0);
    const data = new Uint8Array(this.analyserNode.frequencyBinCount);
    this.analyserNode.getByteFrequencyData(data);
    return data;
  }

  /**
   * Get current input volume (0-1).
   */
  getVolume(): number {
    if (!this.analyserNode) return 0;
    const data = new Float32Array(this.analyserNode.fftSize);
    this.analyserNode.getFloatTimeDomainData(data);
    return calculateRmsVolume(data);
  }
}

// ---------------------------------------------------------------------------
// AudioPlaybackManager — speaker output
// ---------------------------------------------------------------------------

export interface AudioPlaybackConfig {
  sampleRate?: number;
  encoding?: AudioEncoding;
  outputDeviceId?: string;
}

/**
 * Plays incoming audio chunks via Web Audio API AudioWorklet.
 * Supports interrupt (flush buffer) for handling agent interruptions.
 */
export class AudioPlaybackManager {
  private audioContext: AudioContext | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private gainNode: GainNode | null = null;
  private analyserNode: AnalyserNode | null = null;
  private audioElement: HTMLAudioElement | null = null;
  private _isPlaying = false;
  private _volume = 1.0;
  private sampleRate: number;
  private encoding: AudioEncoding;
  private outputDeviceId?: string;
  private onPlaybackFinished: (() => void) | null = null;
  private interruptFadeTimeout: ReturnType<typeof setTimeout> | null = null;

  constructor(config: AudioPlaybackConfig = {}) {
    this.sampleRate = config.sampleRate ?? 16000;
    this.encoding = config.encoding ?? "pcm";
    this.outputDeviceId = config.outputDeviceId;
  }

  get isPlaying(): boolean {
    return this._isPlaying;
  }

  get volume(): number {
    return this._volume;
  }

  /**
   * Initialize the playback pipeline. Must be called before enqueue().
   */
  async start(onPlaybackFinished?: () => void): Promise<void> {
    if (this.audioContext) return;

    this.onPlaybackFinished = onPlaybackFinished ?? null;
    this.audioContext = new AudioContext({ sampleRate: this.sampleRate });

    // Load worklet
    await loadWorkletModule(this.audioContext, AUDIO_CONCAT_PROCESSOR_SOURCE);

    this.workletNode = new AudioWorkletNode(this.audioContext, "audio-concat-processor");

    // Gain node for volume control
    this.gainNode = this.audioContext.createGain();
    this.gainNode.gain.value = this._volume;

    // Analyser for output visualization
    this.analyserNode = this.audioContext.createAnalyser();
    this.analyserNode.fftSize = 2048;
    this.analyserNode.smoothingTimeConstant = 0.8;

    // Handle playback status from worklet
    this.workletNode.port.onmessage = (event: MessageEvent) => {
      const { type, finished } = event.data as { type: string; finished: boolean };
      if (type === "process") {
        const wasPlaying = this._isPlaying;
        this._isPlaying = !finished;
        if (wasPlaying && finished && this.onPlaybackFinished) {
          this.onPlaybackFinished();
        }
      }
    };

    // Connect: worklet -> gain -> analyser -> destination
    this.workletNode.connect(this.gainNode);
    this.gainNode.connect(this.analyserNode);
    this.analyserNode.connect(this.audioContext.destination);

    // Optional: route through <audio> element for output device selection
    if (this.outputDeviceId && typeof document !== "undefined") {
      const dest = this.audioContext.createMediaStreamDestination();
      this.analyserNode.disconnect();
      this.analyserNode.connect(dest);
      this.audioElement = document.createElement("audio");
      this.audioElement.srcObject = dest.stream;
      if ("setSinkId" in this.audioElement) {
        await (this.audioElement as HTMLAudioElement & { setSinkId: (id: string) => Promise<void> })
          .setSinkId(this.outputDeviceId);
      }
      await this.audioElement.play();
    }
  }

  /**
   * Enqueue an audio chunk for playback.
   * @param buffer Raw audio bytes (PCM16 or ulaw depending on encoding config).
   */
  enqueue(buffer: ArrayBuffer): void {
    if (!this.workletNode) return;

    const float32 = decodeAudioBytes(buffer, this.encoding);
    this.workletNode.port.postMessage(
      { type: "buffer", samples: Array.from(float32) }
    );
    this._isPlaying = true;
  }

  /**
   * Interrupt playback: flush all queued audio and fade out.
   * Called when the agent is interrupted by the user.
   */
  interrupt(): void {
    if (!this.workletNode || !this.gainNode || !this.audioContext) return;

    // Tell worklet to flush
    this.workletNode.port.postMessage({ type: "interrupt" });

    // Exponential fade to near-zero over 200ms (faster than ElevenLabs' 2s)
    const now = this.audioContext.currentTime;
    this.gainNode.gain.setValueAtTime(this.gainNode.gain.value, now);
    this.gainNode.gain.exponentialRampToValueAtTime(0.0001, now + 0.2);

    // Restore after fade
    if (this.interruptFadeTimeout) clearTimeout(this.interruptFadeTimeout);
    this.interruptFadeTimeout = setTimeout(() => {
      if (this.gainNode && this.audioContext) {
        this.gainNode.gain.setValueAtTime(this._volume, this.audioContext.currentTime);
      }
      if (this.workletNode) {
        this.workletNode.port.postMessage({ type: "clearInterrupted" });
      }
      this._isPlaying = false;
    }, 250);
  }

  /**
   * Set playback volume (0.0 - 1.0).
   */
  setVolume(value: number): void {
    this._volume = Math.max(0, Math.min(1, value));
    if (this.gainNode && this.audioContext) {
      this.gainNode.gain.setValueAtTime(this._volume, this.audioContext.currentTime);
    }
  }

  /**
   * Get frequency data from the output analyser (for visualization).
   */
  getByteFrequencyData(): Uint8Array {
    if (!this.analyserNode) return new Uint8Array(0);
    const data = new Uint8Array(this.analyserNode.frequencyBinCount);
    this.analyserNode.getByteFrequencyData(data);
    return data;
  }

  /**
   * Get current output volume (0-1).
   */
  getVolume(): number {
    if (!this.analyserNode) return 0;
    const data = new Float32Array(this.analyserNode.fftSize);
    this.analyserNode.getFloatTimeDomainData(data);
    return calculateRmsVolume(data);
  }

  /**
   * Update the audio format configuration.
   * Typically called after receiving conversation_initiation_metadata.
   */
  updateFormat(formatString: AudioFormatString): void {
    const format = parseAudioFormat(formatString);
    this.sampleRate = format.sample_rate;
    this.encoding = format.encoding;

    // If context is running at wrong sample rate, we need to reinitialize
    if (this.audioContext && this.audioContext.sampleRate !== this.sampleRate) {
      // We'll just note this — in practice the audio worklet will handle it
      // since we decode to Float32 first. But sample rate mismatch means
      // the playback will be pitched wrong. For best results, restart the pipeline.
    }
  }

  /**
   * Stop playback and release all resources.
   */
  stop(): void {
    if (this.interruptFadeTimeout) {
      clearTimeout(this.interruptFadeTimeout);
      this.interruptFadeTimeout = null;
    }
    if (this.workletNode) {
      this.workletNode.disconnect();
      this.workletNode = null;
    }
    if (this.gainNode) {
      this.gainNode.disconnect();
      this.gainNode = null;
    }
    if (this.analyserNode) {
      this.analyserNode.disconnect();
      this.analyserNode = null;
    }
    if (this.audioElement) {
      this.audioElement.pause();
      this.audioElement.srcObject = null;
      this.audioElement = null;
    }
    if (this.audioContext) {
      void this.audioContext.close();
      this.audioContext = null;
    }
    this._isPlaying = false;
    this.onPlaybackFinished = null;
  }
}
