"""Echo ConvAI — ASR Service (Speech-to-Text).

Production-ready FastAPI application for real-time speech recognition.
Uses faster-whisper (CTranslate2) with large-v3-turbo model on GPU.

Features:
- WebSocket streaming: client sends PCM audio chunks, server returns tentative + final transcripts
- REST endpoint for file upload transcription
- Keyword boosting via initial_prompt seeding + post-processing
- Multiple input formats: PCM 16-bit at 8/16/22/24/44.1/48 kHz, ulaw_8000
- Silero VAD integration for voice activity detection
- Language detection or explicit language_code
- Target latency: < 200ms from end-of-utterance to final transcript
- GPU memory: ~1.5GB for large-v3-turbo

Port: 8801
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import struct
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

import numpy as np
from fastapi import FastAPI, File, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

SERVICE_NAME = "echo-asr"
SERVICE_VERSION = "1.0.0"
DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_DEVICE = "cuda"
DEFAULT_COMPUTE_TYPE = "float16"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_BEAM_SIZE = 5
DEFAULT_BEST_OF = 3
MIN_AUDIO_SAMPLES = 1600  # 100ms at 16kHz
VAD_THRESHOLD = 0.5
SILENCE_DURATION_MS = 300  # ms of silence to trigger final transcript
CHUNK_DURATION_MS = 500  # ms of audio to buffer before transcription
MAX_AUDIO_DURATION_S = 600  # 10 minutes max per file upload
PORT = 8801

# Silero VAD constants
SILERO_VAD_SAMPLE_RATE = 16000
SILERO_VAD_WINDOW_SIZE = 512  # 32ms at 16kHz


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS & MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class AudioFormat(str, Enum):
    """Supported input audio formats."""
    PCM_8000 = "pcm_8000"
    PCM_16000 = "pcm_16000"
    PCM_22050 = "pcm_22050"
    PCM_24000 = "pcm_24000"
    PCM_44100 = "pcm_44100"
    PCM_48000 = "pcm_48000"
    ULAW_8000 = "ulaw_8000"


class CommitStrategy(str, Enum):
    """When to finalize transcripts."""
    VAD = "vad"
    MANUAL = "manual"


class WordTimestamp(BaseModel):
    """Individual word with timing information."""
    word: str
    start: float
    end: float
    probability: float


class TranscriptResult(BaseModel):
    """Transcript output from ASR."""
    text: str = ""
    is_final: bool = False
    language: str = "en"
    confidence: float = 0.0
    words: list[WordTimestamp] = Field(default_factory=list)
    duration_s: float = 0.0
    latency_ms: float = 0.0


class StreamConfig(BaseModel):
    """Configuration for a streaming ASR session."""
    audio_format: AudioFormat = AudioFormat.PCM_16000
    language_code: str | None = None
    commit_strategy: CommitStrategy = CommitStrategy.VAD
    keywords: list[dict[str, str]] = Field(default_factory=list)
    vad_threshold: float = VAD_THRESHOLD
    vad_silence_threshold_secs: float = 0.3
    min_speech_duration_ms: int = 250
    min_silence_duration_ms: int = 200
    include_timestamps: bool = True
    previous_text: str | None = None
    model_id: str = DEFAULT_MODEL


class TranscribeRequest(BaseModel):
    """Request for REST transcription endpoint."""
    language_code: str | None = None
    keywords: list[dict[str, str]] = Field(default_factory=list)
    include_timestamps: bool = True
    model_id: str = DEFAULT_MODEL


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    service: str
    version: str
    model_loaded: bool
    model_name: str
    device: str
    uptime_seconds: float
    active_sessions: int
    total_transcriptions: int


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIO FORMAT UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def get_sample_rate(audio_format: AudioFormat) -> int:
    """Extract sample rate from AudioFormat enum."""
    rate_map = {
        AudioFormat.PCM_8000: 8000,
        AudioFormat.PCM_16000: 16000,
        AudioFormat.PCM_22050: 22050,
        AudioFormat.PCM_24000: 24000,
        AudioFormat.PCM_44100: 44100,
        AudioFormat.PCM_48000: 48000,
        AudioFormat.ULAW_8000: 8000,
    }
    return rate_map.get(audio_format, DEFAULT_SAMPLE_RATE)


def ulaw_to_pcm16(ulaw_bytes: bytes) -> np.ndarray:
    """Decode G.711 u-law encoded bytes to 16-bit PCM numpy array."""
    BIAS = 0x84
    SEGMENT_DECODE = [0, 132, 396, 924, 1980, 4092, 8316, 16764]

    samples = []
    for byte_val in ulaw_bytes:
        byte_val = ~byte_val & 0xFF
        sign = byte_val & 0x80
        exponent = (byte_val >> 4) & 0x07
        mantissa = byte_val & 0x0F
        sample = SEGMENT_DECODE[exponent] + (mantissa * 2 + 1) * (1 << (exponent + 3)) - BIAS
        if sign:
            sample = -sample
        samples.append(sample)
    return np.array(samples, dtype=np.int16)


def resample_audio(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample audio from one sample rate to another using linear interpolation."""
    if from_rate == to_rate:
        return audio
    try:
        import soxr
        return soxr.resample(audio.astype(np.float32), from_rate, to_rate).astype(audio.dtype)
    except ImportError:
        ratio = to_rate / from_rate
        new_length = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, new_length)
        return np.interp(indices, np.arange(len(audio)), audio.astype(np.float64)).astype(audio.dtype)


def decode_audio_chunk(raw_bytes: bytes, audio_format: AudioFormat) -> np.ndarray:
    """Decode raw audio bytes into float32 numpy array at 16kHz."""
    source_rate = get_sample_rate(audio_format)

    if audio_format == AudioFormat.ULAW_8000:
        pcm16 = ulaw_to_pcm16(raw_bytes)
    else:
        pcm16 = np.frombuffer(raw_bytes, dtype=np.int16)

    # Resample to 16kHz if needed (Whisper expects 16kHz)
    if source_rate != SILERO_VAD_SAMPLE_RATE:
        pcm16 = resample_audio(pcm16, source_rate, SILERO_VAD_SAMPLE_RATE)

    return pcm16.astype(np.float32) / 32768.0


# ═══════════════════════════════════════════════════════════════════════════════
# KEYWORD BOOSTER
# ═══════════════════════════════════════════════════════════════════════════════

class KeywordBooster:
    """Boosts recognition of domain-specific keywords via prompt seeding and post-processing."""

    def __init__(self, keywords: list[dict[str, str]] | None = None) -> None:
        self.keywords: dict[str, str] = {}
        self.boost_terms: list[str] = []

        if keywords:
            for kw in keywords:
                term = kw.get("text", "")
                if term:
                    self.keywords[term.lower()] = term
                    self.boost_terms.append(term)

    def get_initial_prompt(self) -> str:
        """Generate Whisper initial_prompt with boosted keywords."""
        if not self.boost_terms:
            return ""
        return "Keywords: " + ", ".join(self.boost_terms) + ". "

    def post_process(self, transcript: str) -> str:
        """Fix casing and spelling of boosted keywords in transcript."""
        result = transcript
        for lower_term, proper_term in self.keywords.items():
            idx = result.lower().find(lower_term)
            while idx != -1:
                result = result[:idx] + proper_term + result[idx + len(lower_term):]
                idx = result.lower().find(lower_term, idx + len(proper_term))
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# SILERO VAD WRAPPER
# ═══════════════════════════════════════════════════════════════════════════════

class SileroVAD:
    """Voice Activity Detection using Silero VAD model.

    Detects speech segments in audio to determine when the user has stopped speaking.
    """

    def __init__(self, threshold: float = VAD_THRESHOLD) -> None:
        self.threshold = threshold
        self._model: Any = None
        self._lock = asyncio.Lock()

    def _load_model(self) -> None:
        """Lazy-load Silero VAD model."""
        if self._model is not None:
            return
        try:
            import torch
            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=False,
                trust_repo=True,
            )
            self._model = model
            logger.info("Silero VAD model loaded")
        except Exception as e:
            logger.error(f"Failed to load Silero VAD: {e}")

    def get_speech_prob(self, audio_chunk: np.ndarray) -> float:
        """Get speech probability for an audio chunk.

        Args:
            audio_chunk: Float32 audio at 16kHz, should be 512 samples (32ms).

        Returns:
            Speech probability between 0.0 and 1.0.
        """
        self._load_model()
        if self._model is None:
            return 0.5  # Assume speech if VAD unavailable

        try:
            import torch
            tensor = torch.from_numpy(audio_chunk).float()
            if len(tensor) < SILERO_VAD_WINDOW_SIZE:
                tensor = torch.nn.functional.pad(tensor, (0, SILERO_VAD_WINDOW_SIZE - len(tensor)))
            prob = self._model(tensor, SILERO_VAD_SAMPLE_RATE).item()
            return float(prob)
        except Exception as e:
            logger.warning(f"VAD inference error: {e}")
            return 0.5

    def reset(self) -> None:
        """Reset VAD state for new utterance."""
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# STREAMING ASR SESSION
# ═══════════════════════════════════════════════════════════════════════════════

class ASRSession:
    """Manages state for a single streaming ASR session.

    Accumulates audio, runs VAD, triggers transcription, and tracks whether
    the current transcript is tentative or final.
    """

    def __init__(
        self,
        session_id: str,
        config: StreamConfig,
        whisper_model: Any,
        vad: SileroVAD,
    ) -> None:
        self.session_id = session_id
        self.config = config
        self.whisper_model = whisper_model
        self.vad = vad
        self.booster = KeywordBooster(config.keywords) if config.keywords else None

        # Audio buffer
        self._buffer: list[np.ndarray] = []
        self._total_samples: int = 0
        self._last_chunk_time: float = time.time()

        # VAD state
        self._speech_detected: bool = False
        self._silence_start: float | None = None
        self._vad_scores: list[float] = []
        self._speech_start_time: float | None = None

        # Transcript state
        self._last_tentative: str = ""
        self._committed_text: str = ""

        # Metrics
        self._transcription_count: int = 0

    @property
    def buffered_duration_s(self) -> float:
        """Total buffered audio duration in seconds."""
        return self._total_samples / SILERO_VAD_SAMPLE_RATE

    @property
    def seconds_since_last_chunk(self) -> float:
        """Time since last audio chunk received."""
        return time.time() - self._last_chunk_time

    def add_audio(self, audio_b64: str) -> None:
        """Add base64-encoded audio chunk to the buffer."""
        raw_bytes = base64.b64decode(audio_b64)
        audio_float = decode_audio_chunk(raw_bytes, self.config.audio_format)
        self._buffer.append(audio_float)
        self._total_samples += len(audio_float)
        self._last_chunk_time = time.time()

    def add_raw_audio(self, raw_bytes: bytes) -> None:
        """Add raw audio bytes to the buffer."""
        audio_float = decode_audio_chunk(raw_bytes, self.config.audio_format)
        self._buffer.append(audio_float)
        self._total_samples += len(audio_float)
        self._last_chunk_time = time.time()

    def run_vad(self) -> float:
        """Run VAD on the most recent audio and update speech state.

        Returns the current VAD score.
        """
        if not self._buffer:
            return 0.0

        latest = self._buffer[-1]
        # Process in 512-sample windows
        scores = []
        for i in range(0, len(latest) - SILERO_VAD_WINDOW_SIZE + 1, SILERO_VAD_WINDOW_SIZE):
            window = latest[i:i + SILERO_VAD_WINDOW_SIZE]
            score = self.vad.get_speech_prob(window)
            scores.append(score)

        if not scores:
            if len(latest) > 0:
                score = self.vad.get_speech_prob(latest)
                scores.append(score)
            else:
                return 0.0

        avg_score = float(np.mean(scores))
        self._vad_scores.append(avg_score)

        now = time.time()
        if avg_score >= self.config.vad_threshold:
            self._speech_detected = True
            self._silence_start = None
            if self._speech_start_time is None:
                self._speech_start_time = now
        else:
            if self._speech_detected and self._silence_start is None:
                self._silence_start = now

        return avg_score

    def should_transcribe(self) -> bool:
        """Check if enough audio has accumulated for transcription."""
        duration_ms = self.buffered_duration_s * 1000
        return duration_ms >= CHUNK_DURATION_MS and self._speech_detected

    def is_end_of_utterance(self) -> bool:
        """Check if VAD indicates the user has stopped speaking."""
        if self.config.commit_strategy == CommitStrategy.MANUAL:
            return False

        if not self._speech_detected or self._silence_start is None:
            return False

        silence_duration = time.time() - self._silence_start
        return silence_duration >= self.config.vad_silence_threshold_secs

    def transcribe(self) -> TranscriptResult:
        """Run Whisper transcription on buffered audio.

        Returns a TranscriptResult with the current transcript.
        """
        if not self._buffer or self._total_samples < MIN_AUDIO_SAMPLES:
            return TranscriptResult()

        t_start = time.time()
        audio = np.concatenate(self._buffer)

        if self.whisper_model is None:
            return TranscriptResult(
                latency_ms=(time.time() - t_start) * 1000,
                duration_s=len(audio) / SILERO_VAD_SAMPLE_RATE,
            )

        initial_prompt = ""
        if self.booster:
            initial_prompt = self.booster.get_initial_prompt()
        if self.config.previous_text:
            initial_prompt = (self.config.previous_text + " " + initial_prompt).strip()

        try:
            segments, info = self.whisper_model.transcribe(
                audio,
                language=self.config.language_code,
                beam_size=DEFAULT_BEAM_SIZE,
                best_of=DEFAULT_BEST_OF,
                initial_prompt=initial_prompt or None,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": self.config.min_silence_duration_ms,
                    "min_speech_duration_ms": self.config.min_speech_duration_ms,
                    "threshold": self.config.vad_threshold,
                },
                word_timestamps=self.config.include_timestamps,
            )

            text_parts: list[str] = []
            words: list[WordTimestamp] = []
            total_log_prob: float = 0.0
            segment_count: int = 0

            for segment in segments:
                text_parts.append(segment.text.strip())
                total_log_prob += segment.avg_log_prob
                segment_count += 1

                if self.config.include_timestamps and hasattr(segment, "words") and segment.words:
                    for w in segment.words:
                        words.append(WordTimestamp(
                            word=w.word.strip(),
                            start=round(w.start, 3),
                            end=round(w.end, 3),
                            probability=round(w.probability, 3),
                        ))

            full_text = " ".join(text_parts).strip()
            if self.booster:
                full_text = self.booster.post_process(full_text)

            avg_log_prob = total_log_prob / segment_count if segment_count > 0 else -1.0
            confidence = min(1.0, max(0.0, float(np.exp(avg_log_prob))))
            is_final = self.is_end_of_utterance()

            latency_ms = (time.time() - t_start) * 1000
            self._transcription_count += 1
            self._last_tentative = full_text

            return TranscriptResult(
                text=full_text,
                is_final=is_final,
                language=info.language if info else (self.config.language_code or "en"),
                confidence=round(confidence, 3),
                words=words,
                duration_s=round(len(audio) / SILERO_VAD_SAMPLE_RATE, 3),
                latency_ms=round(latency_ms, 1),
            )

        except Exception as e:
            logger.error(f"Transcription error in session {self.session_id}: {e}")
            return TranscriptResult(
                latency_ms=(time.time() - t_start) * 1000,
                duration_s=len(audio) / SILERO_VAD_SAMPLE_RATE,
            )

    def commit(self) -> TranscriptResult:
        """Force-commit the current buffer as a final transcript."""
        result = self.transcribe()
        result.is_final = True
        if result.text:
            self._committed_text += (" " + result.text).strip()
        self.clear_buffer()
        return result

    def clear_buffer(self) -> None:
        """Clear audio buffer and reset VAD state for new utterance."""
        self._buffer.clear()
        self._total_samples = 0
        self._speech_detected = False
        self._silence_start = None
        self._vad_scores.clear()
        self._speech_start_time = None
        self._last_tentative = ""
        self.vad.reset()

    def cleanup(self) -> None:
        """Full cleanup of session state."""
        self.clear_buffer()
        self._committed_text = ""
        self._transcription_count = 0


# ═══════════════════════════════════════════════════════════════════════════════
# ASR ENGINE (GLOBAL SINGLETON)
# ═══════════════════════════════════════════════════════════════════════════════

class ASREngine:
    """Global ASR engine that manages the Whisper model and active sessions."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model: Any = None
        self._vad = SileroVAD()
        self._sessions: dict[str, ASRSession] = {}
        self._lock = asyncio.Lock()
        self._start_time = time.time()
        self._total_transcriptions = 0

    def load_model(self) -> None:
        """Load the Whisper model."""
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
            logger.info(f"Loading Whisper model '{self.model_name}' on {self.device} ({self.compute_type})...")
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
            logger.info(f"Whisper model '{self.model_name}' loaded successfully on {self.device}")
        except ImportError:
            logger.error("faster-whisper not installed — ASR will return empty transcripts")
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")

    @property
    def model_loaded(self) -> bool:
        """Check if the Whisper model is loaded."""
        return self._model is not None

    @property
    def uptime_seconds(self) -> float:
        """Service uptime in seconds."""
        return time.time() - self._start_time

    @property
    def active_session_count(self) -> int:
        """Number of active streaming sessions."""
        return len(self._sessions)

    def create_session(self, config: StreamConfig) -> ASRSession:
        """Create a new streaming ASR session."""
        session_id = str(uuid.uuid4())
        session = ASRSession(
            session_id=session_id,
            config=config,
            whisper_model=self._model,
            vad=SileroVAD(threshold=config.vad_threshold),
        )
        self._sessions[session_id] = session
        logger.info(
            f"ASR session created: {session_id} "
            f"(format={config.audio_format}, lang={config.language_code}, "
            f"commit={config.commit_strategy})"
        )
        return session

    def get_session(self, session_id: str) -> ASRSession | None:
        """Get an active session by ID."""
        return self._sessions.get(session_id)

    def remove_session(self, session_id: str) -> None:
        """Remove and clean up a session."""
        session = self._sessions.pop(session_id, None)
        if session:
            session.cleanup()
            logger.info(f"ASR session removed: {session_id}")

    async def transcribe_file(
        self,
        audio_data: bytes,
        language_code: str | None = None,
        keywords: list[dict[str, str]] | None = None,
        include_timestamps: bool = True,
    ) -> TranscriptResult:
        """Transcribe a complete audio file (WAV, PCM, etc.)."""
        self.load_model()

        if self._model is None:
            return TranscriptResult()

        t_start = time.time()
        booster = KeywordBooster(keywords) if keywords else None
        initial_prompt = booster.get_initial_prompt() if booster else ""

        try:
            # Try reading as WAV first
            audio_array = self._decode_file(audio_data)

            async with self._lock:
                segments, info = self._model.transcribe(
                    audio_array,
                    language=language_code,
                    beam_size=DEFAULT_BEAM_SIZE,
                    best_of=DEFAULT_BEST_OF,
                    initial_prompt=initial_prompt or None,
                    vad_filter=True,
                    word_timestamps=include_timestamps,
                )

                text_parts: list[str] = []
                words: list[WordTimestamp] = []
                total_log_prob: float = 0.0
                segment_count: int = 0

                for segment in segments:
                    text_parts.append(segment.text.strip())
                    total_log_prob += segment.avg_log_prob
                    segment_count += 1

                    if include_timestamps and hasattr(segment, "words") and segment.words:
                        for w in segment.words:
                            words.append(WordTimestamp(
                                word=w.word.strip(),
                                start=round(w.start, 3),
                                end=round(w.end, 3),
                                probability=round(w.probability, 3),
                            ))

                full_text = " ".join(text_parts).strip()
                if booster:
                    full_text = booster.post_process(full_text)

                avg_log_prob = total_log_prob / segment_count if segment_count > 0 else -1.0
                confidence = min(1.0, max(0.0, float(np.exp(avg_log_prob))))

                self._total_transcriptions += 1
                latency_ms = (time.time() - t_start) * 1000

                return TranscriptResult(
                    text=full_text,
                    is_final=True,
                    language=info.language if info else (language_code or "en"),
                    confidence=round(confidence, 3),
                    words=words,
                    duration_s=round(len(audio_array) / SILERO_VAD_SAMPLE_RATE, 3),
                    latency_ms=round(latency_ms, 1),
                )

        except Exception as e:
            logger.error(f"File transcription error: {e}")
            return TranscriptResult(latency_ms=(time.time() - t_start) * 1000)

    def _decode_file(self, audio_data: bytes) -> np.ndarray:
        """Decode audio file bytes into float32 numpy array at 16kHz."""
        # Check for WAV header
        if audio_data[:4] == b"RIFF" and audio_data[8:12] == b"WAVE":
            return self._decode_wav(audio_data)

        # Try raw PCM 16-bit as fallback
        pcm16 = np.frombuffer(audio_data, dtype=np.int16)
        return pcm16.astype(np.float32) / 32768.0

    def _decode_wav(self, wav_data: bytes) -> np.ndarray:
        """Decode WAV file to float32 numpy array at 16kHz."""
        try:
            import soundfile as sf
            audio, sr = sf.read(io.BytesIO(wav_data), dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)  # Mix to mono
            if sr != SILERO_VAD_SAMPLE_RATE:
                audio = resample_audio(audio, sr, SILERO_VAD_SAMPLE_RATE)
            return audio
        except ImportError:
            pass

        # Manual WAV parsing fallback
        fmt_offset = wav_data.find(b"fmt ")
        if fmt_offset < 0:
            raise ValueError("Invalid WAV: no fmt chunk")

        chunk_size = struct.unpack_from("<I", wav_data, fmt_offset + 4)[0]
        audio_fmt = struct.unpack_from("<H", wav_data, fmt_offset + 8)[0]
        channels = struct.unpack_from("<H", wav_data, fmt_offset + 10)[0]
        sample_rate = struct.unpack_from("<I", wav_data, fmt_offset + 12)[0]
        bits_per_sample = struct.unpack_from("<H", wav_data, fmt_offset + 22)[0]

        data_offset = wav_data.find(b"data")
        if data_offset < 0:
            raise ValueError("Invalid WAV: no data chunk")

        data_size = struct.unpack_from("<I", wav_data, data_offset + 4)[0]
        raw_data = wav_data[data_offset + 8:data_offset + 8 + data_size]

        if bits_per_sample == 16:
            pcm = np.frombuffer(raw_data, dtype=np.int16)
        elif bits_per_sample == 32:
            if audio_fmt == 3:  # IEEE float
                pcm = np.frombuffer(raw_data, dtype=np.float32)
                pcm = (pcm * 32767).astype(np.int16)
            else:
                pcm = np.frombuffer(raw_data, dtype=np.int32)
                pcm = (pcm >> 16).astype(np.int16)
        else:
            raise ValueError(f"Unsupported bits per sample: {bits_per_sample}")

        # Mix to mono if stereo
        if channels > 1:
            pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)

        audio_f32 = pcm.astype(np.float32) / 32768.0

        if sample_rate != SILERO_VAD_SAMPLE_RATE:
            audio_f32 = resample_audio(audio_f32, sample_rate, SILERO_VAD_SAMPLE_RATE)

        return audio_f32


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL ENGINE INSTANCE
# ═══════════════════════════════════════════════════════════════════════════════

engine = ASREngine()


# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: load model on startup."""
    logger.info(f"Starting {SERVICE_NAME} v{SERVICE_VERSION} on port {PORT}")
    engine.load_model()
    engine._vad._load_model()
    logger.info(f"{SERVICE_NAME} ready — model_loaded={engine.model_loaded}")
    yield
    logger.info(f"Shutting down {SERVICE_NAME}")
    for sid in list(engine._sessions.keys()):
        engine.remove_session(sid)


app = FastAPI(
    title="Echo ConvAI ASR Service",
    description="Real-time speech recognition with faster-whisper and Silero VAD",
    version=SERVICE_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(
        status="healthy" if engine.model_loaded else "degraded",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        model_loaded=engine.model_loaded,
        model_name=engine.model_name,
        device=engine.device,
        uptime_seconds=round(engine.uptime_seconds, 1),
        active_sessions=engine.active_session_count,
        total_transcriptions=engine._total_transcriptions,
    )


# ── REST Transcription ───────────────────────────────────────────────────────

@app.post("/transcribe", response_model=TranscriptResult)
async def transcribe(
    file: UploadFile = File(...),
    language_code: str | None = Query(None, description="Language code (e.g., 'en', 'es')"),
    include_timestamps: bool = Query(True, description="Include word-level timestamps"),
) -> TranscriptResult:
    """Transcribe an uploaded audio file."""
    logger.info(f"REST transcribe: filename={file.filename}, content_type={file.content_type}")
    audio_data = await file.read()

    if len(audio_data) == 0:
        return TranscriptResult()

    result = await engine.transcribe_file(
        audio_data=audio_data,
        language_code=language_code,
        include_timestamps=include_timestamps,
    )
    logger.info(f"REST transcribe complete: text_len={len(result.text)}, latency={result.latency_ms}ms")
    return result


# ── WebSocket Streaming ──────────────────────────────────────────────────────

@app.websocket("/stream")
async def websocket_stream(
    ws: WebSocket,
    audio_format: str = "pcm_16000",
    language_code: str | None = None,
    commit_strategy: str = "vad",
    vad_threshold: float = VAD_THRESHOLD,
    vad_silence_threshold_secs: float = 0.3,
    min_speech_duration_ms: int = 250,
    min_silence_duration_ms: int = 200,
    include_timestamps: bool = True,
    model_id: str = DEFAULT_MODEL,
) -> None:
    """WebSocket endpoint for real-time streaming ASR.

    Protocol:
    - Client sends JSON messages:
      {"message_type": "input_audio_chunk", "audio_base_64": "...", "commit": false}
      {"message_type": "input_audio_chunk", "audio_base_64": "", "commit": true}
    - Server sends JSON events:
      {"type": "session_started", "session_id": "..."}
      {"type": "partial_transcript", "text": "...", "confidence": 0.95, ...}
      {"type": "committed_transcript", "text": "...", "is_final": true, ...}
      {"type": "error", "message": "..."}
    """
    await ws.accept()

    try:
        fmt = AudioFormat(audio_format)
    except ValueError:
        fmt = AudioFormat.PCM_16000

    try:
        strategy = CommitStrategy(commit_strategy)
    except ValueError:
        strategy = CommitStrategy.VAD

    config = StreamConfig(
        audio_format=fmt,
        language_code=language_code,
        commit_strategy=strategy,
        vad_threshold=vad_threshold,
        vad_silence_threshold_secs=vad_silence_threshold_secs,
        min_speech_duration_ms=min_speech_duration_ms,
        min_silence_duration_ms=min_silence_duration_ms,
        include_timestamps=include_timestamps,
        model_id=model_id,
    )

    session = engine.create_session(config)

    # Send session_started
    await ws.send_json({
        "type": "session_started",
        "session_id": session.session_id,
        "model_id": engine.model_name,
        "audio_format": audio_format,
        "language_code": language_code,
        "commit_strategy": commit_strategy,
    })

    # Background task: periodic transcription check
    transcription_task: asyncio.Task[None] | None = None
    stop_event = asyncio.Event()

    async def transcription_loop() -> None:
        """Periodically check for transcription and end-of-utterance."""
        while not stop_event.is_set():
            await asyncio.sleep(0.1)  # 100ms check interval

            if stop_event.is_set():
                break

            if not session.should_transcribe():
                continue

            # Run transcription
            try:
                result = session.transcribe()

                if result.text:
                    if result.is_final:
                        await ws.send_json({
                            "type": "committed_transcript",
                            **result.model_dump(),
                        })
                        engine._total_transcriptions += 1
                        session.clear_buffer()
                    else:
                        await ws.send_json({
                            "type": "partial_transcript",
                            **result.model_dump(),
                        })
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"Transcription loop error: {e}")

    transcription_task = asyncio.create_task(transcription_loop())

    try:
        while True:
            raw_msg = await ws.receive_text()
            try:
                msg = __import__("json").loads(raw_msg)
            except Exception:
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("message_type", "")

            if msg_type == "input_audio_chunk":
                audio_b64 = msg.get("audio_base_64", "")
                commit = msg.get("commit", False)
                previous_text = msg.get("previous_text")

                if previous_text and not session.config.previous_text:
                    session.config.previous_text = previous_text

                if audio_b64:
                    session.add_audio(audio_b64)
                    session.run_vad()

                if commit:
                    result = session.commit()
                    if result.text:
                        await ws.send_json({
                            "type": "committed_transcript",
                            **result.model_dump(),
                        })
                        engine._total_transcriptions += 1

            elif msg_type == "configure":
                # Allow runtime reconfiguration
                keywords = msg.get("keywords")
                if keywords:
                    session.booster = KeywordBooster(keywords)
                lang = msg.get("language_code")
                if lang:
                    session.config.language_code = lang

            elif msg_type == "reset":
                session.clear_buffer()
                await ws.send_json({"type": "reset_acknowledged"})

            else:
                await ws.send_json({
                    "type": "error",
                    "message": f"Unknown message_type: {msg_type}",
                })

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: session={session.session_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        stop_event.set()
        if transcription_task:
            transcription_task.cancel()
            try:
                await transcription_task
            except asyncio.CancelledError:
                pass
        engine.remove_session(session.session_id)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting {SERVICE_NAME} on port {PORT}")
    uvicorn.run(
        "asr_service:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        ws_max_size=16 * 1024 * 1024,
    )
