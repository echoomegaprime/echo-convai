"""Echo ConvAI SDK — CLI demo for real-time voice conversation.

Records microphone input, streams to the ConvAI server, and plays back
agent audio responses. Displays transcripts in the terminal.

Usage::

    python -m echo_convai.demo --agent-id agent_xxx --server-url wss://echo-convai.example.com --api-key your-key

Press Ctrl+C to end the conversation.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from loguru import logger

from echo_convai.audio import AudioPlayer, AudioRecorder
from echo_convai.conversation import Conversation
from echo_convai.types import AgentChatResponsePartType


async def run_demo(agent_id: str, server_url: str, api_key: str | None, text_only: bool) -> None:
    """Run the interactive voice/text demo."""
    player: AudioPlayer | None = None
    recorder: AudioRecorder | None = None

    if not text_only:
        player = AudioPlayer()
        recorder = AudioRecorder()

    conv = Conversation(
        agent_id=agent_id,
        server_url=server_url,
        api_key=api_key,
        text_only=text_only,
    )

    # Register callbacks
    def on_transcript(text: str) -> None:
        print(f"\n  [YOU] {text}")

    def on_agent_response(text: str) -> None:
        print(f"\n  [AGENT] {text}")

    def on_audio(audio_bytes: bytes) -> None:
        if player:
            player.play(audio_bytes)

    def on_interruption() -> None:
        if player:
            player.interrupt()
        print("\n  [---] (interrupted)")

    def on_error(code: str, message: str) -> None:
        print(f"\n  [ERROR] {code}: {message}", file=sys.stderr)

    def on_latency(ms: int) -> None:
        logger.debug("Latency: {}ms", ms)

    conv.on_transcript(on_transcript)
    conv.on_agent_response(on_agent_response)
    conv.on_audio(on_audio)
    conv.on_interruption(on_interruption)
    conv.on_error(on_error)
    conv.on_latency(on_latency)

    print("=" * 60)
    print("  Echo ConvAI Demo")
    print(f"  Agent: {agent_id}")
    print(f"  Mode:  {'text-only' if text_only else 'voice'}")
    print("  Press Ctrl+C to end")
    print("=" * 60)

    try:
        async with conv:
            print(f"\n  Connected! conversation_id={conv.conversation_id or '(pending)'}\n")

            if not text_only and player and recorder:
                # Voice mode: stream mic audio to server
                player.start()

                def on_mic_chunk(chunk: bytes) -> None:
                    asyncio.run_coroutine_threadsafe(conv.send_audio(chunk), loop)

                loop = asyncio.get_running_loop()
                recorder.start(on_chunk=on_mic_chunk)

                print("  Listening... speak into your microphone.\n")
                await conv.wait_until_done()
            else:
                # Text mode: read from stdin
                print("  Type your messages below. Empty line to send.\n")
                loop = asyncio.get_running_loop()
                while conv.is_connected:
                    try:
                        line = await loop.run_in_executor(None, input, "  > ")
                        if line.strip():
                            await conv.send_message(line.strip())
                    except EOFError:
                        break

    except KeyboardInterrupt:
        print("\n\n  Ending conversation...")
    finally:
        if recorder and recorder.is_running:
            recorder.stop()
        if player and player.is_running:
            player.stop()

    print(f"\n  Session ended. conversation_id={conv.conversation_id}")
    print("=" * 60)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Echo ConvAI Demo — Real-time voice conversation",
        prog="echo-convai-demo",
    )
    parser.add_argument("--agent-id", required=True, help="Agent ID to converse with")
    parser.add_argument(
        "--server-url",
        default="wss://api.elevenlabs.io",
        help="WebSocket server URL (default: wss://api.elevenlabs.io)",
    )
    parser.add_argument("--api-key", default=None, help="API key for authentication")
    parser.add_argument("--text-only", action="store_true", help="Use text-only mode (no audio)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # Configure logging
    logger.remove()
    if args.debug:
        logger.add(sys.stderr, level="DEBUG", format="{time:HH:mm:ss} | {level:<7} | {message}")
    else:
        logger.add(sys.stderr, level="WARNING", format="{level:<7} | {message}")

    try:
        asyncio.run(run_demo(args.agent_id, args.server_url, args.api_key, args.text_only))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
