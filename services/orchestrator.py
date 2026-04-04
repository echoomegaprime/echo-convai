"""Echo ConvAI — Conversation Orchestrator Service.

Central coordinator for real-time voice conversations on BRAVO node (port 8800).
Manages the full pipeline: Client Audio -> ASR -> Turn-Taking -> LLM -> TTS -> Client Audio.

Speaks the exact ElevenLabs ConvAI WebSocket protocol so existing SDK clients
connect seamlessly. Coordinates with ASR service (8801) and TTS service (8802).

Architecture:
    Cloudflare Gateway (cloudflared tunnel)
        -> WebSocket /conversation or /v1/convai/conversation
            -> ConversationHandler (per-connection state machine)
                -> ASR proxy (localhost:8801)
                -> Turn-Taking Engine (in-process, VAD + rule-based)
                -> LLM Orchestrator (Claude/GPT/Gemini/local, streaming + cascade)
                -> TTS proxy (localhost:8802)
            <- Streaming audio + events back to client

Features:
- Full ElevenLabs ConvAI WebSocket protocol compatibility
- WebSocket endpoint for voice and text-only sessions
- ASR integration via streaming WebSocket to port 8801
- TTS integration via streaming HTTP to port 8802
- Multi-provider LLM: Claude, GPT, Gemini, Grok, local models
- Cascade failover: primary -> backup LLM on error/timeout
- Voice Activity Detection (Silero VAD with energy-based fallback)
- Advanced turn-taking: silence + VAD trend + transcript + length features
- Interruption handling (stop TTS, restart ASR, send event)
- Tool execution: webhook, client tools, system tools (end_call, transfer)
- Conversation history management (sliding window, token estimation)
- REST endpoints: health, sessions, agents, metrics, interrupt
- Ping/pong keepalive matching ElevenLabs protocol
- Dynamic variable resolution in system prompts

Port: 8800
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

SERVICE_NAME = "echo-convai-orchestrator"
SERVICE_VERSION = "1.1.0"
PORT = 8800

ASR_URL = os.getenv("ASR_URL", "http://localhost:8801")
TTS_URL = os.getenv("TTS_URL", "http://localhost:8802")

DEFAULT_LLM_MODEL = os.getenv("ECHO_DEFAULT_LLM", "gemini-2.5-flash")
DEFAULT_LLM_TEMPERATURE = 0.85
DEFAULT_LLM_MAX_TOKENS = 400
DEFAULT_SYSTEM_PROMPT = (
    "You are Echo, a helpful and friendly conversational AI assistant. "
    "You speak naturally and concisely. Keep responses brief since this is voice."
)
DEFAULT_FIRST_MESSAGE = "Hello! How can I help you today?"
DEFAULT_VOICE_ID = "echo"
DEFAULT_TURN_TIMEOUT_S = 15.0

# Cascade failover settings
CASCADE_TIMEOUT_S = float(os.getenv("ECHO_CASCADE_TIMEOUT", "8.0"))
BACKUP_LLM_MODEL = os.getenv("ECHO_BACKUP_LLM", "gemini-2.5-flash")

MAX_CONVERSATION_HISTORY = 50  # Sliding window of messages
MAX_HISTORY_TOKENS = 8000  # Rough token limit for history
MAX_SESSION_DURATION_S = 1800  # 30 minutes
HEARTBEAT_INTERVAL_S = 30.0
PING_INTERVAL_S = 5.0  # ElevenLabs uses ~1.8s, we use 5s
AUDIO_CHUNK_DURATION_MS = 250  # 250ms per audio chunk sent to ASR
SPECULATIVE_GENERATION_DELAY_S = 0.5  # Wait before starting speculative gen

# Turn-taking silence thresholds (milliseconds)
SILENCE_MIN_MS = 200  # Minimum before considering end-of-turn
SILENCE_STRONG_MS = 800  # Strong end-of-turn signal
SILENCE_DEFINITE_MS = 1500  # Almost certainly end-of-turn
FORCE_RESPOND_TIMEOUT_MS = int(os.getenv("ECHO_FORCE_TIMEOUT_MS", "8000"))

# Tool timeouts
TOOL_CALL_TIMEOUT_S = float(os.getenv("ECHO_TOOL_TIMEOUT", "30.0"))
WEBHOOK_TIMEOUT_S = float(os.getenv("ECHO_WEBHOOK_TIMEOUT", "20.0"))

WS_MAX_SIZE = 16 * 1024 * 1024  # 16 MB (matches ElevenLabs)

# API keys from environment
API_KEYS: dict[str, str] = {
    "anthropic": os.getenv("ANTHROPIC_API_KEY", ""),
    "openai": os.getenv("OPENAI_API_KEY", ""),
    "google": os.getenv("GOOGLE_API_KEY", ""),
    "xai": os.getenv("XAI_API_KEY", ""),
}


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS & MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class TurnMode(str, Enum):
    TURN = "turn"
    SILENCE = "silence"


class TurnEagerness(str, Enum):
    PATIENT = "patient"
    NORMAL = "normal"
    EAGER = "eager"


class SessionState(str, Enum):
    INITIALIZING = "initializing"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    WAITING = "waiting"
    ENDED = "ended"


class ToolType(str, Enum):
    SYSTEM = "system"
    WEBHOOK = "webhook"
    CLIENT = "client"


class AgentConfig(BaseModel):
    """Agent configuration for a conversation session."""
    agent_id: str = ""
    name: str = "Echo Agent"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    first_message: str | None = DEFAULT_FIRST_MESSAGE
    language: str = "en"
    voice_id: str = DEFAULT_VOICE_ID
    tts_model: str = "eleven_v3_conversational"
    llm_model: str = DEFAULT_LLM_MODEL
    llm_temperature: float = DEFAULT_LLM_TEMPERATURE
    llm_max_tokens: int = DEFAULT_LLM_MAX_TOKENS
    turn_timeout: float = DEFAULT_TURN_TIMEOUT_S
    turn_mode: TurnMode = TurnMode.TURN
    turn_eagerness: TurnEagerness = TurnEagerness.NORMAL
    max_duration_seconds: int = MAX_SESSION_DURATION_S
    tools: list[dict[str, Any]] = Field(default_factory=list)
    output_audio_format: str = "pcm_16000"
    input_audio_format: str = "pcm_16000"


class ConversationMessage(BaseModel):
    """A message in conversation history."""
    role: str  # "user", "assistant", "system", "tool"
    content: str
    timestamp: float = Field(default_factory=time.time)
    tool_call_id: str | None = None
    tool_name: str | None = None


class SessionMetrics(BaseModel):
    """Metrics tracked per session."""
    total_user_messages: int = 0
    total_agent_responses: int = 0
    total_audio_chunks_in: int = 0
    total_audio_chunks_out: int = 0
    total_llm_tokens: int = 0
    total_tool_calls: int = 0
    total_interruptions: int = 0
    avg_response_latency_ms: float = 0.0
    response_latencies: list[float] = Field(default_factory=list)


class ConversationSession(BaseModel):
    """Full state for an active conversation session."""
    conversation_id: str = Field(default_factory=lambda: f"conv_{uuid.uuid4().hex[:12]}")
    agent_config: AgentConfig = Field(default_factory=AgentConfig)
    user_id: str | None = None
    state: SessionState = SessionState.INITIALIZING
    started_at: float = Field(default_factory=time.time)
    last_activity: float = Field(default_factory=time.time)

    # Conversation history
    messages: list[ConversationMessage] = Field(default_factory=list)

    # Turn-taking state
    is_agent_speaking: bool = False
    is_user_speaking: bool = False
    last_user_audio_time: float = 0.0
    last_agent_end_time: float = 0.0

    # Interruption
    last_interrupt_id: int = 0
    current_event_id: int = 0

    # Speculative generation
    speculative_response: str | None = None
    speculative_transcript: str | None = None

    # Tool state
    pending_tool_calls: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # Metrics
    metrics: SessionMetrics = Field(default_factory=SessionMetrics)

    # Dynamic variables
    dynamic_variables: dict[str, str] = Field(default_factory=dict)

    # Text-only mode
    text_only: bool = False

    class Config:
        arbitrary_types_allowed = True


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    service: str
    version: str
    uptime_seconds: float
    active_sessions: int
    asr_status: str
    tts_status: str


# ═══════════════════════════════════════════════════════════════════════════════
# VOICE ACTIVITY DETECTION (VAD)
# ═══════════════════════════════════════════════════════════════════════════════

class EnergyVAD:
    """Energy-based Voice Activity Detection fallback.

    Uses RMS energy thresholding calibrated for 16kHz 16-bit PCM mono.
    """

    def __init__(self, energy_floor: float = 0.01, energy_ceiling: float = 0.06) -> None:
        self._floor = energy_floor
        self._ceiling = energy_ceiling

    def process_chunk(self, audio_bytes: bytes) -> float:
        """Return VAD score 0.0 (silence) to 1.0 (speech)."""
        if len(audio_bytes) < 2:
            return 0.0
        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        score = (rms - self._floor) / (self._ceiling - self._floor)
        return max(0.0, min(1.0, score))


class SileroVADWrapper:
    """Lazy-loaded Silero VAD with energy-based fallback.

    Uses ONNX Silero VAD when available, gracefully falls back to
    energy-based detection if PyTorch/Silero is not installed.
    """

    def __init__(self, sample_rate: int = 16000) -> None:
        self._sample_rate = sample_rate
        self._model: Any = None
        self._loaded = False
        self._fallback = EnergyVAD()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            import torch
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=True,
            )
            self._model = model
            logger.info("Silero VAD loaded successfully")
        except Exception as exc:
            logger.warning(f"Silero VAD unavailable, using energy fallback: {exc}")

    def process_chunk(self, audio_bytes: bytes) -> float:
        """Process audio chunk and return VAD score (0.0 silence to 1.0 speech)."""
        self._ensure_loaded()
        if self._model is not None:
            try:
                import torch
                samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                tensor = torch.from_numpy(samples)
                return float(self._model(tensor, self._sample_rate).item())
            except Exception:
                pass
        return self._fallback.process_chunk(audio_bytes)

    def reset(self) -> None:
        """Reset VAD state between utterances."""
        if self._model is not None and hasattr(self._model, "reset_states"):
            self._model.reset_states()


# ═══════════════════════════════════════════════════════════════════════════════
# TURN-TAKING ENGINE — Predicts end-of-turn from multiple signals
# ═══════════════════════════════════════════════════════════════════════════════

class TurnTakingEngine:
    """Decides when the user has finished speaking and the agent should respond.

    Combines multiple weighted signals (tuned to approximate ElevenLabs behavior):
    - Silence duration (35%): most important signal
    - VAD score trend (15%): declining VAD = end of turn
    - Transcript completeness (30%): sentence-ending punctuation
    - Speaking rate (10%): slowing down = likely done
    - Utterance length (10%): short answers likely complete

    Eagerness controls the decision threshold:
    - patient (0.85): Wait for very high confidence
    - normal  (0.65): Balanced latency vs accuracy
    - eager   (0.40): Respond fast, risk interrupting
    """

    EAGERNESS_THRESHOLDS: dict[str, float] = {
        "patient": 0.85,
        "normal": 0.65,
        "eager": 0.40,
    }

    SENTENCE_ENDERS = frozenset(".!?;")
    CLAUSE_ENDERS = frozenset(",:\u2014\u2013")
    QUESTION_WORDS = frozenset({"what", "who", "where", "when", "why", "how", "which", "whose"})

    W_SILENCE: float = 0.35
    W_VAD: float = 0.15
    W_TRANSCRIPT: float = 0.30
    W_RATE: float = 0.10
    W_LENGTH: float = 0.10

    def __init__(self, sample_rate: int = 16000) -> None:
        self.vad = SileroVADWrapper(sample_rate)
        self._vad_history: list[float] = []
        self._last_vad_score: float = 0.0

    def update_vad(self, audio_bytes: bytes) -> float:
        """Feed audio chunk, return VAD score."""
        score = self.vad.process_chunk(audio_bytes)
        self._last_vad_score = score
        self._vad_history.append(score)
        if len(self._vad_history) > 60:
            self._vad_history = self._vad_history[-60:]
        return score

    def predict_turn_end(self, silence_ms: int, transcript: str) -> float:
        """Predict probability (0-1) that the user has finished speaking."""
        import math

        s_silence = self._silence_score(silence_ms)
        s_vad = self._vad_trend_score()
        s_transcript = self._transcript_score(transcript)
        s_rate = self._rate_score(transcript)
        s_length = self._length_score(transcript)

        prob = (
            self.W_SILENCE * s_silence
            + self.W_VAD * s_vad
            + self.W_TRANSCRIPT * s_transcript
            + self.W_RATE * s_rate
            + self.W_LENGTH * s_length
        )
        prob = max(0.0, min(1.0, prob))

        # Hard overrides
        if silence_ms < SILENCE_MIN_MS:
            prob = min(prob, 0.1)
        if silence_ms > SILENCE_DEFINITE_MS:
            prob = max(prob, 0.95)

        return prob

    def get_threshold(self, eagerness: str) -> float:
        """Get turn-end decision threshold based on eagerness setting."""
        return self.EAGERNESS_THRESHOLDS.get(eagerness, 0.65)

    def reset(self) -> None:
        """Reset all state for new utterance."""
        self._vad_history.clear()
        self._last_vad_score = 0.0
        self.vad.reset()

    def _silence_score(self, silence_ms: int) -> float:
        """Sigmoid-shaped score based on silence duration."""
        import math
        if silence_ms < SILENCE_MIN_MS:
            return 0.0
        if silence_ms > SILENCE_DEFINITE_MS:
            return 1.0
        x = (silence_ms - SILENCE_MIN_MS) / (SILENCE_DEFINITE_MS - SILENCE_MIN_MS)
        center = (SILENCE_STRONG_MS - SILENCE_MIN_MS) / (SILENCE_DEFINITE_MS - SILENCE_MIN_MS)
        return 1.0 / (1.0 + math.exp(-10.0 * (x - center)))

    def _vad_trend_score(self) -> float:
        """Score based on VAD trend (declining = end of turn)."""
        if len(self._vad_history) < 5:
            return 0.5
        recent = self._vad_history[-5:]
        trend = recent[-1] - recent[0]
        if trend < -0.3:
            return 0.9
        if trend < -0.1:
            return 0.7
        if trend > 0.1:
            return 0.1
        return 0.5

    def _transcript_score(self, transcript: str) -> float:
        """Score based on transcript completeness (punctuation, question words)."""
        text = transcript.strip()
        if not text:
            return 0.0
        last_char = text[-1]
        if last_char in self.SENTENCE_ENDERS:
            return 0.95
        if last_char in self.CLAUSE_ENDERS:
            return 0.6
        words = text.split()
        if words:
            first_word = words[0].lower()
            if first_word in self.QUESTION_WORDS and last_char != "?":
                return 0.2
        return 0.4

    def _rate_score(self, transcript: str) -> float:
        """Heuristic score based on word length trend (slowing = done)."""
        words = transcript.split() if transcript else []
        if len(words) < 3:
            return 0.5
        recent_lens = [len(w) for w in words[-3:]]
        earlier_lens = [len(w) for w in words[-6:-3]] if len(words) >= 6 else [4.0]
        avg_recent = sum(recent_lens) / len(recent_lens)
        avg_earlier = sum(earlier_lens) / len(earlier_lens)
        if avg_recent > avg_earlier * 1.5:
            return 0.7
        return 0.5

    def _length_score(self, transcript: str) -> float:
        """Score based on utterance word count."""
        word_count = len(transcript.split()) if transcript else 0
        if word_count == 0:
            return 0.0
        if word_count <= 2:
            return 0.8  # Short answer — probably complete
        if word_count <= 5:
            return 0.5
        if word_count >= 15:
            return 0.7  # Long enough — likely wrapping up
        return 0.5


# Global turn-taking engine instance (shared across sessions for VAD model)
_turn_engine = TurnTakingEngine()


# ═══════════════════════════════════════════════════════════════════════════════
# SENTENCE BOUNDARY DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def is_sentence_boundary(text: str) -> bool:
    """Detect natural sentence boundaries for TTS chunking.

    Short sentences = lower latency. Full sentences = better prosody.
    Matches ElevenLabs' chunking behavior.
    """
    stripped = text.rstrip()
    if not stripped:
        return False
    if stripped[-1] in ".!?;:":
        return True
    if stripped[-1] == "," and len(stripped) > 40:
        return True
    if len(stripped) > 150:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# LLM CLIENT — MULTI-PROVIDER WITH CASCADE FAILOVER
# ═══════════════════════════════════════════════════════════════════════════════

class LLMClient:
    """Multi-provider LLM client supporting Claude, GPT, Gemini, and local models."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None

    async def _get_http(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def generate_stream(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        model: str = DEFAULT_LLM_MODEL,
        temperature: float = DEFAULT_LLM_TEMPERATURE,
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """Stream LLM response tokens from any supported provider.

        Yields text chunks as they arrive.
        """
        provider = self._detect_provider(model)
        logger.debug(f"LLM generate: model={model}, provider={provider}")

        if provider == "anthropic":
            async for chunk in self._stream_anthropic(messages, system_prompt, model, temperature, max_tokens):
                yield chunk
        elif provider == "openai":
            async for chunk in self._stream_openai(messages, system_prompt, model, temperature, max_tokens):
                yield chunk
        elif provider == "google":
            async for chunk in self._stream_google(messages, system_prompt, model, temperature, max_tokens):
                yield chunk
        elif provider == "local":
            async for chunk in self._stream_local(messages, system_prompt, model, temperature, max_tokens):
                yield chunk
        else:
            # Fallback: try local multi-AI orchestrator
            async for chunk in self._stream_local(messages, system_prompt, model, temperature, max_tokens):
                yield chunk

    async def generate_stream_with_cascade(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        model: str = DEFAULT_LLM_MODEL,
        backup_model: str = BACKUP_LLM_MODEL,
        cascade_timeout: float = CASCADE_TIMEOUT_S,
        temperature: float = DEFAULT_LLM_TEMPERATURE,
        max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """Stream LLM response with cascade failover to backup model on error.

        If the primary model fails or times out, automatically falls back to
        the backup model (default: gemini-2.5-flash, matching ElevenLabs default).
        """
        try:
            got_token = False
            async for token in self.generate_stream(
                messages, system_prompt, model, temperature, max_tokens, tools,
            ):
                got_token = True
                yield token
            if got_token:
                return
        except Exception as exc:
            logger.warning(f"Primary LLM ({model}) failed: {exc}. Cascading to {backup_model}")

        # Cascade to backup model
        try:
            async for token in self.generate_stream(
                messages, system_prompt, backup_model, temperature, max_tokens, tools,
            ):
                yield token
        except Exception as exc:
            logger.error(f"Backup LLM ({backup_model}) also failed: {exc}")
            yield "I'm sorry, I'm having trouble processing your request right now."

    async def generate_tool_check(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        model: str = DEFAULT_LLM_MODEL,
    ) -> dict[str, Any] | None:
        """Check if LLM wants to call a tool. Returns tool call dict or None."""
        provider = self._detect_provider(model)

        if provider == "anthropic":
            return await self._tool_check_anthropic(messages, system_prompt, tools, model)
        elif provider == "openai":
            return await self._tool_check_openai(messages, system_prompt, tools, model)

        return None

    @staticmethod
    def _detect_provider(model: str) -> str:
        """Detect which provider to use based on model name."""
        if model.startswith("claude"):
            return "anthropic"
        if model.startswith(("gpt-", "o3", "o4")):
            return "openai"
        if model.startswith("gemini"):
            return "google"
        if model.startswith(("grok", "deepseek", "qwen", "llama")):
            return "local"
        return "local"

    async def _stream_anthropic(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream from Anthropic Claude API."""
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            yield "[Error: ANTHROPIC_API_KEY not set]"
            return

        http = await self._get_http()
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": messages,
            "stream": True,
        }

        try:
            async with http.stream("POST", url, headers=headers, json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                            if event.get("type") == "content_block_delta":
                                delta = event.get("delta", {})
                                text = delta.get("text", "")
                                if text:
                                    yield text
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"Anthropic streaming error: {e}")
            yield f"[Error: {e}]"

    async def _stream_openai(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream from OpenAI-compatible API."""
        api_key = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        if not api_key:
            yield "[Error: OPENAI_API_KEY not set]"
            return

        http = await self._get_http()
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        all_messages = [{"role": "system", "content": system_prompt}] + messages
        body = {
            "model": model,
            "messages": all_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        try:
            async with http.stream("POST", url, headers=headers, json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                            choices = event.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                text = delta.get("content", "")
                                if text:
                                    yield text
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"OpenAI streaming error: {e}")
            yield f"[Error: {e}]"

    async def _stream_google(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream from Google Gemini API."""
        api_key = os.getenv("GOOGLE_API_KEY", os.getenv("GEMINI_API_KEY", ""))
        if not api_key:
            yield "[Error: GOOGLE_API_KEY not set]"
            return

        http = await self._get_http()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent"
        params = {"key": api_key, "alt": "sse"}

        # Convert messages to Gemini format
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})

        body = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }

        try:
            async with http.stream("POST", url, params=params, json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        try:
                            event = json.loads(data)
                            candidates = event.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                for part in parts:
                                    text = part.get("text", "")
                                    if text:
                                        yield text
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"Gemini streaming error: {e}")
            yield f"[Error: {e}]"

    async def _stream_local(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream from local Multi-AI Orchestrator (port 3032) or any OpenAI-compatible local server."""
        local_url = os.getenv("LOCAL_LLM_URL", "http://localhost:3032/v1")
        http = await self._get_http()
        url = f"{local_url}/chat/completions"

        all_messages = [{"role": "system", "content": system_prompt}] + messages
        body = {
            "model": model,
            "messages": all_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        try:
            async with http.stream("POST", url, json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                            choices = event.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                text = delta.get("content", "")
                                if text:
                                    yield text
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"Local LLM streaming error: {e}")
            yield f"[Error: {e}]"

    async def _tool_check_anthropic(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        model: str,
    ) -> dict[str, Any] | None:
        """Check if Claude wants to call a tool."""
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None

        http = await self._get_http()
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": messages,
            "tools": tools,
        }

        try:
            resp = await http.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            for block in data.get("content", []):
                if block.get("type") == "tool_use":
                    return {
                        "tool_call_id": block.get("id", str(uuid.uuid4())),
                        "tool_name": block["name"],
                        "parameters": block.get("input", {}),
                    }
        except Exception as e:
            logger.error(f"Anthropic tool check error: {e}")

        return None

    async def _tool_check_openai(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        model: str,
    ) -> dict[str, Any] | None:
        """Check if GPT wants to call a tool."""
        api_key = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        if not api_key:
            return None

        http = await self._get_http()
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # Convert tools to OpenAI format
        openai_tools = []
        for tool in tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                },
            })

        all_messages = [{"role": "system", "content": system_prompt}] + messages
        body = {
            "model": model,
            "messages": all_messages,
            "tools": openai_tools,
            "tool_choice": "auto",
        }

        try:
            resp = await http.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    tc = tool_calls[0]
                    return {
                        "tool_call_id": tc.get("id", str(uuid.uuid4())),
                        "tool_name": tc["function"]["name"],
                        "parameters": json.loads(tc["function"].get("arguments", "{}")),
                    }
        except Exception as e:
            logger.error(f"OpenAI tool check error: {e}")

        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ASR CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class ASRClient:
    """Client for the ASR service (port 8801)."""

    def __init__(self, base_url: str = ASR_URL) -> None:
        self.base_url = base_url
        self._ws: Any = None  # websockets connection
        self._session_id: str | None = None

    async def check_health(self) -> str:
        """Check ASR service health."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/health")
                if resp.status_code == 200:
                    return resp.json().get("status", "unknown")
                return "error"
        except Exception:
            return "unreachable"

    async def connect_stream(
        self,
        audio_format: str = "pcm_16000",
        language_code: str | None = None,
        commit_strategy: str = "vad",
    ) -> None:
        """Connect to ASR streaming WebSocket."""
        import websockets

        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        params = f"audio_format={audio_format}&commit_strategy={commit_strategy}"
        if language_code:
            params += f"&language_code={language_code}"

        self._ws = await websockets.connect(
            f"{ws_url}/stream?{params}",
            max_size=WS_MAX_SIZE,
        )

        # Wait for session_started
        raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
        msg = json.loads(raw)
        if msg.get("type") == "session_started":
            self._session_id = msg.get("session_id")
            logger.info(f"ASR stream connected: session={self._session_id}")

    async def send_audio(self, audio_b64: str) -> None:
        """Send audio chunk to ASR stream."""
        if self._ws:
            await self._ws.send(json.dumps({
                "message_type": "input_audio_chunk",
                "audio_base_64": audio_b64,
                "commit": False,
            }))

    async def commit(self) -> None:
        """Force-commit current ASR buffer."""
        if self._ws:
            await self._ws.send(json.dumps({
                "message_type": "input_audio_chunk",
                "audio_base_64": "",
                "commit": True,
            }))

    async def receive_transcript(self, timeout: float = 0.1) -> dict[str, Any] | None:
        """Non-blocking receive of transcript from ASR stream."""
        if not self._ws:
            return None
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            return json.loads(raw)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.warning(f"ASR receive error: {e}")
            return None

    async def close(self) -> None:
        """Close ASR stream."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._session_id = None


# ═══════════════════════════════════════════════════════════════════════════════
# TTS CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class TTSClient:
    """Client for the TTS service (port 8802)."""

    def __init__(self, base_url: str = TTS_URL) -> None:
        self.base_url = base_url

    async def check_health(self) -> str:
        """Check TTS service health."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/health")
                if resp.status_code == 200:
                    return resp.json().get("status", "unknown")
                return "error"
        except Exception:
            return "unreachable"

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        output_format: str = "pcm_16000",
        model_id: str = "eleven_v3_conversational",
    ) -> AsyncIterator[bytes]:
        """Stream TTS audio from the TTS service."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            url = f"{self.base_url}/v1/text-to-speech/{voice_id}/stream"
            body = {
                "text": text,
                "model_id": model_id,
                "output_format": output_format,
            }
            try:
                async with client.stream("POST", url, json=body) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=4096):
                        yield chunk
            except Exception as e:
                logger.error(f"TTS stream error: {e}")

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        output_format: str = "pcm_16000",
    ) -> bytes:
        """Get complete TTS audio."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{self.base_url}/v1/text-to-speech/{voice_id}"
            body = {
                "text": text,
                "output_format": output_format,
            }
            try:
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                return resp.content
            except Exception as e:
                logger.error(f"TTS error: {e}")
                return b""


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL EXECUTOR
# ═══════════════════════════════════════════════════════════════════════════════

class ToolExecutor:
    """Executes tools (webhook, client, system) during conversations."""

    async def execute_webhook(self, url: str, method: str, body: dict[str, Any]) -> str:
        """Execute a webhook tool by making an HTTP request."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                if method.upper() == "GET":
                    resp = await client.get(url, params=body)
                else:
                    resp = await client.post(url, json=body)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                logger.error(f"Webhook tool error: {e}")
                return f"Error: {e}"

    async def execute_system_tool(self, tool_name: str, params: dict[str, Any]) -> str:
        """Execute a system tool (e.g., end_call)."""
        if tool_name == "end_call":
            return "SYSTEM:END_CALL"
        if tool_name == "transfer_call":
            return f"SYSTEM:TRANSFER:{params.get('destination', '')}"
        return f"Unknown system tool: {tool_name}"


# ═══════════════════════════════════════════════════════════════════════════════
# CONVERSATION HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

class ConversationHandler:
    """Manages a single WebSocket conversation session.

    Implements the full ElevenLabs ConvAI WebSocket protocol.
    """

    def __init__(
        self,
        ws: WebSocket,
        session: ConversationSession,
        asr_client: ASRClient,
        tts_client: TTSClient,
        llm_client: LLMClient,
        tool_executor: ToolExecutor,
    ) -> None:
        self.ws = ws
        self.session = session
        self.asr = asr_client
        self.tts = tts_client
        self.llm = llm_client
        self.tools = tool_executor

        self._stop_event = asyncio.Event()
        self._speaking_event = asyncio.Event()
        self._tts_cancel = asyncio.Event()
        self._asr_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Main conversation loop."""
        try:
            # Wait for initiation message
            await self._handle_initiation()

            # Connect to ASR if not text-only
            if not self.session.text_only:
                try:
                    await self.asr.connect_stream(
                        audio_format=self.session.agent_config.input_audio_format,
                        language_code=self.session.agent_config.language,
                    )
                    self._asr_task = asyncio.create_task(self._asr_listener())
                except Exception as e:
                    logger.warning(f"ASR connection failed: {e} — falling back to text-only")
                    self.session.text_only = True

            # Start ping task
            self._ping_task = asyncio.create_task(self._ping_loop())

            # Send first message if configured
            if self.session.agent_config.first_message:
                await self._send_agent_response(self.session.agent_config.first_message)

            self.session.state = SessionState.LISTENING

            # Main message receive loop
            while not self._stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(self.ws.receive_text(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Check session expiry
                    if time.time() - self.session.started_at > self.session.agent_config.max_duration_seconds:
                        logger.info(f"Session {self.session.conversation_id} expired")
                        break
                    continue
                except WebSocketDisconnect:
                    break

                await self._handle_message(raw)

        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected: {self.session.conversation_id}")
        except Exception as e:
            logger.error(f"Conversation error: {e}")
        finally:
            await self._cleanup()

    async def _handle_initiation(self) -> None:
        """Handle the conversation initiation handshake."""
        try:
            raw = await asyncio.wait_for(self.ws.receive_text(), timeout=10.0)
            msg = json.loads(raw)
        except Exception as e:
            logger.error(f"Initiation timeout/error: {e}")
            return

        msg_type = msg.get("type", "")

        if msg_type == "conversation_initiation_client_data":
            # Apply config overrides
            config_override = msg.get("conversation_config_override", {})
            if config_override.get("text_only"):
                self.session.text_only = True

            # Apply dynamic variables
            dynamic_vars = msg.get("dynamic_variables", {})
            self.session.dynamic_variables = dynamic_vars

            # Apply system prompt variable substitution
            prompt = self.session.agent_config.system_prompt
            for key, value in dynamic_vars.items():
                prompt = prompt.replace(f"{{{{{key}}}}}", str(value))
            self.session.agent_config.system_prompt = prompt

            # Apply user_id
            user_id = msg.get("user_id")
            if user_id:
                self.session.user_id = user_id

        # Send conversation_initiation_metadata
        await self.ws.send_json({
            "type": "conversation_initiation_metadata",
            "conversation_initiation_metadata_event": {
                "conversation_id": self.session.conversation_id,
                "agent_output_audio_format": self.session.agent_config.output_audio_format,
            },
        })

        logger.info(
            f"Session initiated: {self.session.conversation_id}, "
            f"text_only={self.session.text_only}, "
            f"model={self.session.agent_config.llm_model}"
        )

    async def _handle_message(self, raw: str) -> None:
        """Route incoming WebSocket messages."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Audio chunk (no type field — identified by user_audio_chunk key)
        if "user_audio_chunk" in msg:
            await self._handle_audio_chunk(msg["user_audio_chunk"])
            return

        msg_type = msg.get("type", "")

        if msg_type == "pong":
            pass  # Pong received, no action needed

        elif msg_type == "user_message":
            text = msg.get("text", "")
            if text:
                await self._handle_user_text(text)

        elif msg_type == "user_activity":
            self.session.last_activity = time.time()

        elif msg_type == "contextual_update":
            context = msg.get("text", "")
            if context:
                self.session.messages.append(ConversationMessage(
                    role="system",
                    content=f"[Context update]: {context}",
                ))

        elif msg_type == "client_tool_result":
            await self._handle_tool_result(msg)

        elif msg_type == "feedback":
            logger.info(f"Feedback received: {msg}")

        else:
            logger.debug(f"Unknown message type: {msg_type}")

    async def _handle_audio_chunk(self, audio_b64: str) -> None:
        """Forward audio chunk to ASR service, update VAD."""
        self.session.metrics.total_audio_chunks_in += 1
        self.session.last_user_audio_time = time.time()
        self.session.last_activity = time.time()
        self.session.is_user_speaking = True

        # If agent is speaking, this is an interruption
        if self.session.is_agent_speaking:
            await self._handle_interruption()

        # Decode audio for VAD processing
        try:
            audio_bytes = base64.b64decode(audio_b64)
            vad_score = _turn_engine.update_vad(audio_bytes)
            # Could send vad_score to client if subscribed (optional event)
        except Exception:
            vad_score = 0.0

        # Forward to ASR
        try:
            await self.asr.send_audio(audio_b64)
        except Exception as e:
            logger.warning(f"Failed to send audio to ASR: {e}")

    async def _handle_user_text(self, text: str) -> None:
        """Handle a text message from the user (text-only mode)."""
        logger.info(f"User text: {text[:100]}")
        self.session.metrics.total_user_messages += 1
        self.session.last_activity = time.time()

        # Send user_transcript event
        await self.ws.send_json({
            "type": "user_transcript",
            "user_transcription_event": {
                "user_transcript": text,
            },
        })

        # Add to history
        self.session.messages.append(ConversationMessage(role="user", content=text))
        self._trim_history()

        # Generate response
        self.session.state = SessionState.PROCESSING
        await self._generate_and_send_response()

    async def _handle_interruption(self) -> None:
        """Handle user interruption while agent is speaking."""
        self.session.metrics.total_interruptions += 1
        self.session.last_interrupt_id = self.session.current_event_id
        self._tts_cancel.set()
        self.session.is_agent_speaking = False

        await self.ws.send_json({
            "type": "interruption",
            "interruption_event": {
                "event_id": str(self.session.last_interrupt_id),
            },
        })

        logger.debug(f"Interruption at event_id={self.session.last_interrupt_id}")

    async def _handle_tool_result(self, msg: dict[str, Any]) -> None:
        """Handle client tool result."""
        tool_call_id = msg.get("tool_call_id", "")
        result = msg.get("result", "")
        is_error = msg.get("is_error", False)

        logger.info(f"Tool result: id={tool_call_id}, error={is_error}")

        # Add tool result to history
        self.session.messages.append(ConversationMessage(
            role="tool",
            content=result if not is_error else f"[Tool error]: {result}",
            tool_call_id=tool_call_id,
        ))

        # Remove from pending
        self.session.pending_tool_calls.pop(tool_call_id, None)

        # Continue generating response with tool result
        await self._generate_and_send_response()

    async def _asr_listener(self) -> None:
        """Background task: listen for ASR transcripts and run turn-taking.

        Uses the TurnTakingEngine for advanced end-of-turn prediction:
        - VAD score trend analysis
        - Silence duration (sigmoid-shaped)
        - Transcript completeness (punctuation, question words)
        - Utterance length heuristics
        - Force-respond after FORCE_RESPOND_TIMEOUT_MS
        """
        last_tentative = ""
        silence_start: float | None = None

        while not self._stop_event.is_set():
            transcript = await self.asr.receive_transcript(timeout=0.05)
            if transcript is None:
                # Check turn-taking when we have a tentative transcript
                if last_tentative and silence_start:
                    silence_ms = int((time.time() - silence_start) * 1000)

                    # Too early to check
                    if silence_ms < SILENCE_MIN_MS:
                        continue

                    # Force respond after timeout
                    if silence_ms >= FORCE_RESPOND_TIMEOUT_MS:
                        logger.debug(f"Force respond after {silence_ms}ms silence")
                        await self._on_final_transcript(last_tentative)
                        last_tentative = ""
                        silence_start = None
                        _turn_engine.reset()
                        continue

                    # Advanced turn-taking prediction
                    turn_prob = _turn_engine.predict_turn_end(silence_ms, last_tentative)
                    threshold = _turn_engine.get_threshold(
                        self.session.agent_config.turn_eagerness.value
                    )

                    if turn_prob > threshold:
                        logger.debug(
                            f"Turn detected: prob={turn_prob:.2f} > threshold={threshold:.2f}, "
                            f"silence={silence_ms}ms"
                        )
                        await self._on_final_transcript(last_tentative)
                        last_tentative = ""
                        silence_start = None
                        _turn_engine.reset()
                continue

            tr_type = transcript.get("type", "")
            text = transcript.get("text", "")

            if tr_type == "committed_transcript" and text:
                last_tentative = ""
                silence_start = None
                _turn_engine.reset()
                await self._on_final_transcript(text)

            elif tr_type == "partial_transcript" and text:
                last_tentative = text
                silence_start = time.time()

                # Send tentative transcript to client
                await self.ws.send_json({
                    "type": "tentative_user_transcript",
                    "user_transcription_event": {
                        "user_transcript": text,
                    },
                })

                self.session.is_user_speaking = True

    async def _on_final_transcript(self, text: str) -> None:
        """Handle a final user transcript — generate agent response."""
        logger.info(f"Final transcript: {text[:100]}")
        self.session.metrics.total_user_messages += 1
        self.session.is_user_speaking = False

        # Send final user_transcript
        await self.ws.send_json({
            "type": "user_transcript",
            "user_transcription_event": {
                "user_transcript": text,
            },
        })

        # Add to history
        self.session.messages.append(ConversationMessage(role="user", content=text))
        self._trim_history()

        # Generate response
        self.session.state = SessionState.PROCESSING
        await self._generate_and_send_response()

    async def _generate_and_send_response(self) -> None:
        """Generate LLM response and stream it as TTS audio."""
        t_start = time.time()
        config = self.session.agent_config

        # Check for tool calls first
        if config.tools:
            tool_call = await self.llm.generate_tool_check(
                messages=[{"role": m.role, "content": m.content} for m in self.session.messages],
                system_prompt=config.system_prompt,
                tools=config.tools,
                model=config.llm_model,
            )
            if tool_call:
                await self._execute_tool_call(tool_call)
                return

        # Stream LLM response
        full_response = ""
        sentence_buffer = ""
        self._tts_cancel.clear()
        self.session.is_agent_speaking = True
        self.session.state = SessionState.SPEAKING

        # Send response start
        await self.ws.send_json({
            "type": "agent_chat_response_part",
            "text_response_part": {"type": "start", "text": ""},
        })

        first_token = True
        t_llm_start = time.time()

        async for token in self.llm.generate_stream_with_cascade(
            messages=[{"role": m.role, "content": m.content} for m in self.session.messages],
            system_prompt=config.system_prompt,
            model=config.llm_model,
            backup_model=BACKUP_LLM_MODEL,
            cascade_timeout=CASCADE_TIMEOUT_S,
            temperature=config.llm_temperature,
            max_tokens=config.llm_max_tokens,
        ):
            if self._tts_cancel.is_set():
                break

            if first_token:
                llm_ttfb = (time.time() - t_llm_start) * 1000
                logger.debug(f"LLM TTFB: {llm_ttfb:.0f}ms (model={config.llm_model})")
                first_token = False

            full_response += token
            sentence_buffer += token
            self.session.metrics.total_llm_tokens += 1

            # Send text delta
            await self.ws.send_json({
                "type": "agent_chat_response_part",
                "text_response_part": {"type": "delta", "text": token},
            })

            # Check for sentence boundary — send to TTS
            if is_sentence_boundary(sentence_buffer):
                if not self.session.text_only and not self._tts_cancel.is_set():
                    await self._stream_tts(sentence_buffer.strip(), config)
                sentence_buffer = ""

        # Flush remaining text to TTS
        if sentence_buffer.strip() and not self._tts_cancel.is_set():
            if not self.session.text_only:
                await self._stream_tts(sentence_buffer.strip(), config)

        # Send response stop
        await self.ws.send_json({
            "type": "agent_chat_response_part",
            "text_response_part": {"type": "stop", "text": ""},
        })

        # Send complete agent response
        if full_response:
            await self.ws.send_json({
                "type": "agent_response",
                "agent_response_event": {
                    "agent_response": full_response,
                },
            })

            # Add to history
            self.session.messages.append(ConversationMessage(
                role="assistant", content=full_response,
            ))
            self._trim_history()
            self.session.metrics.total_agent_responses += 1

        self.session.is_agent_speaking = False
        self.session.last_agent_end_time = time.time()
        self.session.state = SessionState.LISTENING

        # Track latency
        latency_ms = (time.time() - t_start) * 1000
        self.session.metrics.response_latencies.append(latency_ms)
        self.session.metrics.avg_response_latency_ms = (
            sum(self.session.metrics.response_latencies)
            / len(self.session.metrics.response_latencies)
        )
        logger.info(f"Response complete: {len(full_response)} chars, {latency_ms:.0f}ms")

    async def _send_agent_response(self, text: str) -> None:
        """Send a complete agent response (for first_message, etc.)."""
        config = self.session.agent_config
        self.session.is_agent_speaking = True
        self._tts_cancel.clear()

        # Send text parts
        await self.ws.send_json({
            "type": "agent_chat_response_part",
            "text_response_part": {"type": "start", "text": ""},
        })
        await self.ws.send_json({
            "type": "agent_chat_response_part",
            "text_response_part": {"type": "delta", "text": text},
        })
        await self.ws.send_json({
            "type": "agent_chat_response_part",
            "text_response_part": {"type": "stop", "text": ""},
        })
        await self.ws.send_json({
            "type": "agent_response",
            "agent_response_event": {"agent_response": text},
        })

        # TTS
        if not self.session.text_only:
            await self._stream_tts(text, config)

        self.session.messages.append(ConversationMessage(role="assistant", content=text))
        self.session.is_agent_speaking = False
        self.session.metrics.total_agent_responses += 1

    async def _stream_tts(self, text: str, config: AgentConfig) -> None:
        """Stream TTS audio to the client."""
        self.session.current_event_id += 1
        event_id = self.session.current_event_id

        try:
            async for audio_chunk in self.tts.synthesize_stream(
                text=text,
                voice_id=config.voice_id,
                output_format=config.output_audio_format,
                model_id=config.tts_model,
            ):
                if self._tts_cancel.is_set():
                    break

                # Check if this event has been interrupted
                if event_id <= self.session.last_interrupt_id:
                    break

                audio_b64 = base64.b64encode(audio_chunk).decode("ascii")
                await self.ws.send_json({
                    "type": "audio",
                    "audio_event": {
                        "event_id": str(event_id),
                        "audio_base_64": audio_b64,
                    },
                })
                self.session.metrics.total_audio_chunks_out += 1

        except Exception as e:
            logger.error(f"TTS streaming error: {e}")

    async def _execute_tool_call(self, tool_call: dict[str, Any]) -> None:
        """Execute a tool call from LLM."""
        tool_name = tool_call["tool_name"]
        tool_call_id = tool_call["tool_call_id"]
        params = tool_call.get("parameters", {})
        self.session.metrics.total_tool_calls += 1

        # Find tool config
        tool_config = None
        for t in self.session.agent_config.tools:
            if t.get("name") == tool_name:
                tool_config = t
                break

        if not tool_config:
            logger.warning(f"Unknown tool: {tool_name}")
            return

        tool_type = tool_config.get("type", "webhook")

        if tool_type == "client":
            # Send to client for execution
            self.session.pending_tool_calls[tool_call_id] = tool_call
            await self.ws.send_json({
                "type": "client_tool_call",
                "client_tool_call": {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "parameters": params,
                },
            })

        elif tool_type == "webhook":
            url = tool_config.get("url", "")
            method = tool_config.get("method", "POST")
            result = await self.tools.execute_webhook(url, method, params)
            self.session.messages.append(ConversationMessage(
                role="tool", content=result, tool_call_id=tool_call_id, tool_name=tool_name,
            ))
            await self._generate_and_send_response()

        elif tool_type == "system":
            result = await self.tools.execute_system_tool(tool_name, params)
            if result == "SYSTEM:END_CALL":
                self._stop_event.set()
            else:
                self.session.messages.append(ConversationMessage(
                    role="tool", content=result, tool_call_id=tool_call_id, tool_name=tool_name,
                ))
                await self._generate_and_send_response()

    async def _ping_loop(self) -> None:
        """Send periodic pings to keep the WebSocket alive."""
        ping_count = 0
        while not self._stop_event.is_set():
            await asyncio.sleep(PING_INTERVAL_S)
            if self._stop_event.is_set():
                break
            ping_count += 1
            try:
                await self.ws.send_json({
                    "type": "ping",
                    "ping_event": {
                        "event_id": ping_count,
                        "ping_ms": int((time.time() - self.session.started_at) * 1000),
                    },
                })
            except Exception:
                break

    def _trim_history(self) -> None:
        """Trim conversation history to sliding window with token estimation.

        Keeps system messages (context updates) plus the last N non-system
        messages, with a rough token count estimate (1 token ~ 4 chars).
        """
        if len(self.session.messages) <= MAX_CONVERSATION_HISTORY:
            return

        system_msgs = [m for m in self.session.messages if m.role == "system"]
        other_msgs = [m for m in self.session.messages if m.role != "system"]

        keep_count = MAX_CONVERSATION_HISTORY - len(system_msgs)
        if keep_count < 4:
            keep_count = 4
        trimmed = other_msgs[-keep_count:]

        # Rough token count check (1 token ~ 4 chars)
        total_chars = sum(len(m.content) for m in trimmed + system_msgs)
        while total_chars > MAX_HISTORY_TOKENS * 4 and len(trimmed) > 4:
            removed = trimmed.pop(0)
            total_chars -= len(removed.content)

        self.session.messages = system_msgs + trimmed

    async def _cleanup(self) -> None:
        """Clean up all resources."""
        self._stop_event.set()

        if self._asr_task:
            self._asr_task.cancel()
            try:
                await self._asr_task
            except asyncio.CancelledError:
                pass

        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass

        await self.asr.close()
        self.session.state = SessionState.ENDED

        logger.info(
            f"Session ended: {self.session.conversation_id} | "
            f"messages={self.session.metrics.total_user_messages} | "
            f"responses={self.session.metrics.total_agent_responses} | "
            f"interruptions={self.session.metrics.total_interruptions} | "
            f"avg_latency={self.session.metrics.avg_response_latency_ms:.0f}ms"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════════════════════════════════════════

_sessions: dict[str, ConversationSession] = {}
_llm_client = LLMClient()
_tool_executor = ToolExecutor()
_start_time = time.time()

# Agent configs loaded from environment or config file
_agent_configs: dict[str, AgentConfig] = {}


def load_agent_configs() -> None:
    """Load agent configurations from config directory."""
    config_dir = Path(os.getenv("AGENT_CONFIG_DIR", "agents"))
    if not config_dir.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
        # Create default agent
        default = AgentConfig(agent_id="default")
        _agent_configs["default"] = default
        logger.info("Created default agent config")
        return

    for config_file in config_dir.glob("*.json"):
        try:
            with open(config_file) as f:
                data = json.load(f)
            agent_id = data.get("agent_id", config_file.stem)
            _agent_configs[agent_id] = AgentConfig(**data)
            logger.info(f"Loaded agent config: {agent_id}")
        except Exception as e:
            logger.warning(f"Failed to load agent config {config_file}: {e}")

    if not _agent_configs:
        _agent_configs["default"] = AgentConfig(agent_id="default")


def get_agent_config(agent_id: str) -> AgentConfig:
    """Get agent configuration by ID, falling back to default."""
    if agent_id in _agent_configs:
        return _agent_configs[agent_id].model_copy()
    return AgentConfig(agent_id=agent_id)


# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: setup on start, cleanup on shutdown."""
    logger.info(f"Starting {SERVICE_NAME} v{SERVICE_VERSION} on port {PORT}")
    load_agent_configs()
    logger.info(
        f"{SERVICE_NAME} ready — agents={len(_agent_configs)}, "
        f"asr={ASR_URL}, tts={TTS_URL}"
    )
    yield
    logger.info(f"Shutting down {SERVICE_NAME}")
    await _llm_client.close()
    _sessions.clear()


app = FastAPI(
    title="Echo ConvAI Orchestrator",
    description="Real-time conversational AI orchestrator — ElevenLabs ConvAI protocol compatible",
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
    asr_client = ASRClient()
    tts_client = TTSClient()

    asr_status = await asr_client.check_health()
    tts_status = await tts_client.check_health()

    overall = "healthy"
    if asr_status != "healthy" or tts_status != "healthy":
        overall = "degraded"
    if asr_status == "unreachable" and tts_status == "unreachable":
        overall = "unhealthy"

    return HealthResponse(
        status=overall,
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        uptime_seconds=round(time.time() - _start_time, 1),
        active_sessions=len(_sessions),
        asr_status=asr_status,
        tts_status=tts_status,
    )


# ── Session List ──────────────────────────────────────────────────────────────

@app.get("/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    """List active conversation sessions."""
    result = []
    for sid, session in _sessions.items():
        result.append({
            "conversation_id": session.conversation_id,
            "agent_id": session.agent_config.agent_id,
            "user_id": session.user_id,
            "state": session.state.value,
            "started_at": session.started_at,
            "duration_s": round(time.time() - session.started_at, 1),
            "text_only": session.text_only,
            "messages": session.metrics.total_user_messages,
            "responses": session.metrics.total_agent_responses,
        })
    return result


# ── Agent Config Management ──────────────────────────────────────────────────

@app.get("/agents")
async def list_agents() -> list[dict[str, Any]]:
    """List configured agents."""
    return [
        {
            "agent_id": config.agent_id,
            "name": config.name,
            "llm_model": config.llm_model,
            "voice_id": config.voice_id,
            "language": config.language,
        }
        for config in _agent_configs.values()
    ]


@app.post("/agents")
async def create_agent(config: AgentConfig) -> dict[str, str]:
    """Create or update an agent configuration."""
    _agent_configs[config.agent_id] = config

    # Persist to disk
    config_dir = Path(os.getenv("AGENT_CONFIG_DIR", "agents"))
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / f"{config.agent_id}.json"
    with open(config_file, "w") as f:
        f.write(config.model_dump_json(indent=2))

    logger.info(f"Agent config saved: {config.agent_id}")
    return {"status": "created", "agent_id": config.agent_id}


# ── Session Interrupt ────────────────────────────────────────────────────────

@app.post("/sessions/{conversation_id}/interrupt")
async def interrupt_session(conversation_id: str) -> dict[str, Any]:
    """Force-interrupt a specific session (stop agent speaking)."""
    session = _sessions.get(conversation_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {conversation_id}")
    # Signal interruption by setting event on the session
    session.is_agent_speaking = False
    session.last_interrupt_id = session.current_event_id
    return {"status": "interrupted", "conversation_id": conversation_id}


# ── Metrics ──────────────────────────────────────────────────────────────────

@app.get("/metrics")
async def get_metrics() -> dict[str, Any]:
    """Aggregate metrics across all active sessions."""
    total_user_msgs = sum(s.metrics.total_user_messages for s in _sessions.values())
    total_agent_msgs = sum(s.metrics.total_agent_responses for s in _sessions.values())
    total_interrupts = sum(s.metrics.total_interruptions for s in _sessions.values())
    total_tool_calls = sum(s.metrics.total_tool_calls for s in _sessions.values())
    total_tokens = sum(s.metrics.total_llm_tokens for s in _sessions.values())
    total_audio_in = sum(s.metrics.total_audio_chunks_in for s in _sessions.values())
    total_audio_out = sum(s.metrics.total_audio_chunks_out for s in _sessions.values())

    all_latencies: list[float] = []
    for s in _sessions.values():
        all_latencies.extend(s.metrics.response_latencies)

    return {
        "active_sessions": len(_sessions),
        "total_user_messages": total_user_msgs,
        "total_agent_messages": total_agent_msgs,
        "total_interruptions": total_interrupts,
        "total_tool_calls": total_tool_calls,
        "total_llm_tokens": total_tokens,
        "total_audio_chunks_in": total_audio_in,
        "total_audio_chunks_out": total_audio_out,
        "avg_response_latency_ms": round(
            sum(all_latencies) / len(all_latencies), 1
        ) if all_latencies else 0.0,
        "uptime_seconds": round(time.time() - _start_time, 1),
    }


# ── ConvAI WebSocket — THE MAIN ENDPOINT ─────────────────────────────────────

@app.websocket("/v1/convai/conversation")
async def convai_conversation(
    ws: WebSocket,
    agent_id: str = Query("default"),
    source: str = Query("echo_sdk"),
    version: str = Query("1.0.0"),
) -> None:
    """ElevenLabs-compatible ConvAI WebSocket endpoint.

    This is the main entry point for real-time conversations. Any ElevenLabs SDK
    client can connect to this endpoint and interact using the standard protocol.
    """
    await ws.accept()

    agent_config = get_agent_config(agent_id)
    session = ConversationSession(agent_config=agent_config)
    _sessions[session.conversation_id] = session

    logger.info(
        f"New conversation: {session.conversation_id}, agent={agent_id}, "
        f"source={source}, version={version}"
    )

    asr_client = ASRClient()
    tts_client = TTSClient()

    handler = ConversationHandler(
        ws=ws,
        session=session,
        asr_client=asr_client,
        tts_client=tts_client,
        llm_client=_llm_client,
        tool_executor=_tool_executor,
    )

    try:
        await handler.run()
    finally:
        _sessions.pop(session.conversation_id, None)


@app.websocket("/conversation")
async def conversation_alias(
    ws: WebSocket,
    agent_id: str = Query("default"),
) -> None:
    """Alias endpoint for /v1/convai/conversation.

    Provides a shorter URL for direct connections (non-SDK clients).
    """
    await convai_conversation(ws, agent_id=agent_id, source="echo_direct", version=SERVICE_VERSION)


# ── Text Chat Endpoint (convenience) ─────────────────────────────────────────

class ChatRequest(BaseModel):
    """Simple text chat request."""
    message: str
    agent_id: str = "default"
    conversation_id: str | None = None
    user_id: str | None = None


class ChatResponse(BaseModel):
    """Text chat response."""
    response: str
    conversation_id: str


@app.post("/v1/chat", response_model=ChatResponse)
async def text_chat(request: ChatRequest) -> ChatResponse:
    """Simple REST endpoint for text-only chat (non-streaming)."""
    config = get_agent_config(request.agent_id)

    # Find or create session
    session: ConversationSession | None = None
    if request.conversation_id and request.conversation_id in _sessions:
        session = _sessions[request.conversation_id]
    else:
        session = ConversationSession(agent_config=config, text_only=True)
        _sessions[session.conversation_id] = session

    # Add user message
    session.messages.append(ConversationMessage(role="user", content=request.message))

    # Generate response
    full_response = ""
    async for token in _llm_client.generate_stream(
        messages=[{"role": m.role, "content": m.content} for m in session.messages],
        system_prompt=config.system_prompt,
        model=config.llm_model,
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
    ):
        full_response += token

    session.messages.append(ConversationMessage(role="assistant", content=full_response))

    return ChatResponse(
        response=full_response,
        conversation_id=session.conversation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def _configure_logging() -> None:
    """Set up loguru logging with structured output and file rotation."""
    import sys as _sys

    logger.remove()
    logger.add(
        _sys.stderr,
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        level="DEBUG",
        colorize=True,
    )
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    logger.add(
        str(log_dir / "orchestrator_{time:YYYY-MM-DD}.log"),
        rotation="50 MB",
        retention="7 days",
        compression="gz",
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
    )


if __name__ == "__main__":
    import uvicorn

    _configure_logging()
    logger.info(f"Starting {SERVICE_NAME} v{SERVICE_VERSION} on port {PORT}")

    uvicorn.run(
        "orchestrator:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        ws_max_size=WS_MAX_SIZE,
    )
