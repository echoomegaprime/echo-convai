"""Echo ConvAI — WebSocket Server.

Drop-in compatible WebSocket server that speaks the ElevenLabs ConvAI protocol.
Any ElevenLabs SDK (@elevenlabs/react, Python SDK, etc.) can connect to this server
by simply changing the WebSocket URL.

Reverse-engineered from:
- ElevenLabs OpenAPI spec (220 endpoints, 1039 schemas)
- ElevenLabs JS/Python SDK WebSocket client source code
- LiveKit WebRTC token structure (JWT claims)
- AsyncAPI 2.6.0 protocol definition
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel, Field

from protocols.websocket_protocol import (
    ConversationInitiation,
    WebSocketCloseCode,
    generate_conversation_id,
    generate_signed_url,
)
from core.conversation_engine import (
    ConversationEngine,
    ConversationState,
    AgentDefinition,
)


# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Echo ConvAI Server",
    description="ElevenLabs-compatible Conversational AI WebSocket Server",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory stores (production: D1 database)
AGENTS: dict[str, AgentDefinition] = {}
ACTIVE_CONVERSATIONS: dict[str, ConversationEngine] = {}
CONVERSATION_HISTORY: dict[str, list[dict[str, Any]]] = {}

# Signing secret for private agents
SIGNING_SECRET = "echo-convai-signing-secret-change-me"


# ═══════════════════════════════════════════════════════════════════════════════
# REST API — Agent CRUD (ElevenLabs compatible)
# ═══════════════════════════════════════════════════════════════════════════════

class CreateAgentRequest(BaseModel):
    """Matches ElevenLabs POST /v1/convai/agents/create schema."""
    name: str
    conversation_config: dict[str, Any] = Field(default_factory=dict)
    platform_settings: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentSummary(BaseModel):
    """Agent list item."""
    agent_id: str
    name: str
    language: str = "en"
    is_public: bool = True


@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Health endpoint."""
    return {
        "status": "healthy",
        "service": "echo-convai",
        "version": "1.0.0",
        "active_conversations": len(ACTIVE_CONVERSATIONS),
        "registered_agents": len(AGENTS),
        "uptime_seconds": int(time.time() - _start_time),
    }


@app.post("/v1/convai/agents/create")
async def create_agent(req: CreateAgentRequest) -> dict[str, Any]:
    """Create a new conversational AI agent."""
    agent_id = f"agent_{uuid.uuid4().hex[:24]}"

    # Extract config
    agent_cfg = req.conversation_config.get("agent", {})
    tts_cfg = req.conversation_config.get("tts", {})
    turn_cfg = req.conversation_config.get("turn", {})
    conv_cfg = req.conversation_config.get("conversation", {})
    prompt_cfg = agent_cfg.get("prompt", {})

    agent = AgentDefinition(
        agent_id=agent_id,
        name=req.name,
        system_prompt=prompt_cfg.get("prompt", ""),
        first_message=agent_cfg.get("first_message"),
        language=agent_cfg.get("language", "en"),
        voice_id=tts_cfg.get("voice_id", ""),
        tts_model=tts_cfg.get("model_id", "eleven_v3_conversational"),
        llm_model=prompt_cfg.get("llm", "gemini-2.5-flash"),
        llm_temperature=prompt_cfg.get("temperature", 0.85),
        llm_max_tokens=prompt_cfg.get("max_tokens", 400),
        tools=agent_cfg.get("tools", []),
        knowledge_base_ids=[],
        turn_timeout=turn_cfg.get("turn_timeout", 15.0),
        turn_eagerness=turn_cfg.get("turn_eagerness", "normal"),
        max_duration_seconds=conv_cfg.get("max_duration_seconds", 1800),
        is_public=req.platform_settings.get("auth", {}).get("type", "public") == "public",
    )

    AGENTS[agent_id] = agent
    logger.info(f"Agent created: {agent_id} ({req.name})")

    return {
        "agent_id": agent_id,
        "name": req.name,
        "conversation_config": req.conversation_config,
        "platform_settings": req.platform_settings,
    }


@app.get("/v1/convai/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict[str, Any]:
    """Get agent details."""
    agent = AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent.model_dump()


@app.get("/v1/convai/agents/summaries")
async def list_agents() -> dict[str, Any]:
    """List all agents."""
    return {
        "agents": [
            AgentSummary(
                agent_id=a.agent_id,
                name=a.name,
                language=a.language,
                is_public=a.is_public,
            ).model_dump()
            for a in AGENTS.values()
        ]
    }


@app.patch("/v1/convai/agents/{agent_id}")
async def update_agent(agent_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Update agent configuration."""
    agent = AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Apply updates
    for key, value in updates.items():
        if hasattr(agent, key):
            setattr(agent, key, value)

    return agent.model_dump()


@app.delete("/v1/convai/agents/{agent_id}")
async def delete_agent(agent_id: str) -> dict[str, str]:
    """Delete an agent."""
    if agent_id not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    del AGENTS[agent_id]
    return {"status": "deleted"}


@app.post("/v1/convai/agents/{agent_id}/duplicate")
async def duplicate_agent(agent_id: str) -> dict[str, Any]:
    """Duplicate an agent."""
    agent = AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    new_id = f"agent_{uuid.uuid4().hex[:24]}"
    new_agent = agent.model_copy(update={"agent_id": new_id, "name": f"{agent.name} (copy)"})
    AGENTS[new_id] = new_agent
    return new_agent.model_dump()


# ═══════════════════════════════════════════════════════════════════════════════
# CONVERSATION ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/v1/convai/conversations")
async def list_conversations(agent_id: str | None = None) -> dict[str, Any]:
    """List conversations, optionally filtered by agent."""
    convos = []
    for conv_id, engine in ACTIVE_CONVERSATIONS.items():
        if agent_id and engine.state and engine.state.agent_id != agent_id:
            continue
        convos.append({
            "conversation_id": conv_id,
            "agent_id": engine.state.agent_id if engine.state else "",
            "status": "active" if engine.state and engine.state.is_active else "ended",
            "started_at": engine.state.started_at if engine.state else 0,
        })
    return {"conversations": convos}


@app.get("/v1/convai/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    """Get conversation details."""
    engine = ACTIVE_CONVERSATIONS.get(conversation_id)
    if not engine or not engine.state:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {
        "conversation_id": conversation_id,
        "agent_id": engine.state.agent_id,
        "status": "active" if engine.state.is_active else "ended",
        "messages": engine.state.messages,
        "started_at": engine.state.started_at,
        "total_user_messages": engine.state.total_user_messages,
        "total_audio_chunks_in": engine.state.total_audio_chunks_in,
        "total_audio_chunks_out": engine.state.total_audio_chunks_out,
    }


@app.delete("/v1/convai/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict[str, str]:
    """End and delete a conversation."""
    engine = ACTIVE_CONVERSATIONS.get(conversation_id)
    if engine:
        await engine.stop()
        del ACTIVE_CONVERSATIONS[conversation_id]
    return {"status": "deleted"}


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/v1/convai/conversation/get-signed-url")
async def get_signed_url(
    agent_id: str = Query(...),
    xi_api_key: str = Header(None, alias="xi-api-key"),
) -> dict[str, str]:
    """Generate signed WebSocket URL for private agents."""
    agent = AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not agent.is_public and not xi_api_key:
        raise HTTPException(status_code=401, detail="API key required for private agents")

    signed_url = generate_signed_url(agent_id, SIGNING_SECRET)
    return {"signed_url": signed_url}


@app.get("/v1/convai/conversation/token")
async def get_conversation_token(
    agent_id: str = Query(...),
    xi_api_key: str = Header(None, alias="xi-api-key"),
) -> dict[str, str]:
    """Generate WebRTC conversation token (LiveKit-compatible JWT)."""
    agent = AGENTS.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # In production, generate a proper LiveKit JWT
    # For now, return a placeholder token
    conv_id = generate_conversation_id()
    token = f"echo_token_{uuid.uuid4().hex}"

    return {
        "token": token,
        "conversation_id": conv_id,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET ENDPOINT — THE CORE
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/v1/convai/conversation")
async def websocket_conversation(
    websocket: WebSocket,
    agent_id: str = Query(...),
    conversation_signature: str | None = Query(None),
) -> None:
    """Main WebSocket endpoint for real-time conversation.

    Protocol: ElevenLabs ConvAI WebSocket (AsyncAPI 2.6.0 compatible)
    Audio: Base64-encoded PCM (16kHz 16-bit mono default)

    Connection flow:
    1. Client connects with agent_id (+ optional signature for private agents)
    2. Client sends conversation_initiation_client_data with config overrides
    3. Server responds with conversation_initiation_metadata
    4. Bidirectional audio + text exchange begins
    5. Server manages turn-taking, LLM routing, TTS streaming
    """
    # Validate agent
    agent = AGENTS.get(agent_id)
    if not agent:
        await websocket.close(code=1008, reason="Agent not found")
        return

    # Validate signature for private agents
    if not agent.is_public and not conversation_signature:
        await websocket.close(code=1008, reason="Signature required for private agents")
        return

    await websocket.accept()
    logger.info(f"WebSocket connected: agent={agent_id}")

    # Create send function
    async def send_message(msg: dict[str, Any]) -> None:
        try:
            await websocket.send_json(msg)
        except Exception as e:
            logger.error(f"Send error: {e}")

    # Create conversation engine (with stub interfaces for now)
    # In production, these would be real ASR/LLM/TTS/TurnTaking implementations
    from core.conversation_engine import ASRInterface, LLMInterface, TTSInterface, TurnTakingInterface

    engine = ConversationEngine(
        asr=ASRInterface(),
        llm=LLMInterface(),
        tts=TTSInterface(),
        turn_taking=TurnTakingInterface(),
        send_message=send_message,
    )

    conversation_id = None

    try:
        # Wait for first message (should be conversation_initiation_client_data)
        first_raw = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)

        initiation = None
        if first_raw.get("type") == "conversation_initiation_client_data":
            initiation = ConversationInitiation(**first_raw)

        # Start conversation
        conversation_id = await engine.start(agent, initiation)
        ACTIVE_CONVERSATIONS[conversation_id] = engine

        # Main message loop
        while engine.state and engine.state.is_active:
            try:
                raw = await asyncio.wait_for(websocket.receive_json(), timeout=60.0)
                await engine.handle_message(raw)
            except asyncio.TimeoutError:
                # 60s no input → transport_receive_timeout
                logger.info(f"Receive timeout: {conversation_id}")
                await websocket.close(
                    code=WebSocketCloseCode.PROTOCOL_ERROR,
                    reason="transport_receive_timeout",
                )
                break

    except WebSocketDisconnect as e:
        logger.info(f"WebSocket disconnected: {conversation_id} code={e.code}")
    except asyncio.TimeoutError:
        logger.warning(f"Initial message timeout for agent {agent_id}")
        await websocket.close(code=1008, reason="initiation_timeout")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.close(code=1011, reason="internal_error")
        except Exception:
            pass
    finally:
        if engine:
            await engine.stop()
        if conversation_id and conversation_id in ACTIVE_CONVERSATIONS:
            del ACTIVE_CONVERSATIONS[conversation_id]
        logger.info(f"Conversation cleanup: {conversation_id}")


# ═══════════════════════════════════════════════════════════════════════════════
# TTS PROXY ENDPOINTS (ElevenLabs TTS API compatible)
# ═══════════════════════════════════════════════════════════════════════════════

class TTSRequest(BaseModel):
    """Matches ElevenLabs POST /v1/text-to-speech/{voice_id} schema."""
    text: str
    model_id: str = "eleven_multilingual_v2"
    language_code: str | None = None
    voice_settings: dict[str, Any] | None = None
    pronunciation_dictionary_locators: list[dict[str, str]] = Field(default_factory=list)
    seed: int | None = None
    previous_text: str | None = None
    next_text: str | None = None
    previous_request_ids: list[str] = Field(default_factory=list)
    next_request_ids: list[str] = Field(default_factory=list)
    apply_text_normalization: str = "auto"


class DialogueInput(BaseModel):
    """Single dialogue input entry."""
    text: str
    voice_id: str


class DialogueRequest(BaseModel):
    """Matches ElevenLabs POST /v1/text-to-dialogue schema."""
    inputs: list[DialogueInput]
    model_id: str = "eleven_v3"
    settings: dict[str, Any] | None = None
    seed: int | None = None
    language_code: str | None = None
    apply_text_normalization: str = "auto"


@app.post("/v1/text-to-speech/{voice_id}")
async def text_to_speech(voice_id: str, req: TTSRequest) -> dict[str, str]:
    """TTS endpoint — generates speech from text.

    In production, this routes to the local GPU TTS model on BRAVO.
    Returns audio in the requested format.
    """
    # TODO: Route to actual TTS inference on BRAVO (RTX 4060)
    return {
        "status": "tts_endpoint_ready",
        "voice_id": voice_id,
        "model_id": req.model_id,
        "text_length": len(req.text),
        "message": "Wire to BRAVO GPU inference: faster-whisper + F5-TTS",
    }


@app.post("/v1/text-to-speech/{voice_id}/stream")
async def text_to_speech_stream(voice_id: str, req: TTSRequest) -> dict[str, str]:
    """Streaming TTS endpoint."""
    return {
        "status": "streaming_tts_endpoint_ready",
        "voice_id": voice_id,
        "model_id": req.model_id,
    }


@app.post("/v1/text-to-dialogue")
async def text_to_dialogue(req: DialogueRequest) -> dict[str, str]:
    """Multi-speaker dialogue synthesis (v3 exclusive)."""
    unique_voices = len(set(inp.voice_id for inp in req.inputs))
    return {
        "status": "dialogue_endpoint_ready",
        "speakers": unique_voices,
        "segments": len(req.inputs),
        "model_id": req.model_id,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

_start_time = time.time()


@app.on_event("startup")
async def startup() -> None:
    """Server startup."""
    logger.info("Echo ConvAI Server starting...")
    logger.info(f"Endpoints: {len(app.routes)} routes")

    # Create a default demo agent
    demo_agent = AgentDefinition(
        agent_id="agent_echo_demo_001",
        name="Echo Demo Agent",
        system_prompt="You are Echo, a helpful AI assistant built by Echo Prime Technologies. Be concise and friendly.",
        first_message="Hello! I'm Echo, your AI assistant. How can I help you today?",
        language="en",
        voice_id="keDMh3sQlEXKM4EQxvvi",  # Echo voice
        tts_model="eleven_v3_conversational",
        llm_model="claude-sonnet-4-6",
        is_public=True,
    )
    AGENTS[demo_agent.agent_id] = demo_agent
    logger.info(f"Demo agent registered: {demo_agent.agent_id}")


def run_server(host: str = "0.0.0.0", port: int = 8900) -> None:
    """Run the ConvAI server."""
    uvicorn.run(
        "core.websocket_server:app",
        host=host,
        port=port,
        log_level="info",
        ws_max_size=16 * 1024 * 1024,  # 16MB max WebSocket message
    )


if __name__ == "__main__":
    run_server()
