"""Echo ConvAI SDK — REST API client for agent management and conversation history.

Provides both async and sync interfaces for the Echo ConvAI HTTP API.
All endpoints authenticate via the ``xi-api-key`` header.

Usage::

    from echo_convai import EchoConvAIClient

    # Async
    async with EchoConvAIClient(api_key="key", base_url="https://echo-convai.example.com") as client:
        agent = await client.create_agent(name="Support Bot", prompt="You are helpful.")
        agents = await client.list_agents()

    # Sync wrapper
    client = EchoConvAIClient(api_key="key", base_url="https://echo-convai.example.com")
    agent = client.create_agent_sync(name="Support Bot", prompt="You are helpful.")
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

from echo_convai.types import (
    AgentDetail,
    AgentListResponse,
    AgentSummary,
    ConversationHistoryItem,
    ConversationListResponse,
    CreateAgentRequest,
    SignedUrlResponse,
    UpdateAgentRequest,
)

DEFAULT_TIMEOUT = 30.0
SDK_USER_AGENT = "echo-convai-python/0.1.0"


class EchoConvAIClient:
    """HTTP client for the Echo ConvAI REST API.

    Manages agents (CRUD), signed URLs, and conversation history retrieval.
    Uses httpx for both async and sync operations.

    Args:
        api_key: API key sent as ``xi-api-key`` header.
        base_url: Base HTTP URL of the ConvAI server (no trailing slash).
        timeout: Default request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.elevenlabs.io",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._async_client: httpx.AsyncClient | None = None
        self._sync_client: httpx.Client | None = None
        logger.debug("EchoConvAIClient initialized | base_url={}", self._base_url)

    # ───────────────────────────────────────────────────────────────────────
    # HTTP client lifecycle
    # ───────────────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        """Build common request headers."""
        return {
            "xi-api-key": self._api_key,
            "User-Agent": SDK_USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get_async_client(self) -> httpx.AsyncClient:
        """Lazy-create the async httpx client."""
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers(),
                timeout=self._timeout,
            )
        return self._async_client

    def _get_sync_client(self) -> httpx.Client:
        """Lazy-create the sync httpx client."""
        if self._sync_client is None or self._sync_client.is_closed:
            self._sync_client = httpx.Client(
                base_url=self._base_url,
                headers=self._headers(),
                timeout=self._timeout,
            )
        return self._sync_client

    async def aclose(self) -> None:
        """Close the async HTTP client."""
        if self._async_client and not self._async_client.is_closed:
            await self._async_client.aclose()
            logger.debug("Async HTTP client closed")

    def close(self) -> None:
        """Close the sync HTTP client."""
        if self._sync_client and not self._sync_client.is_closed:
            self._sync_client.close()
            logger.debug("Sync HTTP client closed")

    async def __aenter__(self) -> EchoConvAIClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    def __enter__(self) -> EchoConvAIClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ───────────────────────────────────────────────────────────────────────
    # Internal request helpers
    # ───────────────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an async HTTP request and return the JSON response body."""
        client = self._get_async_client()
        url = path if path.startswith("http") else path
        logger.debug("{} {} | params={}", method.upper(), url, params)
        response = await client.request(method, url, json=json, params=params)
        response.raise_for_status()
        if response.status_code == 204:
            return {}
        return response.json()  # type: ignore[no-any-return]

    def _request_sync(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a sync HTTP request and return the JSON response body."""
        client = self._get_sync_client()
        url = path if path.startswith("http") else path
        logger.debug("{} {} (sync) | params={}", method.upper(), url, params)
        response = client.request(method, url, json=json, params=params)
        response.raise_for_status()
        if response.status_code == 204:
            return {}
        return response.json()  # type: ignore[no-any-return]

    # ───────────────────────────────────────────────────────────────────────
    # AGENT CRUD — Async
    # ───────────────────────────────────────────────────────────────────────

    async def create_agent(
        self,
        name: str,
        *,
        prompt: str = "",
        llm: str = "gemini-2.5-flash",
        first_message: str | None = None,
        voice_id: str | None = None,
        language: str = "en",
        tags: list[str] | None = None,
        conversation_config: dict[str, Any] | None = None,
        platform_settings: dict[str, Any] | None = None,
    ) -> AgentDetail:
        """Create a new conversational agent.

        Args:
            name: Display name for the agent.
            prompt: System prompt / persona instructions.
            llm: LLM model identifier.
            first_message: Optional greeting sent when conversation starts.
            voice_id: TTS voice ID for audio responses.
            language: ISO language code.
            tags: Optional tags for organization.
            conversation_config: Full conversation config override (advanced).
            platform_settings: Platform settings override (advanced).

        Returns:
            Full agent detail with generated agent_id.
        """
        config = conversation_config or {}
        if "agent" not in config:
            agent_block: dict[str, Any] = {
                "prompt": {"prompt": prompt, "llm": llm},
                "language": language,
            }
            if first_message:
                agent_block["first_message"] = first_message
            config["agent"] = agent_block
        if voice_id and "tts" not in config:
            config["tts"] = {"voice_id": voice_id}

        body = CreateAgentRequest(
            name=name,
            conversation_config=config,
            platform_settings=platform_settings or {},
            tags=tags or [],
        )
        data = await self._request("POST", "/v1/convai/agents/create", json=body.model_dump(exclude_none=True))
        logger.info("Agent created | agent_id={} name={}", data.get("agent_id"), name)
        return AgentDetail.model_validate(data)

    async def get_agent(self, agent_id: str) -> AgentDetail:
        """Retrieve full details for an agent by ID."""
        data = await self._request("GET", f"/v1/convai/agents/{agent_id}")
        return AgentDetail.model_validate(data)

    async def update_agent(self, agent_id: str, **kwargs: Any) -> AgentDetail:
        """Partially update an agent's configuration.

        Args:
            agent_id: Target agent ID.
            **kwargs: Fields to update (name, conversation_config, platform_settings, tags, metadata).

        Returns:
            Updated agent detail.
        """
        body = UpdateAgentRequest(**kwargs)
        data = await self._request(
            "PATCH",
            f"/v1/convai/agents/{agent_id}",
            json=body.model_dump(exclude_none=True),
        )
        logger.info("Agent updated | agent_id={}", agent_id)
        return AgentDetail.model_validate(data)

    async def delete_agent(self, agent_id: str) -> None:
        """Delete an agent by ID."""
        await self._request("DELETE", f"/v1/convai/agents/{agent_id}")
        logger.info("Agent deleted | agent_id={}", agent_id)

    async def list_agents(
        self,
        *,
        cursor: str | None = None,
        page_size: int = 30,
        search: str | None = None,
    ) -> AgentListResponse:
        """List agents with optional pagination and search.

        Args:
            cursor: Pagination cursor from a previous response.
            page_size: Number of agents per page (max 100).
            search: Search query to filter by agent name.

        Returns:
            Paginated agent list.
        """
        params: dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        if search:
            params["search"] = search
        data = await self._request("GET", "/v1/convai/agents", params=params)
        return AgentListResponse.model_validate(data)

    # ───────────────────────────────────────────────────────────────────────
    # SIGNED URL
    # ───────────────────────────────────────────────────────────────────────

    async def get_signed_url(self, agent_id: str) -> str:
        """Get a pre-authenticated WebSocket URL for a private agent.

        Args:
            agent_id: Agent to generate URL for.

        Returns:
            Signed WSS URL string ready for WebSocket connection.
        """
        data = await self._request(
            "GET",
            "/v1/convai/conversation/get-signed-url",
            params={"agent_id": agent_id},
        )
        resp = SignedUrlResponse.model_validate(data)
        logger.debug("Signed URL obtained for agent_id={}", agent_id)
        return resp.signed_url

    # ───────────────────────────────────────────────────────────────────────
    # CONVERSATIONS
    # ───────────────────────────────────────────────────────────────────────

    async def list_conversations(
        self,
        *,
        agent_id: str | None = None,
        cursor: str | None = None,
        page_size: int = 30,
        call_successful: str | None = None,
    ) -> ConversationListResponse:
        """List conversation history with filters.

        Args:
            agent_id: Filter by agent ID.
            cursor: Pagination cursor.
            page_size: Results per page.
            call_successful: Filter by call success status.

        Returns:
            Paginated conversation list.
        """
        params: dict[str, Any] = {"page_size": page_size}
        if agent_id:
            params["agent_id"] = agent_id
        if cursor:
            params["cursor"] = cursor
        if call_successful:
            params["call_successful"] = call_successful
        data = await self._request("GET", "/v1/convai/conversations", params=params)
        return ConversationListResponse.model_validate(data)

    async def get_conversation(self, conversation_id: str) -> ConversationHistoryItem:
        """Get full details for a specific conversation.

        Args:
            conversation_id: Target conversation ID.

        Returns:
            Conversation detail including transcript and analysis.
        """
        data = await self._request("GET", f"/v1/convai/conversations/{conversation_id}")
        return ConversationHistoryItem.model_validate(data)

    async def get_conversation_audio(self, conversation_id: str) -> bytes:
        """Download the audio recording for a conversation.

        Args:
            conversation_id: Target conversation ID.

        Returns:
            Raw audio bytes.
        """
        client = self._get_async_client()
        response = await client.get(f"/v1/convai/conversations/{conversation_id}/audio")
        response.raise_for_status()
        return response.content

    # ───────────────────────────────────────────────────────────────────────
    # SYNC WRAPPERS — run async methods from sync code
    # ───────────────────────────────────────────────────────────────────────

    def _run_sync(self, coro: Any) -> Any:
        """Run an async coroutine synchronously."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return asyncio.run(coro)

    def create_agent_sync(self, name: str, **kwargs: Any) -> AgentDetail:
        """Sync wrapper for :meth:`create_agent`."""
        return self._run_sync(self.create_agent(name, **kwargs))

    def get_agent_sync(self, agent_id: str) -> AgentDetail:
        """Sync wrapper for :meth:`get_agent`."""
        return self._run_sync(self.get_agent(agent_id))

    def update_agent_sync(self, agent_id: str, **kwargs: Any) -> AgentDetail:
        """Sync wrapper for :meth:`update_agent`."""
        return self._run_sync(self.update_agent(agent_id, **kwargs))

    def delete_agent_sync(self, agent_id: str) -> None:
        """Sync wrapper for :meth:`delete_agent`."""
        self._run_sync(self.delete_agent(agent_id))

    def list_agents_sync(self, **kwargs: Any) -> AgentListResponse:
        """Sync wrapper for :meth:`list_agents`."""
        return self._run_sync(self.list_agents(**kwargs))

    def get_signed_url_sync(self, agent_id: str) -> str:
        """Sync wrapper for :meth:`get_signed_url`."""
        return self._run_sync(self.get_signed_url(agent_id))

    def list_conversations_sync(self, **kwargs: Any) -> ConversationListResponse:
        """Sync wrapper for :meth:`list_conversations`."""
        return self._run_sync(self.list_conversations(**kwargs))

    def get_conversation_sync(self, conversation_id: str) -> ConversationHistoryItem:
        """Sync wrapper for :meth:`get_conversation`."""
        return self._run_sync(self.get_conversation(conversation_id))
