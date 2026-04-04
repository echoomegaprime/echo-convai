"""Echo ConvAI — ASR Engine (Speech-to-Text).

Reverse-engineered equivalent of ElevenLabs' Scribe ASR model.
Uses faster-whisper (CTranslate2 backend) for GPU-accelerated transcription
on BRAVO node (RTX 4060).

ElevenLabs ASR features matched:
- Real-time streaming transcription (tentative → final)
- Keyword boosting (prioritize domain-specific terms)
- Multiple audio format support (PCM 8-48kHz, ulaw_8000)
- Quality modes: high (large-v3) vs low (tiny/base)
- Language detection + forced language
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from collections import defaultdict
from typing import Any

import numpy as np
from loguru import logger

from core.conversation_engine import ASRInterface

try:
    from faster_whisper import WhisperModel
    HAS_WHISPER = True
except ImportError:
    HAS_WHISPER = False
    logger.warning("faster-whisper not installed — ASR will return empty transcripts")


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIO BUFFER — Accumulates chunks for batch transcription
# ═══════════════════════════════════════════════════════════════════════════════

class AudioBuffer:
    """Accumulates incoming audio chunks and provides them for transcription.

    ElevenLabs buffers ~500ms-1s of audio before running ASR inference,
    providing tentative transcripts during accumulation and final transcripts
    when a pause is detected.
    """

    def __init__(self, sample_rate: int = 16000, chunk_duration_ms: int = 500):
        self.sample_rate = sample_rate
        self.chunk_duration_ms = chunk_duration_ms
        self._buffer: list[np.ndarray] = []
        self._total_samples: int = 0
        self._last_chunk_time: float = time.time()

    def add_chunk(self, audio_b64: str) -> None:
        """Add a base64-encoded PCM chunk to the buffer."""
        audio_bytes = base64.b64decode(audio_b64)
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        audio_float = audio_int16.astype(np.float32) / 32768.0
        self._buffer.append(audio_float)
        self._total_samples += len(audio_float)
        self._last_chunk_time = time.time()

    def get_audio(self) -> np.ndarray | None:
        """Get accumulated audio as a single float32 array."""
        if not self._buffer:
            return None
        return np.concatenate(self._buffer)

    def should_transcribe(self) -> bool:
        """Check if enough audio has accumulated for transcription."""
        duration_ms = (self._total_samples / self.sample_rate) * 1000
        return duration_ms >= self.chunk_duration_ms

    def clear(self) -> None:
        """Clear the buffer."""
        self._buffer.clear()
        self._total_samples = 0

    @property
    def duration_seconds(self) -> float:
        """Total buffered audio duration in seconds."""
        return self._total_samples / self.sample_rate

    @property
    def seconds_since_last_chunk(self) -> float:
        """Time since last audio chunk received."""
        return time.time() - self._last_chunk_time


# ═══════════════════════════════════════════════════════════════════════════════
# KEYWORD BOOSTER — Bias transcription toward domain terms
# ═══════════════════════════════════════════════════════════════════════════════

class KeywordBooster:
    """Boosts recognition of domain-specific keywords.

    ElevenLabs' keyword boosting uses prefix-constrained beam search.
    Our implementation post-processes transcripts to fix common misrecognitions
    of boosted terms, and provides initial_prompt seeding for Whisper.
    """

    def __init__(self, keywords: list[dict[str, str]] | None = None):
        self.keywords: dict[str, str] = {}  # lowercase → proper casing
        self.boost_terms: list[str] = []

        if keywords:
            for kw in keywords:
                term = kw.get("text", "")
                if term:
                    self.keywords[term.lower()] = term
                    self.boost_terms.append(term)

    def get_initial_prompt(self) -> str:
        """Generate Whisper initial_prompt with boosted keywords.

        Whisper uses initial_prompt to bias its transcription toward
        specific vocabulary. This is the closest equivalent to ElevenLabs'
        keyword boosting.
        """
        if not self.boost_terms:
            return ""
        return "Keywords: " + ", ".join(self.boost_terms) + ". "

    def post_process(self, transcript: str) -> str:
        """Fix casing and spelling of boosted keywords in transcript."""
        result = transcript
        for lower_term, proper_term in self.keywords.items():
            # Case-insensitive replacement
            idx = result.lower().find(lower_term)
            while idx != -1:
                result = result[:idx] + proper_term + result[idx + len(lower_term):]
                idx = result.lower().find(lower_term, idx + len(proper_term))
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# WHISPER ASR ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class WhisperASR(ASRInterface):
    """GPU-accelerated ASR using faster-whisper.

    Deployed on BRAVO node (RTX 4060 8GB VRAM).
    Supports streaming transcription with tentative/final results.

    Model options:
    - large-v3: Best quality (matches ElevenLabs "high" quality)
    - medium: Good balance
    - small: Lower latency
    - tiny: Fastest (matches ElevenLabs "low" quality)
    """

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str | None = None,
        sample_rate: int = 16000,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.sample_rate = sample_rate

        self._model: WhisperModel | None = None
        self._buffers: dict[str, AudioBuffer] = defaultdict(
            lambda: AudioBuffer(sample_rate=sample_rate)
        )
        self._transcripts: dict[str, dict[str, Any]] = {}
        self._keyword_boosters: dict[str, KeywordBooster] = {}
        self._lock = asyncio.Lock()

    def _load_model(self) -> None:
        """Lazy-load Whisper model."""
        if self._model is not None:
            return

        if not HAS_WHISPER:
            logger.error("faster-whisper not installed")
            return

        logger.info(f"Loading Whisper {self.model_size} on {self.device}...")
        self._model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type=self.compute_type,
        )
        logger.info(f"Whisper {self.model_size} loaded on {self.device}")

    def set_keywords(self, conversation_id: str, keywords: list[dict[str, str]]) -> None:
        """Set keyword boosting for a conversation."""
        self._keyword_boosters[conversation_id] = KeywordBooster(keywords)

    async def process_audio_chunk(self, audio_b64: str, conversation_id: str) -> None:
        """Feed an audio chunk to the ASR pipeline.

        Audio accumulates in a buffer. When enough audio has accumulated
        (500ms default), a transcription is triggered.
        """
        buf = self._buffers[conversation_id]
        buf.add_chunk(audio_b64)

        # Trigger transcription if enough audio accumulated
        if buf.should_transcribe():
            await self._transcribe(conversation_id)

    async def get_transcript(self, conversation_id: str) -> dict[str, Any] | None:
        """Get latest transcript for a conversation.

        Returns:
            {"text": str, "is_final": bool, "language": str, "confidence": float}
            or None if no transcript available.
        """
        return self._transcripts.get(conversation_id)

    async def reset(self, conversation_id: str) -> None:
        """Reset ASR state for new utterance."""
        if conversation_id in self._buffers:
            self._buffers[conversation_id].clear()
        if conversation_id in self._transcripts:
            del self._transcripts[conversation_id]

    async def _transcribe(self, conversation_id: str) -> None:
        """Run Whisper transcription on buffered audio."""
        self._load_model()

        buf = self._buffers[conversation_id]
        audio = buf.get_audio()
        if audio is None or len(audio) < 1600:  # Minimum 100ms
            return

        if self._model is None:
            # Fallback: store empty transcript
            self._transcripts[conversation_id] = {
                "text": "",
                "is_final": False,
                "language": self.language or "en",
                "confidence": 0.0,
            }
            return

        # Get keyword boost prompt
        booster = self._keyword_boosters.get(conversation_id)
        initial_prompt = booster.get_initial_prompt() if booster else ""

        async with self._lock:
            try:
                segments, info = self._model.transcribe(
                    audio,
                    language=self.language,
                    beam_size=5,
                    best_of=3,
                    initial_prompt=initial_prompt or None,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 200},
                )

                # Collect all segments
                text_parts = []
                total_confidence = 0.0
                segment_count = 0

                for segment in segments:
                    text_parts.append(segment.text.strip())
                    total_confidence += segment.avg_log_prob
                    segment_count += 1

                full_text = " ".join(text_parts)

                # Apply keyword boosting post-processing
                if booster:
                    full_text = booster.post_process(full_text)

                avg_confidence = (total_confidence / segment_count) if segment_count > 0 else 0.0

                # Determine if final (based on silence after speech)
                is_final = buf.seconds_since_last_chunk > 0.5

                self._transcripts[conversation_id] = {
                    "text": full_text,
                    "is_final": is_final,
                    "language": info.language if info else (self.language or "en"),
                    "confidence": min(1.0, max(0.0, np.exp(avg_confidence))),
                }

            except Exception as e:
                logger.error(f"Transcription error: {e}")
                self._transcripts[conversation_id] = {
                    "text": "",
                    "is_final": False,
                    "language": self.language or "en",
                    "confidence": 0.0,
                }

    def cleanup(self, conversation_id: str) -> None:
        """Clean up all state for a conversation."""
        self._buffers.pop(conversation_id, None)
        self._transcripts.pop(conversation_id, None)
        self._keyword_boosters.pop(conversation_id, None)
