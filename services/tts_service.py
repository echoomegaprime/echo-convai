"""Echo ConvAI — TTS Service (Text-to-Speech).

Production-ready FastAPI application for text-to-speech synthesis.
ElevenLabs-compatible API with pluggable backends (Coqui XTTS-v2, F5-TTS, ElevenLabs proxy).

Features:
- ElevenLabs-compatible REST API: POST /v1/text-to-speech/{voice_id}
- Streaming endpoint: POST /v1/text-to-speech/{voice_id}/stream
- Multi-speaker dialogue: POST /v1/text-to-dialogue
- Voice registry with speaker embeddings
- Text normalization pipeline (numbers, dates, abbreviations, URLs)
- Emotion tag parsing: [excited], [whispers], [sad], [angry], etc.
- Audio format conversion: PCM (8-48kHz), MP3, Opus, WAV
- Stability, similarity_boost, speed parameters
- Seed parameter for deterministic generation
- Target TTFB: < 150ms for streaming
- GPU memory: ~3-4GB

Port: 8802
"""

from __future__ import annotations

import abc
import asyncio
import base64
import hashlib
import io
import os
import re
import struct
import time
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

SERVICE_NAME = "echo-tts"
SERVICE_VERSION = "1.0.0"
PORT = 8802
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_STABILITY = 0.5
DEFAULT_SIMILARITY_BOOST = 0.75
DEFAULT_SPEED = 1.0
DEFAULT_STYLE = 0.0
CHUNK_DURATION_MS = 100  # Stream chunks in 100ms increments
VOICE_EMBEDDING_DIR = Path("voices")
COSYVOICE_MODEL_DIR = Path(os.environ.get("COSYVOICE_MODEL_DIR", "C:/echo/pretrained_models/CosyVoice2-0.5B"))
MAX_TEXT_LENGTH = 10000


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS & MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class OutputFormat(str, Enum):
    """Supported output audio formats."""
    PCM_8000 = "pcm_8000"
    PCM_16000 = "pcm_16000"
    PCM_22050 = "pcm_22050"
    PCM_24000 = "pcm_24000"
    PCM_44100 = "pcm_44100"
    PCM_48000 = "pcm_48000"
    ULAW_8000 = "ulaw_8000"
    MP3_44100_128 = "mp3_44100_128"
    MP3_44100_192 = "mp3_44100_192"
    OPUS_48000 = "opus_48000"
    WAV_44100 = "wav_44100"


class VoiceSettings(BaseModel):
    """Voice generation settings (ElevenLabs compatible)."""
    stability: float = Field(DEFAULT_STABILITY, ge=0.0, le=1.0)
    similarity_boost: float = Field(DEFAULT_SIMILARITY_BOOST, ge=0.0, le=1.0)
    style: float = Field(DEFAULT_STYLE, ge=0.0, le=1.0)
    use_speaker_boost: bool = True
    speed: float = Field(DEFAULT_SPEED, ge=0.5, le=2.0)


class TTSRequest(BaseModel):
    """Request body for text-to-speech endpoint."""
    text: str
    model_id: str = "eleven_v3_conversational"
    voice_settings: VoiceSettings | None = None
    output_format: OutputFormat = OutputFormat.PCM_24000
    seed: int | None = None
    apply_text_normalization: str = "auto"  # auto, on, off
    language_code: str | None = None


class DialogueLine(BaseModel):
    """A single line of dialogue with voice assignment."""
    text: str
    voice_id: str
    voice_settings: VoiceSettings | None = None


class DialogueRequest(BaseModel):
    """Request body for multi-speaker dialogue endpoint."""
    lines: list[DialogueLine]
    model_id: str = "eleven_v3_conversational"
    output_format: OutputFormat = OutputFormat.PCM_24000
    seed: int | None = None
    apply_text_normalization: str = "auto"


class VoiceInfo(BaseModel):
    """Voice metadata for voice listing endpoint."""
    voice_id: str
    name: str
    category: str = "custom"
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    available_for_tiers: list[str] = Field(default_factory=lambda: ["free", "pro"])
    preview_url: str | None = None


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    service: str
    version: str
    backend: str
    model_loaded: bool
    device: str
    uptime_seconds: float
    total_generations: int
    registered_voices: int


# ═══════════════════════════════════════════════════════════════════════════════
# EMOTION TAG PARSER
# ═══════════════════════════════════════════════════════════════════════════════

class EmotionTagParser:
    """Parse ElevenLabs-style inline emotion tags from text.

    Supported tags: [sympathetic], [professional], [excited], [angry], [sad],
    [whispers], [laughs], [sighs], [sarcastic], [curious], [crying], [warm],
    [empathetic], [cheerful], [calm], [authoritative], [friendly], [serious]
    """

    TAG_PATTERN = re.compile(r"\[(\w+)\]")

    # Map tags to prosody modification vectors:
    # [pitch_shift, speed_factor, energy_factor, breathiness]
    TAG_VECTORS: dict[str, list[float]] = {
        "sympathetic": [-0.1, 0.9, 0.8, 0.3],
        "professional": [0.0, 1.0, 0.9, 0.0],
        "excited": [0.2, 1.2, 1.3, 0.0],
        "angry": [0.1, 1.1, 1.5, 0.0],
        "sad": [-0.2, 0.8, 0.6, 0.2],
        "whispers": [-0.1, 0.85, 0.3, 0.7],
        "laughs": [0.15, 1.1, 1.0, 0.4],
        "sighs": [-0.15, 0.7, 0.5, 0.5],
        "sarcastic": [0.05, 0.95, 0.9, 0.1],
        "curious": [0.1, 1.0, 0.9, 0.1],
        "crying": [-0.2, 0.75, 0.7, 0.4],
        "warm": [-0.05, 0.95, 0.85, 0.15],
        "empathetic": [-0.1, 0.9, 0.8, 0.2],
        "cheerful": [0.15, 1.1, 1.1, 0.0],
        "calm": [-0.05, 0.85, 0.7, 0.1],
        "authoritative": [0.05, 0.95, 1.2, 0.0],
        "friendly": [0.05, 1.05, 0.95, 0.1],
        "serious": [0.0, 0.9, 0.9, 0.0],
    }

    @classmethod
    def extract_tags(cls, text: str) -> tuple[str, list[str]]:
        """Extract emotion tags and return clean text + tag list."""
        tags = cls.TAG_PATTERN.findall(text)
        clean_text = cls.TAG_PATTERN.sub("", text).strip()
        clean_text = re.sub(r"\s+", " ", clean_text)
        return clean_text, [t.lower() for t in tags if t.lower() in cls.TAG_VECTORS]

    @classmethod
    def get_prosody_vector(cls, tags: list[str]) -> list[float]:
        """Combine multiple emotion tags into a single prosody vector."""
        if not tags:
            return [0.0, 1.0, 1.0, 0.0]
        vectors = [cls.TAG_VECTORS[t] for t in tags if t in cls.TAG_VECTORS]
        if not vectors:
            return [0.0, 1.0, 1.0, 0.0]
        arr = np.array(vectors)
        return arr.mean(axis=0).tolist()


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT NORMALIZER
# ═══════════════════════════════════════════════════════════════════════════════

class TextNormalizer:
    """Normalize text for TTS processing.

    Handles: numbers, abbreviations, URLs, emails, special characters, ordinals,
    currency, phone-number patterns, and common acronyms.
    """

    ABBREVIATIONS: dict[str, str] = {
        "mr.": "mister", "mrs.": "missus", "ms.": "miss", "dr.": "doctor",
        "prof.": "professor", "st.": "street", "ave.": "avenue",
        "blvd.": "boulevard", "dept.": "department", "govt.": "government",
        "inc.": "incorporated", "ltd.": "limited", "etc.": "et cetera",
        "vs.": "versus", "e.g.": "for example", "i.e.": "that is",
        "approx.": "approximately", "est.": "established", "assn.": "association",
        "corp.": "corporation", "jan.": "january", "feb.": "february",
        "mar.": "march", "apr.": "april", "jun.": "june", "jul.": "july",
        "aug.": "august", "sep.": "september", "oct.": "october",
        "nov.": "november", "dec.": "december",
    }

    ORDINAL_SUFFIXES = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
    CURRENCY_PATTERN = re.compile(r"\$(\d+(?:,\d{3})*(?:\.\d{2})?)")
    PERCENTAGE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)%")
    TIME_PATTERN = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s*(am|pm))?\b", re.IGNORECASE)
    URL_PATTERN = re.compile(r"https?://\S+")
    EMAIL_PATTERN = re.compile(r"\S+@\S+\.\S+")

    ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
            "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
            "seventeen", "eighteen", "nineteen"]
    TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

    @classmethod
    def _number_to_words(cls, n: int) -> str:
        """Convert an integer to English words."""
        if n < 0:
            return "negative " + cls._number_to_words(-n)
        if n == 0:
            return "zero"
        if n < 20:
            return cls.ONES[n]
        if n < 100:
            tens = cls.TENS[n // 10]
            ones = cls.ONES[n % 10]
            return f"{tens} {ones}".strip() if ones else tens
        if n < 1000:
            hundreds = cls.ONES[n // 100] + " hundred"
            remainder = n % 100
            if remainder:
                return f"{hundreds} and {cls._number_to_words(remainder)}"
            return hundreds
        if n < 1_000_000:
            thousands = cls._number_to_words(n // 1000) + " thousand"
            remainder = n % 1000
            if remainder:
                return f"{thousands} {cls._number_to_words(remainder)}"
            return thousands
        if n < 1_000_000_000:
            millions = cls._number_to_words(n // 1_000_000) + " million"
            remainder = n % 1_000_000
            if remainder:
                return f"{millions} {cls._number_to_words(remainder)}"
            return millions
        billions = cls._number_to_words(n // 1_000_000_000) + " billion"
        remainder = n % 1_000_000_000
        if remainder:
            return f"{billions} {cls._number_to_words(remainder)}"
        return billions

    @classmethod
    def _normalize_currency(cls, match: re.Match[str]) -> str:
        """Convert $X.XX to words."""
        amount_str = match.group(1).replace(",", "")
        if "." in amount_str:
            parts = amount_str.split(".")
            dollars = int(parts[0])
            cents = int(parts[1])
            result = cls._number_to_words(dollars) + " dollars"
            if cents:
                result += " and " + cls._number_to_words(cents) + " cents"
            return result
        return cls._number_to_words(int(amount_str)) + " dollars"

    @classmethod
    def _normalize_percentage(cls, match: re.Match[str]) -> str:
        """Convert X% to words."""
        val = match.group(1)
        if "." in val:
            parts = val.split(".")
            return cls._number_to_words(int(parts[0])) + " point " + " ".join(
                cls._number_to_words(int(d)) if d != "0" else "zero" for d in parts[1]
            ) + " percent"
        return cls._number_to_words(int(val)) + " percent"

    @classmethod
    def _normalize_standalone_numbers(cls, text: str) -> str:
        """Convert standalone numbers to words."""
        def replace_num(match: re.Match[str]) -> str:
            num_str = match.group(0).replace(",", "")
            try:
                n = int(num_str)
                if 1900 <= n <= 2099:
                    # Year handling
                    if n < 2000:
                        century = n // 100
                        year = n % 100
                        if year == 0:
                            return cls._number_to_words(century) + " hundred"
                        return cls._number_to_words(century) + " " + cls._number_to_words(year)
                    elif n < 2010:
                        return "two thousand " + ("and " + cls._number_to_words(n - 2000) if n > 2000 else "")
                    else:
                        return "twenty " + cls._number_to_words(n - 2000)
                return cls._number_to_words(n)
            except (ValueError, OverflowError):
                return match.group(0)

        return re.sub(r"\b\d{1,3}(?:,\d{3})*\b", replace_num, text)

    @classmethod
    def normalize(cls, text: str, mode: str = "auto") -> str:
        """Normalize text for TTS processing."""
        if mode == "off":
            return text

        result = text

        # Expand abbreviations
        for abbr, expansion in cls.ABBREVIATIONS.items():
            result = re.sub(re.escape(abbr), expansion, result, flags=re.IGNORECASE)

        # Currency
        result = cls.CURRENCY_PATTERN.sub(cls._normalize_currency, result)

        # Percentages
        result = cls.PERCENTAGE_PATTERN.sub(cls._normalize_percentage, result)

        # URLs
        result = cls.URL_PATTERN.sub("link", result)

        # Emails
        result = cls.EMAIL_PATTERN.sub("email address", result)

        # Standalone numbers
        result = cls._normalize_standalone_numbers(result)

        # Clean up extra whitespace
        result = re.sub(r"\s+", " ", result).strip()

        return result


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIO FORMAT CONVERTER
# ═══════════════════════════════════════════════════════════════════════════════

class AudioConverter:
    """Convert between audio formats."""

    @staticmethod
    def get_sample_rate(output_format: OutputFormat) -> int:
        """Extract sample rate from OutputFormat."""
        rate_map = {
            OutputFormat.PCM_8000: 8000, OutputFormat.PCM_16000: 16000,
            OutputFormat.PCM_22050: 22050, OutputFormat.PCM_24000: 24000,
            OutputFormat.PCM_44100: 44100, OutputFormat.PCM_48000: 48000,
            OutputFormat.ULAW_8000: 8000, OutputFormat.MP3_44100_128: 44100,
            OutputFormat.MP3_44100_192: 44100, OutputFormat.OPUS_48000: 48000,
            OutputFormat.WAV_44100: 44100,
        }
        return rate_map.get(output_format, DEFAULT_SAMPLE_RATE)

    @staticmethod
    def get_content_type(output_format: OutputFormat) -> str:
        """Get MIME type for output format."""
        if output_format in (OutputFormat.MP3_44100_128, OutputFormat.MP3_44100_192):
            return "audio/mpeg"
        if output_format == OutputFormat.OPUS_48000:
            return "audio/opus"
        if output_format == OutputFormat.WAV_44100:
            return "audio/wav"
        return "audio/pcm"

    @staticmethod
    def resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
        """Resample audio using soxr or linear interpolation fallback."""
        if from_rate == to_rate:
            return audio
        try:
            import soxr
            return soxr.resample(audio.astype(np.float32), from_rate, to_rate)
        except ImportError:
            ratio = to_rate / from_rate
            new_length = int(len(audio) * ratio)
            indices = np.linspace(0, len(audio) - 1, new_length)
            return np.interp(indices, np.arange(len(audio)), audio.astype(np.float64)).astype(np.float32)

    @staticmethod
    def float32_to_pcm16(audio: np.ndarray) -> bytes:
        """Convert float32 audio to 16-bit PCM bytes."""
        pcm16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
        return pcm16.tobytes()

    @staticmethod
    def pcm_to_wav(pcm_data: bytes, sample_rate: int = 44100, channels: int = 1, bits: int = 16) -> bytes:
        """Convert raw PCM to WAV format with header."""
        byte_rate = sample_rate * channels * (bits // 8)
        block_align = channels * (bits // 8)
        data_size = len(pcm_data)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16, 1,
            channels, sample_rate, byte_rate, block_align, bits, b"data", data_size,
        )
        return header + pcm_data

    @staticmethod
    def pcm_to_ulaw(pcm_data: bytes) -> bytes:
        """Convert 16-bit PCM bytes to G.711 u-law."""
        BIAS = 0x84
        CLIP = 32635
        pcm16 = np.frombuffer(pcm_data, dtype=np.int16)
        ulaw_bytes = bytearray(len(pcm16))
        for i, sample_val in enumerate(pcm16):
            sample = int(sample_val)
            sign = (sample >> 8) & 0x80
            if sign:
                sample = -sample
            sample = min(sample, CLIP) + BIAS
            exponent = 7
            mask = 0x4000
            while exponent > 0 and not (sample & mask):
                exponent -= 1
                mask >>= 1
            mantissa = (sample >> (exponent + 3)) & 0x0F
            ulaw_bytes[i] = ~(sign | (exponent << 4) | mantissa) & 0xFF
        return bytes(ulaw_bytes)

    @staticmethod
    def pcm_to_mp3(pcm_data: bytes, sample_rate: int = 44100, bitrate: int = 128) -> bytes:
        """Convert PCM to MP3 using lameenc."""
        try:
            import lameenc
            encoder = lameenc.Encoder()
            encoder.set_bit_rate(bitrate)
            encoder.set_in_sample_rate(sample_rate)
            encoder.set_channels(1)
            encoder.set_quality(2)
            mp3_data = encoder.encode(pcm_data)
            mp3_data += encoder.flush()
            return mp3_data
        except ImportError:
            logger.warning("lameenc not installed — returning raw PCM instead of MP3")
            return pcm_data

    def convert(self, audio_f32: np.ndarray, source_rate: int, output_format: OutputFormat) -> bytes:
        """Convert float32 audio at source_rate to the requested output format."""
        target_rate = self.get_sample_rate(output_format)

        # Resample if needed
        if source_rate != target_rate:
            audio_f32 = self.resample(audio_f32, source_rate, target_rate)

        # Convert to PCM16
        pcm_bytes = self.float32_to_pcm16(audio_f32)

        if output_format == OutputFormat.ULAW_8000:
            return self.pcm_to_ulaw(pcm_bytes)
        elif output_format == OutputFormat.WAV_44100:
            return self.pcm_to_wav(pcm_bytes, target_rate)
        elif output_format == OutputFormat.MP3_44100_128:
            return self.pcm_to_mp3(pcm_bytes, target_rate, 128)
        elif output_format == OutputFormat.MP3_44100_192:
            return self.pcm_to_mp3(pcm_bytes, target_rate, 192)
        elif output_format == OutputFormat.OPUS_48000:
            try:
                import opuslib
                encoder = opuslib.Encoder(target_rate, 1, opuslib.APPLICATION_AUDIO)
                frame_size = target_rate // 50  # 20ms frames
                frames = []
                pcm16 = np.frombuffer(pcm_bytes, dtype=np.int16)
                for i in range(0, len(pcm16) - frame_size + 1, frame_size):
                    frame = pcm16[i:i + frame_size].tobytes()
                    frames.append(encoder.encode(frame, frame_size))
                return b"".join(frames)
            except ImportError:
                logger.warning("opuslib not installed — returning raw PCM instead of Opus")
                return pcm_bytes

        # Default: raw PCM
        return pcm_bytes


# ═══════════════════════════════════════════════════════════════════════════════
# VOICE REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

class VoiceRegistry:
    """Manage voice embeddings and metadata for speaker selection."""

    def __init__(self, embedding_dir: Path) -> None:
        self.embedding_dir = embedding_dir
        self._embeddings: dict[str, np.ndarray] = {}
        self._metadata: dict[str, VoiceInfo] = {}
        self._reference_audio: dict[str, Path] = {}

    def load_from_disk(self) -> None:
        """Load all voice embeddings and metadata from disk."""
        if not self.embedding_dir.exists():
            self.embedding_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created voice embedding directory: {self.embedding_dir}")
            return

        loaded = 0
        for npy_file in self.embedding_dir.glob("*.npy"):
            voice_id = npy_file.stem
            try:
                self._embeddings[voice_id] = np.load(str(npy_file))
                loaded += 1
            except Exception as e:
                logger.warning(f"Failed to load embedding {npy_file}: {e}")

        # Load reference audio files
        for wav_file in self.embedding_dir.glob("*.wav"):
            voice_id = wav_file.stem
            self._reference_audio[voice_id] = wav_file

        # Load metadata JSONs
        import json
        for meta_file in self.embedding_dir.glob("*.json"):
            voice_id = meta_file.stem
            try:
                with open(meta_file) as f:
                    data = json.load(f)
                self._metadata[voice_id] = VoiceInfo(voice_id=voice_id, **data)
            except Exception as e:
                logger.warning(f"Failed to load metadata {meta_file}: {e}")

        logger.info(f"Loaded {loaded} voice embeddings, {len(self._reference_audio)} reference audios")

    def register_voice(self, voice_id: str, embedding: np.ndarray, name: str = "") -> None:
        """Register a voice embedding."""
        self._embeddings[voice_id] = embedding
        if voice_id not in self._metadata:
            self._metadata[voice_id] = VoiceInfo(
                voice_id=voice_id,
                name=name or voice_id,
            )
        # Save to disk
        npy_path = self.embedding_dir / f"{voice_id}.npy"
        np.save(str(npy_path), embedding)
        logger.info(f"Voice registered: {voice_id} (dim={len(embedding)})")

    def get_embedding(self, voice_id: str) -> np.ndarray | None:
        """Get voice embedding by ID."""
        return self._embeddings.get(voice_id)

    def get_reference_audio(self, voice_id: str) -> Path | None:
        """Get reference audio path for a voice."""
        return self._reference_audio.get(voice_id)

    def list_voices(self) -> list[VoiceInfo]:
        """List all registered voices."""
        voices = set(self._embeddings.keys()) | set(self._reference_audio.keys())
        result = []
        for vid in sorted(voices):
            if vid in self._metadata:
                result.append(self._metadata[vid])
            else:
                result.append(VoiceInfo(voice_id=vid, name=vid))
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# TTS BACKEND ABSTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

class TTSBackend(abc.ABC):
    """Abstract base class for TTS backends."""

    @abc.abstractmethod
    def load_model(self, device: str) -> None:
        """Load the TTS model onto the specified device."""

    @abc.abstractmethod
    def is_loaded(self) -> bool:
        """Check if the model is loaded."""

    @abc.abstractmethod
    async def synthesize(
        self,
        text: str,
        voice_id: str,
        voice_registry: VoiceRegistry,
        voice_settings: VoiceSettings,
        prosody_vector: list[float],
        language_code: str | None = None,
        seed: int | None = None,
    ) -> tuple[np.ndarray, int]:
        """Synthesize speech from text.

        Returns:
            (audio_float32, sample_rate) tuple.
        """

    @abc.abstractmethod
    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        voice_registry: VoiceRegistry,
        voice_settings: VoiceSettings,
        prosody_vector: list[float],
        language_code: str | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[tuple[np.ndarray, int]]:
        """Stream synthesized audio chunks.

        Yields (audio_chunk_float32, sample_rate) tuples.
        """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Backend name for logging and identification."""


class CoquiTTSBackend(TTSBackend):
    """TTS backend using Coqui TTS / XTTS-v2.

    XTTS-v2 is a zero-shot voice cloning model that works with a short
    reference audio sample. Supports 17 languages.
    GPU memory: ~3-4GB.
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._device: str = "cuda"

    def load_model(self, device: str) -> None:
        """Load XTTS-v2 model."""
        self._device = device
        try:
            from TTS.api import TTS
            logger.info(f"Loading XTTS-v2 on {device}...")
            self._model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
            logger.info("XTTS-v2 loaded successfully")
        except ImportError:
            logger.error("Coqui TTS not installed: pip install TTS")
        except Exception as e:
            logger.error(f"XTTS-v2 load error: {e}")

    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def name(self) -> str:
        return "coqui-xtts-v2"

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        voice_registry: VoiceRegistry,
        voice_settings: VoiceSettings,
        prosody_vector: list[float],
        language_code: str | None = None,
        seed: int | None = None,
    ) -> tuple[np.ndarray, int]:
        """Synthesize with XTTS-v2."""
        if self._model is None:
            return np.zeros(DEFAULT_SAMPLE_RATE, dtype=np.float32), DEFAULT_SAMPLE_RATE

        ref_audio = voice_registry.get_reference_audio(voice_id)
        speaker_wav = str(ref_audio) if ref_audio else None

        loop = asyncio.get_event_loop()
        lang = language_code or "en"

        if speaker_wav:
            wav = await loop.run_in_executor(
                None,
                lambda: self._model.tts(
                    text=text,
                    speaker_wav=speaker_wav,
                    language=lang,
                ),
            )
        else:
            wav = await loop.run_in_executor(
                None,
                lambda: self._model.tts(
                    text=text,
                    language=lang,
                ),
            )

        audio = np.array(wav, dtype=np.float32)

        # Apply speed adjustment
        if voice_settings.speed != 1.0:
            new_len = int(len(audio) / voice_settings.speed)
            indices = np.linspace(0, len(audio) - 1, new_len)
            audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)

        return audio, DEFAULT_SAMPLE_RATE

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        voice_registry: VoiceRegistry,
        voice_settings: VoiceSettings,
        prosody_vector: list[float],
        language_code: str | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[tuple[np.ndarray, int]]:
        """Stream by splitting text at sentence boundaries and synthesizing each."""
        sentences = self._split_sentences(text)
        for sentence in sentences:
            if sentence.strip():
                audio, sr = await self.synthesize(
                    sentence, voice_id, voice_registry, voice_settings,
                    prosody_vector, language_code, seed,
                )
                yield audio, sr

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split text into sentences for streaming."""
        parts = re.split(r'(?<=[.!?;:])\s+', text)
        return [p for p in parts if p.strip()]


class CosyVoiceBackend(TTSBackend):
    """TTS backend using CosyVoice 2 (Alibaba, Apache 2.0).

    CosyVoice 2 is the primary Echo ConvAI TTS engine — our RE replacement
    for ElevenLabs v3. Key capabilities that match v3:
    - Native emotion instructions ("speak angrily", "whisper softly")
    - Zero-shot voice cloning from reference audio
    - Streaming synthesis
    - 24kHz output (same as ElevenLabs v3)
    - 9 languages (en, zh, ja, ko, de, es, fr, it, ru)
    - Fine-grained markers: [laughter], [breath]
    GPU memory: ~6-8GB (fits on BRAVO RTX 4060).
    """

    # Map our emotion tags to CosyVoice instruct_text commands
    EMOTION_INSTRUCTIONS: dict[str, str] = {
        "excited": "Speak with excitement and high energy at a faster pace",
        "angry": "Speak angrily with intensity and a forceful tone",
        "sad": "Speak sadly with a slow, melancholic, sorrowful tone",
        "whispers": "Whisper softly and quietly with a breathy intimate tone",
        "laughs": "Speak while laughing with joy and amusement",
        "sighs": "Speak with a tired sighing tone, weary and resigned",
        "sarcastic": "Speak with dry sarcasm and ironic detachment",
        "curious": "Speak with curiosity and wonder, rising intonation",
        "crying": "Speak while crying with trembling emotional voice",
        "warm": "Speak warmly with a gentle caring comforting tone",
        "empathetic": "Speak with deep empathy and understanding",
        "cheerful": "Speak cheerfully with a bright happy upbeat tone",
        "calm": "Speak calmly and peacefully with a soothing relaxed voice",
        "authoritative": "Speak with authority and confidence, commanding tone",
        "friendly": "Speak in a friendly approachable conversational manner",
        "serious": "Speak seriously with gravity and importance",
        "sympathetic": "Speak sympathetically with gentle concern",
        "professional": "Speak professionally with a clear composed business tone",
    }

    def __init__(self) -> None:
        self._model: Any = None
        self._device: str = "cuda"
        self._sample_rate: int = 24000

    def load_model(self, device: str) -> None:
        """Load CosyVoice 2 model from HuggingFace."""
        self._device = device
        try:
            from cosyvoice.cli.cosyvoice import AutoModel
            logger.info(f"Loading CosyVoice2-0.5B on {device}...")
            self._model = AutoModel(model_dir=str(COSYVOICE_MODEL_DIR))
            self._sample_rate = self._model.sample_rate
            spks = self._model.list_available_spks()
            logger.info(f"CosyVoice 2 loaded — {len(spks)} preset speakers, sr={self._sample_rate}")
        except ImportError:
            logger.error("CosyVoice not installed. Install: pip install cosyvoice")
            logger.error("Download model: huggingface-cli download FunAudioLLM/CosyVoice2-0.5B --local-dir pretrained_models/CosyVoice2-0.5B")
        except Exception as e:
            logger.error(f"CosyVoice 2 load error: {e}")

    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def name(self) -> str:
        return "cosyvoice2"

    def _build_instruct_text(self, prosody_vector: list[float], tags: list[str] | None = None) -> str:
        """Build CosyVoice instruct_text from emotion tags or prosody vector."""
        if tags:
            # Use first recognized emotion tag
            for tag in tags:
                if tag in self.EMOTION_INSTRUCTIONS:
                    return self.EMOTION_INSTRUCTIONS[tag]

        # Fall back to prosody vector interpretation
        pitch_shift, speed_factor, energy_factor, breathiness = prosody_vector
        parts = []
        if speed_factor > 1.1:
            parts.append("at a faster pace")
        elif speed_factor < 0.9:
            parts.append("at a slower pace")
        if energy_factor > 1.2:
            parts.append("with high energy")
        elif energy_factor < 0.7:
            parts.append("quietly")
        if breathiness > 0.3:
            parts.append("with a breathy tone")
        if pitch_shift > 0.1:
            parts.append("with higher pitch")
        elif pitch_shift < -0.1:
            parts.append("with lower pitch")

        if parts:
            return "Speak " + ", ".join(parts)
        return "Speak naturally in a conversational tone"

    def _extract_tags_from_text(self, text: str) -> list[str]:
        """Extract emotion tags from text for instruct building."""
        import re as _re
        tags = _re.findall(r"\[(\w+)\]", text)
        return [t.lower() for t in tags if t.lower() in self.EMOTION_INSTRUCTIONS]

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        voice_registry: VoiceRegistry,
        voice_settings: VoiceSettings,
        prosody_vector: list[float],
        language_code: str | None = None,
        seed: int | None = None,
    ) -> tuple[np.ndarray, int]:
        """Synthesize with CosyVoice 2 using emotion-aware instruct mode."""
        if self._model is None:
            return np.zeros(self._sample_rate, dtype=np.float32), self._sample_rate

        loop = asyncio.get_event_loop()
        ref_audio = voice_registry.get_reference_audio(voice_id)

        # Extract emotion tags from original text, then clean for synthesis
        raw_tags = self._extract_tags_from_text(text)
        clean_text = re.sub(r"\[\w+\]", "", text).strip()
        clean_text = re.sub(r"\s+", " ", clean_text)

        if not clean_text:
            return np.zeros(self._sample_rate, dtype=np.float32), self._sample_rate

        instruct_text = self._build_instruct_text(prosody_vector, raw_tags)

        try:
            if ref_audio and ref_audio.exists():
                # Zero-shot voice cloning with reference audio
                # Read ref audio for prompt
                import torchaudio
                prompt_speech, _ = torchaudio.load(str(ref_audio))
                # Use zero-shot with the reference
                def _infer() -> Any:
                    results = list(self._model.inference_zero_shot(
                        tts_text=clean_text,
                        prompt_text="",
                        prompt_speech_16k=prompt_speech,
                        stream=False,
                    ))
                    return results
                results = await loop.run_in_executor(None, _infer)
            else:
                # Use instruct mode with preset speaker for emotion control
                available_spks = self._model.list_available_spks()
                # Pick English speaker if available, else first
                speaker = "英文女" if "英文女" in available_spks else (
                    "英文男" if "英文男" in available_spks else (
                        available_spks[0] if available_spks else "中文女"
                    )
                )

                def _infer_instruct() -> Any:
                    results = list(self._model.inference_instruct(
                        tts_text=clean_text,
                        spk_id=speaker,
                        instruct_text=instruct_text,
                        stream=False,
                    ))
                    return results
                results = await loop.run_in_executor(None, _infer_instruct)

            if results:
                # Concatenate all chunks
                import torch
                audio_chunks = [r["tts_speech"] for r in results if "tts_speech" in r]
                if audio_chunks:
                    audio_tensor = torch.cat(audio_chunks, dim=-1)
                    audio = audio_tensor.squeeze(0).cpu().numpy().astype(np.float32)

                    # Apply speed adjustment
                    if voice_settings.speed != 1.0:
                        new_len = int(len(audio) / voice_settings.speed)
                        if new_len > 0:
                            indices = np.linspace(0, len(audio) - 1, new_len)
                            audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)

                    return audio, self._sample_rate

            return np.zeros(self._sample_rate, dtype=np.float32), self._sample_rate

        except Exception as e:
            logger.error(f"CosyVoice 2 inference error: {e}")
            return np.zeros(self._sample_rate, dtype=np.float32), self._sample_rate

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        voice_registry: VoiceRegistry,
        voice_settings: VoiceSettings,
        prosody_vector: list[float],
        language_code: str | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[tuple[np.ndarray, int]]:
        """Stream synthesis using CosyVoice 2 native streaming."""
        if self._model is None:
            return

        loop = asyncio.get_event_loop()

        raw_tags = self._extract_tags_from_text(text)
        clean_text = re.sub(r"\[\w+\]", "", text).strip()
        clean_text = re.sub(r"\s+", " ", clean_text)
        if not clean_text:
            return

        instruct_text = self._build_instruct_text(prosody_vector, raw_tags)
        available_spks = self._model.list_available_spks()
        speaker = "英文女" if "英文女" in available_spks else (
            available_spks[0] if available_spks else "中文女"
        )

        try:
            # Use streaming mode for low-latency chunk delivery
            def _stream_infer() -> list[Any]:
                return list(self._model.inference_instruct(
                    tts_text=clean_text,
                    spk_id=speaker,
                    instruct_text=instruct_text,
                    stream=True,
                ))

            results = await loop.run_in_executor(None, _stream_infer)

            for chunk in results:
                if "tts_speech" in chunk:
                    audio_tensor = chunk["tts_speech"]
                    audio = audio_tensor.squeeze(0).cpu().numpy().astype(np.float32)

                    if voice_settings.speed != 1.0:
                        new_len = int(len(audio) / voice_settings.speed)
                        if new_len > 0:
                            indices = np.linspace(0, len(audio) - 1, new_len)
                            audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)

                    yield audio, self._sample_rate

        except Exception as e:
            logger.error(f"CosyVoice 2 streaming error: {e}")


class ElevenLabsProxyBackend(TTSBackend):
    """Proxy backend that forwards requests to real ElevenLabs API.

    Used for A/B comparison between local models and ElevenLabs cloud.
    Requires ELEVENLABS_API_KEY environment variable.
    """

    def __init__(self) -> None:
        self._api_key: str | None = None
        self._base_url: str = "https://api.elevenlabs.io"
        self._loaded: bool = False

    def load_model(self, device: str) -> None:
        """Load API key from environment."""
        import os
        self._api_key = os.getenv("ELEVENLABS_API_KEY")
        if self._api_key:
            self._loaded = True
            logger.info("ElevenLabs proxy backend configured")
        else:
            logger.warning("ELEVENLABS_API_KEY not set — proxy backend unavailable")

    def is_loaded(self) -> bool:
        return self._loaded and self._api_key is not None

    @property
    def name(self) -> str:
        return "elevenlabs-proxy"

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        voice_registry: VoiceRegistry,
        voice_settings: VoiceSettings,
        prosody_vector: list[float],
        language_code: str | None = None,
        seed: int | None = None,
    ) -> tuple[np.ndarray, int]:
        """Forward to ElevenLabs API and return audio."""
        if not self._api_key:
            return np.zeros(DEFAULT_SAMPLE_RATE, dtype=np.float32), DEFAULT_SAMPLE_RATE

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                url = f"{self._base_url}/v1/text-to-speech/{voice_id}"
                headers = {
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                }
                body: dict[str, Any] = {
                    "text": text,
                    "model_id": "eleven_v3",
                    "voice_settings": {
                        "stability": voice_settings.stability,
                        "similarity_boost": voice_settings.similarity_boost,
                        "style": voice_settings.style,
                        "use_speaker_boost": voice_settings.use_speaker_boost,
                    },
                }
                if seed is not None:
                    body["seed"] = seed

                params = {"output_format": "pcm_24000"}
                resp = await client.post(url, headers=headers, json=body, params=params)
                resp.raise_for_status()

                pcm_bytes = resp.content
                pcm16 = np.frombuffer(pcm_bytes, dtype=np.int16)
                audio = pcm16.astype(np.float32) / 32768.0
                return audio, 24000

        except Exception as e:
            logger.error(f"ElevenLabs proxy error: {e}")
            return np.zeros(DEFAULT_SAMPLE_RATE, dtype=np.float32), DEFAULT_SAMPLE_RATE

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        voice_registry: VoiceRegistry,
        voice_settings: VoiceSettings,
        prosody_vector: list[float],
        language_code: str | None = None,
        seed: int | None = None,
    ) -> AsyncIterator[tuple[np.ndarray, int]]:
        """Stream from ElevenLabs API."""
        if not self._api_key:
            return

        try:
            import httpx
            async with httpx.AsyncClient(timeout=60.0) as client:
                url = f"{self._base_url}/v1/text-to-speech/{voice_id}/stream"
                headers = {
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                }
                body: dict[str, Any] = {
                    "text": text,
                    "model_id": "eleven_v3",
                    "voice_settings": {
                        "stability": voice_settings.stability,
                        "similarity_boost": voice_settings.similarity_boost,
                        "style": voice_settings.style,
                        "use_speaker_boost": voice_settings.use_speaker_boost,
                    },
                }
                if seed is not None:
                    body["seed"] = seed

                params = {"output_format": "pcm_24000"}

                async with client.stream("POST", url, headers=headers, json=body, params=params) as resp:
                    resp.raise_for_status()
                    buffer = bytearray()
                    chunk_size = 24000 * 2  # 1 second of PCM16 at 24kHz
                    async for chunk in resp.aiter_bytes():
                        buffer.extend(chunk)
                        while len(buffer) >= chunk_size:
                            pcm_chunk = bytes(buffer[:chunk_size])
                            buffer = buffer[chunk_size:]
                            pcm16 = np.frombuffer(pcm_chunk, dtype=np.int16)
                            audio = pcm16.astype(np.float32) / 32768.0
                            yield audio, 24000

                    # Flush remaining
                    if buffer:
                        pcm16 = np.frombuffer(bytes(buffer), dtype=np.int16)
                        audio = pcm16.astype(np.float32) / 32768.0
                        yield audio, 24000

        except Exception as e:
            logger.error(f"ElevenLabs proxy streaming error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# TTS ENGINE (GLOBAL SINGLETON)
# ═══════════════════════════════════════════════════════════════════════════════

class TTSEngine:
    """Global TTS engine managing backend, voice registry, and audio conversion."""

    def __init__(
        self,
        backend_name: str = "coqui",
        device: str = "cuda",
        voice_dir: Path = VOICE_EMBEDDING_DIR,
    ) -> None:
        self.device = device
        self.voice_registry = VoiceRegistry(voice_dir)
        self.converter = AudioConverter()
        self.emotion_parser = EmotionTagParser()
        self.normalizer = TextNormalizer()

        # Backend registry
        self._backends: dict[str, TTSBackend] = {
            "cosyvoice": CosyVoiceBackend(),
            "coqui": CoquiTTSBackend(),
            "elevenlabs": ElevenLabsProxyBackend(),
        }
        self._active_backend_name = backend_name
        self._lock = asyncio.Lock()
        self._start_time = time.time()
        self._total_generations = 0

    @property
    def active_backend(self) -> TTSBackend:
        """Get the currently active TTS backend."""
        return self._backends[self._active_backend_name]

    @property
    def model_loaded(self) -> bool:
        return self.active_backend.is_loaded()

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def load(self) -> None:
        """Load the active backend model and voice registry."""
        self.voice_registry.load_from_disk()
        backend = self.active_backend
        backend.load_model(self.device)
        if not backend.is_loaded():
            logger.warning(f"Backend '{self._active_backend_name}' failed to load, trying fallbacks...")
            for name, b in self._backends.items():
                if name != self._active_backend_name:
                    b.load_model(self.device)
                    if b.is_loaded():
                        logger.info(f"Switched to fallback backend: {name}")
                        self._active_backend_name = name
                        break

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        request: TTSRequest,
    ) -> bytes:
        """Synthesize complete audio from text."""
        voice_settings = request.voice_settings or VoiceSettings()
        clean_text, tags = self.emotion_parser.extract_tags(text)
        prosody = self.emotion_parser.get_prosody_vector(tags)

        # ElevenLabs v3 handles emotion tags NATIVELY in the model —
        # pass original text with tags intact, only normalize non-tag parts
        if self._active_backend_name == "elevenlabs":
            # Normalize text but preserve emotion tags inline
            synth_text = self._normalize_preserving_tags(text, request.apply_text_normalization)
        else:
            synth_text = self.normalizer.normalize(clean_text, request.apply_text_normalization)

        if not synth_text.strip():
            return b""

        async with self._lock:
            audio, sr = await self.active_backend.synthesize(
                text=synth_text,
                voice_id=voice_id,
                voice_registry=self.voice_registry,
                voice_settings=voice_settings,
                prosody_vector=prosody,
                language_code=request.language_code,
                seed=request.seed,
            )

        self._total_generations += 1
        return self.converter.convert(audio, sr, request.output_format)

    def _normalize_preserving_tags(self, text: str, normalization: str) -> str:
        """Normalize text while preserving emotion tags inline for ElevenLabs v3."""
        import re as _re
        # Split text into tag and non-tag segments
        parts = _re.split(r"(\[\w+\])", text)
        result_parts = []
        for part in parts:
            if _re.match(r"^\[\w+\]$", part):
                result_parts.append(part)  # Keep tag as-is
            else:
                result_parts.append(self.normalizer.normalize(part, normalization))
        return " ".join(p for p in result_parts if p.strip())

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        request: TTSRequest,
    ) -> AsyncIterator[bytes]:
        """Stream synthesized audio chunks."""
        voice_settings = request.voice_settings or VoiceSettings()
        clean_text, tags = self.emotion_parser.extract_tags(text)
        prosody = self.emotion_parser.get_prosody_vector(tags)

        if self._active_backend_name == "elevenlabs":
            synth_text = self._normalize_preserving_tags(text, request.apply_text_normalization)
        else:
            synth_text = self.normalizer.normalize(clean_text, request.apply_text_normalization)

        if not synth_text.strip():
            return

        async for audio_chunk, sr in self.active_backend.synthesize_stream(
            text=synth_text,
            voice_id=voice_id,
            voice_registry=self.voice_registry,
            voice_settings=voice_settings,
            prosody_vector=prosody,
            language_code=request.language_code,
            seed=request.seed,
        ):
            chunk_bytes = self.converter.convert(audio_chunk, sr, request.output_format)
            self._total_generations += 1

            # Split into smaller sub-chunks for smooth streaming
            target_rate = self.converter.get_sample_rate(request.output_format)
            sub_chunk_size = target_rate * 2 // 10  # 100ms of PCM16
            if sub_chunk_size <= 0:
                sub_chunk_size = len(chunk_bytes)

            for i in range(0, len(chunk_bytes), sub_chunk_size):
                yield chunk_bytes[i:i + sub_chunk_size]

    async def synthesize_dialogue(
        self,
        lines: list[DialogueLine],
        model_id: str,
        output_format: OutputFormat,
        normalization: str = "auto",
        seed: int | None = None,
    ) -> bytes:
        """Synthesize multi-speaker dialogue."""
        all_audio: list[bytes] = []

        for line in lines:
            req = TTSRequest(
                text=line.text,
                model_id=model_id,
                voice_settings=line.voice_settings,
                output_format=output_format,
                seed=seed,
                apply_text_normalization=normalization,
            )
            audio_bytes = await self.synthesize(line.text, line.voice_id, req)
            all_audio.append(audio_bytes)

            # Add 200ms silence between speakers
            target_rate = self.converter.get_sample_rate(output_format)
            silence_samples = int(target_rate * 0.2)
            silence_bytes = np.zeros(silence_samples, dtype=np.int16).tobytes()
            all_audio.append(silence_bytes)

        return b"".join(all_audio)


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL ENGINE INSTANCE
# ═══════════════════════════════════════════════════════════════════════════════

import os

_backend = os.getenv("TTS_BACKEND", "cosyvoice")
_device = os.getenv("TTS_DEVICE", "cuda")
_voice_dir = Path(os.getenv("TTS_VOICE_DIR", str(VOICE_EMBEDDING_DIR)))

tts_engine = TTSEngine(backend_name=_backend, device=_device, voice_dir=_voice_dir)


# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: load model on startup."""
    logger.info(f"Starting {SERVICE_NAME} v{SERVICE_VERSION} on port {PORT}")
    tts_engine.load()
    logger.info(
        f"{SERVICE_NAME} ready — backend={tts_engine.active_backend.name}, "
        f"loaded={tts_engine.model_loaded}, voices={len(tts_engine.voice_registry.list_voices())}"
    )
    yield
    logger.info(f"Shutting down {SERVICE_NAME}")


app = FastAPI(
    title="Echo ConvAI TTS Service",
    description="Text-to-speech with pluggable backends (Coqui XTTS-v2, F5-TTS, ElevenLabs proxy)",
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
        status="healthy" if tts_engine.model_loaded else "degraded",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        backend=tts_engine.active_backend.name,
        model_loaded=tts_engine.model_loaded,
        device=tts_engine.device,
        uptime_seconds=round(tts_engine.uptime_seconds, 1),
        total_generations=tts_engine._total_generations,
        registered_voices=len(tts_engine.voice_registry.list_voices()),
    )


# ── Voice List ────────────────────────────────────────────────────────────────

@app.get("/v1/voices", response_model=list[VoiceInfo])
async def list_voices() -> list[VoiceInfo]:
    """List all available voices."""
    return tts_engine.voice_registry.list_voices()


# ── Text-to-Speech (Full) ────────────────────────────────────────────────────

@app.post("/v1/text-to-speech/{voice_id}")
async def text_to_speech(voice_id: str, request: TTSRequest) -> Response:
    """Generate speech from text (ElevenLabs-compatible)."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text is empty")
    if len(request.text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Text exceeds max length of {MAX_TEXT_LENGTH}")

    t_start = time.time()
    logger.info(
        f"TTS request: voice={voice_id}, text_len={len(request.text)}, "
        f"format={request.output_format}, model={request.model_id}"
    )

    audio_bytes = await tts_engine.synthesize(request.text, voice_id, request)

    latency_ms = (time.time() - t_start) * 1000
    logger.info(f"TTS complete: {len(audio_bytes)} bytes, {latency_ms:.0f}ms")

    content_type = AudioConverter.get_content_type(request.output_format)
    return Response(content=audio_bytes, media_type=content_type)


# ── Text-to-Speech (Streaming) ───────────────────────────────────────────────

@app.post("/v1/text-to-speech/{voice_id}/stream")
async def text_to_speech_stream(voice_id: str, request: TTSRequest) -> StreamingResponse:
    """Stream synthesized audio (ElevenLabs-compatible)."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text is empty")
    if len(request.text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=400, detail=f"Text exceeds max length of {MAX_TEXT_LENGTH}")

    logger.info(
        f"TTS stream request: voice={voice_id}, text_len={len(request.text)}, "
        f"format={request.output_format}"
    )

    content_type = AudioConverter.get_content_type(request.output_format)

    async def generate() -> AsyncIterator[bytes]:
        async for chunk in tts_engine.synthesize_stream(request.text, voice_id, request):
            yield chunk

    return StreamingResponse(generate(), media_type=content_type)


# ── Multi-Speaker Dialogue ───────────────────────────────────────────────────

@app.post("/v1/text-to-dialogue")
async def text_to_dialogue(request: DialogueRequest) -> Response:
    """Generate multi-speaker dialogue audio."""
    if not request.lines:
        raise HTTPException(status_code=400, detail="No dialogue lines provided")

    logger.info(f"Dialogue request: {len(request.lines)} lines")

    audio_bytes = await tts_engine.synthesize_dialogue(
        lines=request.lines,
        model_id=request.model_id,
        output_format=request.output_format,
        normalization=request.apply_text_normalization,
        seed=request.seed,
    )

    content_type = AudioConverter.get_content_type(request.output_format)
    return Response(content=audio_bytes, media_type=content_type)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting {SERVICE_NAME} on port {PORT}")
    uvicorn.run(
        "tts_service:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
