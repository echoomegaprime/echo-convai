"""Echo ConvAI Python SDK — Real-time conversational AI over WebSocket.

Provides a complete client for the Echo ConvAI platform including REST API
management, WebSocket conversation handling, and audio I/O utilities.

Usage::

    from echo_convai import EchoConvAIClient, Conversation

    # REST API
    client = EchoConvAIClient(api_key="your-key", base_url="https://echo-convai.example.com")
    agents = await client.list_agents()

    # WebSocket conversation
    async with Conversation(agent_id="agent_xxx", server_url="wss://...") as conv:
        conv.on_transcript(lambda t: print(f"User: {t}"))
        conv.on_agent_response(lambda r: print(f"Agent: {r}"))
        await conv.send_message("Hello!")
"""

__version__ = "0.1.0"
__all__ = [
    "__version__",
    "EchoConvAIClient",
    "Conversation",
    "AudioPlayer",
    "AudioRecorder",
    "types",
]

from echo_convai.audio import AudioPlayer, AudioRecorder
from echo_convai.client import EchoConvAIClient
from echo_convai.conversation import Conversation
