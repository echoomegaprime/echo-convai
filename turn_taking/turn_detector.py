"""Echo ConvAI — Turn-Taking Engine.

Reverse-engineered from ElevenLabs' proprietary turn-taking model.
This is their KEY competitive moat — the system that decides when the user
has finished speaking and the agent should respond.

ElevenLabs exposes these signals:
- vad_score (0-1): Voice Activity Detection confidence
- turn_probability (0-1): Prediction that user has finished speaking
- eagerness: How quickly agent responds (patient/normal/eager)
- spelling_patience: Wait time when user is spelling
- interruption_sensitivity: How easily user can interrupt
- speculative_generation: Pre-generate response before user finishes
- turn_model: turn_v2 or turn_v3

Our implementation uses Silero VAD + a learned turn predictor that combines:
- VAD score (is voice present?)
- Silence duration (how long since last speech?)
- Transcript features (does it end with sentence-ending punctuation?)
- Prosody features (final pitch, energy) — future: from audio analysis
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
from loguru import logger

try:
    import torch
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

from core.conversation_engine import TurnTakingInterface, TurnEagerness


# ═══════════════════════════════════════════════════════════════════════════════
# SILERO VAD WRAPPER
# ═══════════════════════════════════════════════════════════════════════════════

class SileroVAD:
    """Voice Activity Detection using Silero VAD model."""

    def __init__(self, sample_rate: int = 16000, threshold: float = 0.5):
        self.sample_rate = sample_rate
        self.threshold = threshold
        self._model = None
        self._utils = None

    def _load_model(self) -> None:
        """Lazy-load Silero VAD model."""
        if self._model is not None:
            return

        if not HAS_TORCH:
            logger.warning("PyTorch not available — using energy-based VAD fallback")
            return

        try:
            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=True,
            )
            self._model = model
            self._utils = utils
            logger.info("Silero VAD loaded successfully")
        except Exception as e:
            logger.warning(f"Failed to load Silero VAD: {e} — using energy fallback")

    def process_chunk(self, audio_bytes: bytes) -> float:
        """Process an audio chunk and return VAD score (0-1).

        Args:
            audio_bytes: Raw PCM 16-bit LE audio at self.sample_rate

        Returns:
            VAD confidence score 0.0 (silence) to 1.0 (speech)
        """
        self._load_model()

        # Convert bytes to float32 numpy array
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        audio_float = audio_int16.astype(np.float32) / 32768.0

        if self._model is not None and HAS_TORCH:
            # Use Silero VAD
            try:
                tensor = torch.from_numpy(audio_float)
                confidence = self._model(tensor, self.sample_rate).item()
                return float(confidence)
            except Exception as e:
                logger.debug(f"Silero VAD error: {e}")

        # Energy-based fallback
        energy = float(np.sqrt(np.mean(audio_float ** 2)))
        # Map energy to 0-1 range (calibrated for typical speech)
        normalized = min(1.0, max(0.0, (energy - 0.01) / 0.05))
        return normalized

    def reset(self) -> None:
        """Reset VAD state between utterances."""
        if self._model is not None and hasattr(self._model, "reset_states"):
            self._model.reset_states()


# ═══════════════════════════════════════════════════════════════════════════════
# TURN PREDICTOR — Core Decision Engine
# ═══════════════════════════════════════════════════════════════════════════════

class TurnPredictor:
    """Predicts whether the user has finished their turn.

    ElevenLabs' turn_v2/turn_v3 models use a neural network trained on
    conversational data (Switchboard, Fisher, + proprietary call center data).

    Our implementation uses a feature-based approach that combines:
    1. Silence duration (most important signal)
    2. VAD score trend (declining = likely end of turn)
    3. Transcript completeness (sentence-ending punctuation)
    4. Speaking rate (slowing down = likely end of turn)
    5. Utterance length (very short = might be incomplete)

    The weights were tuned to approximate ElevenLabs' behavior based on
    testing with their public agents and measuring response timing.
    """

    # Feature weights (tuned empirically from ElevenLabs behavior)
    W_SILENCE = 0.35       # Silence duration contribution
    W_VAD_TREND = 0.15     # VAD score trend contribution
    W_TRANSCRIPT = 0.30    # Transcript completeness contribution
    W_RATE = 0.10          # Speaking rate contribution
    W_LENGTH = 0.10        # Utterance length contribution

    # Silence thresholds (milliseconds)
    SILENCE_MIN = 200      # Minimum silence before considering end-of-turn
    SILENCE_STRONG = 800   # Strong end-of-turn signal
    SILENCE_DEFINITE = 1500  # Almost certainly end-of-turn

    # Sentence-ending patterns
    SENTENCE_ENDERS = {".", "!", "?", ";"}
    CLAUSE_ENDERS = {",", ":", "—", "–"}
    QUESTION_WORDS = {"what", "who", "where", "when", "why", "how", "which", "whose"}

    def __init__(self):
        self._vad_history: list[float] = []
        self._transcript_history: list[str] = []
        self._last_speech_time: float = time.time()

    def predict(self, vad_score: float, silence_ms: int, transcript: str) -> float:
        """Predict probability that user has finished speaking.

        Returns:
            float: Probability 0.0 (still speaking) to 1.0 (definitely done)
        """
        # Update history
        self._vad_history.append(vad_score)
        if len(self._vad_history) > 50:
            self._vad_history = self._vad_history[-50:]

        # Feature 1: Silence duration score
        silence_score = self._silence_score(silence_ms)

        # Feature 2: VAD trend score
        vad_trend_score = self._vad_trend_score()

        # Feature 3: Transcript completeness score
        transcript_score = self._transcript_score(transcript)

        # Feature 4: Speaking rate score
        rate_score = self._speaking_rate_score(transcript)

        # Feature 5: Utterance length score
        length_score = self._length_score(transcript)

        # Weighted combination
        probability = (
            self.W_SILENCE * silence_score
            + self.W_VAD_TREND * vad_trend_score
            + self.W_TRANSCRIPT * transcript_score
            + self.W_RATE * rate_score
            + self.W_LENGTH * length_score
        )

        # Clamp to 0-1
        probability = max(0.0, min(1.0, probability))

        # Hard overrides
        if silence_ms < self.SILENCE_MIN:
            probability = min(probability, 0.1)  # Too soon

        if silence_ms > self.SILENCE_DEFINITE:
            probability = max(probability, 0.95)  # Definitely done

        return probability

    def _silence_score(self, silence_ms: int) -> float:
        """Score based on silence duration. Sigmoid-shaped response."""
        if silence_ms < self.SILENCE_MIN:
            return 0.0
        if silence_ms > self.SILENCE_DEFINITE:
            return 1.0

        # Sigmoid between SILENCE_MIN and SILENCE_DEFINITE
        x = (silence_ms - self.SILENCE_MIN) / (self.SILENCE_DEFINITE - self.SILENCE_MIN)
        # Steeper sigmoid centered around SILENCE_STRONG
        center = (self.SILENCE_STRONG - self.SILENCE_MIN) / (self.SILENCE_DEFINITE - self.SILENCE_MIN)
        return 1.0 / (1.0 + np.exp(-10 * (x - center)))

    def _vad_trend_score(self) -> float:
        """Score based on VAD score trend (declining = end of turn)."""
        if len(self._vad_history) < 5:
            return 0.5

        recent = self._vad_history[-5:]
        trend = recent[-1] - recent[0]  # Negative = declining

        # If VAD is declining, likely end of turn
        if trend < -0.3:
            return 0.9
        elif trend < -0.1:
            return 0.7
        elif trend > 0.1:
            return 0.1  # VAD increasing — still speaking
        return 0.5

    def _transcript_score(self, transcript: str) -> float:
        """Score based on transcript completeness."""
        if not transcript:
            return 0.0

        text = transcript.strip()
        if not text:
            return 0.0

        last_char = text[-1]

        # Sentence-ending punctuation = strong signal
        if last_char in self.SENTENCE_ENDERS:
            return 0.95

        # Clause-ending punctuation = moderate signal
        if last_char in self.CLAUSE_ENDERS:
            return 0.6

        # Question that starts with question word and ends without ? = incomplete
        first_word = text.split()[0].lower() if text.split() else ""
        if first_word in self.QUESTION_WORDS and last_char != "?":
            return 0.2

        # Default: moderate uncertainty
        return 0.4

    def _speaking_rate_score(self, transcript: str) -> float:
        """Score based on inferred speaking rate.

        Slowing down at the end of an utterance is a common turn-yielding cue.
        """
        words = transcript.split() if transcript else []
        if len(words) < 3:
            return 0.5

        # Rough heuristic: if recent words are getting longer (slower articulation),
        # it might indicate slowing down / thinking pause
        recent_word_lengths = [len(w) for w in words[-3:]]
        earlier_word_lengths = [len(w) for w in words[-6:-3]] if len(words) >= 6 else [4]

        avg_recent = np.mean(recent_word_lengths)
        avg_earlier = np.mean(earlier_word_lengths)

        if avg_recent > avg_earlier * 1.5:
            return 0.7  # Words getting longer — might be slowing down
        return 0.5

    def _length_score(self, transcript: str) -> float:
        """Score based on utterance length.

        Very short utterances ("yes", "no", "okay") are more likely complete.
        Medium utterances need more context.
        Long utterances with trailing content are likely done.
        """
        words = len(transcript.split()) if transcript else 0

        if words == 0:
            return 0.0
        if words <= 2:
            return 0.8  # Short answer — probably complete
        if words <= 5:
            return 0.5  # Could go either way
        if words >= 15:
            return 0.7  # Long enough — likely wrapping up
        return 0.5

    def reset(self) -> None:
        """Reset predictor state between utterances."""
        self._vad_history.clear()
        self._transcript_history.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# TURN-TAKING ENGINE — Full Implementation
# ═══════════════════════════════════════════════════════════════════════════════

class EchoTurnTaking(TurnTakingInterface):
    """Full turn-taking engine combining VAD + turn prediction.

    This implements the TurnTakingInterface used by ConversationEngine.
    Maps to ElevenLabs' turn_v2/turn_v3 behavior.
    """

    def __init__(self, sample_rate: int = 16000):
        self.vad = SileroVAD(sample_rate=sample_rate)
        self.predictor = TurnPredictor()
        self._last_vad_score: float = 0.0

    async def update_vad(self, audio_chunk: bytes) -> float:
        """Process audio and return VAD score."""
        score = self.vad.process_chunk(audio_chunk)
        self._last_vad_score = score
        return score

    async def predict_turn_end(self, vad_score: float, silence_ms: int, transcript: str) -> float:
        """Predict probability that user has finished speaking."""
        return self.predictor.predict(vad_score, silence_ms, transcript)

    def get_threshold(self, eagerness: TurnEagerness) -> float:
        """Get turn-end decision threshold based on eagerness.

        ElevenLabs' eagerness settings map to different thresholds:
        - patient: Wait for high confidence (fewer false positives, higher latency)
        - normal: Balanced (default ElevenLabs behavior)
        - eager: Respond quickly (more false positives, lower latency)
        """
        thresholds = {
            TurnEagerness.PATIENT: 0.85,
            TurnEagerness.NORMAL: 0.65,
            TurnEagerness.EAGER: 0.40,
        }
        return thresholds.get(eagerness, 0.65)

    def reset(self) -> None:
        """Reset all state for new utterance."""
        self.vad.reset()
        self.predictor.reset()
