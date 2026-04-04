"""Echo ConvAI SDK — Audio I/O utilities for microphone capture and PCM playback.

Uses ``sounddevice`` for cross-platform audio with minimal latency.
All audio is 16-bit PCM mono at a configurable sample rate (default 16kHz).

Usage::

    from echo_convai.audio import AudioRecorder, AudioPlayer

    player = AudioPlayer(sample_rate=16000)
    player.start()
    player.play(pcm_bytes)
    player.stop()

    recorder = AudioRecorder(sample_rate=16000)
    recorder.start(on_chunk=lambda chunk: print(len(chunk)))
    recorder.stop()
"""

from __future__ import annotations

import asyncio
import base64
import queue
import threading
from typing import Callable

import numpy as np
import sounddevice as sd
from loguru import logger

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_DTYPE = "int16"
INPUT_BLOCK_SIZE = 4000  # 250ms @ 16kHz
OUTPUT_BLOCK_SIZE = 1000  # 62.5ms @ 16kHz
VOLUME_RMS_THRESHOLD = 100  # RMS threshold for "voice detected"


class AudioPlayer:
    """Plays received PCM audio chunks through the default output device.

    Audio chunks are queued and played sequentially in a background thread.
    Supports interruption (queue drain) for barge-in handling.

    Args:
        sample_rate: Output sample rate in Hz.
        channels: Number of audio channels (1 = mono).
        block_size: Frames per output buffer.
        device: Output device index (None = system default).
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        block_size: int = OUTPUT_BLOCK_SIZE,
        device: int | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._block_size = block_size
        self._device = device
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._stream: sd.RawOutputStream | None = None
        self._running = False
        self._play_thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        """Whether the player is actively running."""
        return self._running

    def start(self) -> None:
        """Start the audio output stream and playback thread."""
        if self._running:
            return

        self._running = True
        self._stream = sd.RawOutputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype=DEFAULT_DTYPE,
            blocksize=self._block_size,
            device=self._device,
        )
        self._stream.start()
        self._play_thread = threading.Thread(target=self._playback_loop, daemon=True, name="audio-player")
        self._play_thread.start()
        logger.debug("AudioPlayer started | rate={} blocksize={}", self._sample_rate, self._block_size)

    def stop(self) -> None:
        """Stop the audio output stream and drain the queue."""
        if not self._running:
            return

        self._running = False
        self._queue.put(None)  # Sentinel to unblock the thread

        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=2.0)

        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.debug("Stream close error (ignored): {}", exc)
            self._stream = None

        logger.debug("AudioPlayer stopped")

    def play(self, pcm_data: bytes) -> None:
        """Queue PCM audio bytes for playback.

        Args:
            pcm_data: Raw 16-bit PCM mono audio bytes.
        """
        if self._running:
            self._queue.put(pcm_data)

    def interrupt(self) -> None:
        """Clear the playback queue (used when user interrupts the agent)."""
        cleared = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                cleared += 1
            except queue.Empty:
                break
        if cleared:
            logger.debug("Audio queue cleared | {} chunks dropped", cleared)

    def _playback_loop(self) -> None:
        """Background thread: dequeue and write audio to the output stream."""
        while self._running:
            try:
                data = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue

            if data is None:
                break

            if self._stream and self._running:
                try:
                    self._stream.write(data)
                except Exception as exc:
                    logger.error("Playback write error: {}", exc)


class AudioRecorder:
    """Captures microphone input as PCM audio chunks.

    Uses ``sounddevice`` input stream with a callback to deliver audio chunks
    to a registered handler.

    Args:
        sample_rate: Input sample rate in Hz.
        channels: Number of audio channels (1 = mono).
        block_size: Frames per input buffer.
        device: Input device index (None = system default).
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        block_size: int = INPUT_BLOCK_SIZE,
        device: int | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._block_size = block_size
        self._device = device
        self._stream: sd.RawInputStream | None = None
        self._running = False
        self._on_chunk: Callable[[bytes], None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether the recorder is actively capturing."""
        return self._running

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        """Start capturing audio from the microphone.

        Args:
            on_chunk: Callback receiving raw PCM bytes for each captured block.
                      Called from the audio thread — must be thread-safe.
        """
        if self._running:
            return

        self._on_chunk = on_chunk
        self._running = True

        self._stream = sd.RawInputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype=DEFAULT_DTYPE,
            blocksize=self._block_size,
            device=self._device,
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.debug("AudioRecorder started | rate={} blocksize={}", self._sample_rate, self._block_size)

    def stop(self) -> None:
        """Stop capturing audio."""
        if not self._running:
            return

        self._running = False

        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.debug("Stream close error (ignored): {}", exc)
            self._stream = None

        self._on_chunk = None
        logger.debug("AudioRecorder stopped")

    def _audio_callback(
        self,
        indata: bytes,
        frames: int,
        time_info: dict,  # type: ignore[type-arg]
        status: sd.CallbackFlags,
    ) -> None:
        """sounddevice callback: forward captured audio to the registered handler."""
        if status:
            logger.warning("Audio input status: {}", status)

        if self._on_chunk and self._running:
            # indata is already bytes in RawInputStream mode
            self._on_chunk(bytes(indata))


# ═══════════════════════════════════════════════════════════════════════════════
# PCM ENCODING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def pcm_to_base64(pcm_bytes: bytes) -> str:
    """Encode raw PCM bytes to base64 string for WebSocket transmission.

    Args:
        pcm_bytes: Raw 16-bit PCM audio bytes.

    Returns:
        Base64-encoded string.
    """
    return base64.b64encode(pcm_bytes).decode("ascii")


def base64_to_pcm(b64_string: str) -> bytes:
    """Decode a base64 string back to raw PCM bytes.

    Args:
        b64_string: Base64-encoded audio string from the server.

    Returns:
        Raw PCM audio bytes.
    """
    return base64.b64decode(b64_string)


def pcm_to_float32(pcm_bytes: bytes) -> np.ndarray:
    """Convert 16-bit PCM bytes to float32 numpy array (range -1.0 to 1.0).

    Args:
        pcm_bytes: Raw 16-bit PCM audio bytes.

    Returns:
        Normalized float32 numpy array.
    """
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    return samples.astype(np.float32) / 32768.0


def float32_to_pcm(audio: np.ndarray) -> bytes:
    """Convert float32 numpy array back to 16-bit PCM bytes.

    Args:
        audio: Float32 audio array (range -1.0 to 1.0).

    Returns:
        Raw 16-bit PCM bytes.
    """
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767).astype(np.int16).tobytes()


def compute_volume_rms(pcm_bytes: bytes) -> float:
    """Compute the RMS volume level of a PCM audio chunk.

    Args:
        pcm_bytes: Raw 16-bit PCM audio bytes.

    Returns:
        RMS value (0.0 = silence, higher = louder). Typical speech: 500-5000.
    """
    if len(pcm_bytes) < 2:
        return 0.0
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def is_voice_detected(pcm_bytes: bytes, threshold: float = VOLUME_RMS_THRESHOLD) -> bool:
    """Simple voice activity detection based on RMS volume.

    Args:
        pcm_bytes: Raw 16-bit PCM audio bytes.
        threshold: RMS threshold above which voice is considered present.

    Returns:
        True if the audio chunk likely contains voice.
    """
    return compute_volume_rms(pcm_bytes) > threshold
