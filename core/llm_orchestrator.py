"""Echo ConvAI — LLM Orchestrator.

Routes LLM requests to multiple providers with cascade failover.
Reverse-engineered from ElevenLabs' multi-LLM support (77 models).

ElevenLabs LLM features matched:
- 77 model support (GPT, Claude, Gemini, Grok, Qwen, custom)
- Default: gemini-2.5-flash
- Streaming token output (sentence-boundary chunking for TTS)
- Tool/function calling with blocking/async modes
- Cascade failover: primary → backup LLM (8s timeout default)
- Temperature, max_tokens, reasoning_effort controls
- Custom LLM via any OpenAI-compatible endpoint
- RAG context injection before LLM call
- Dynamic variable resolution in system prompts
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

import aiohttp
from loguru import logger
from pydantic import BaseModel, Field

from core.conversation_engine import LLMInterface


# ═══════════════════════════════════════════════════════════════════════════════
# LLM PROVIDER REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

class LLMProviderConfig(BaseModel):
    """Configuration for an LLM provider."""
    name: str
    base_url: str
    api_key_env: str = ""  # Environment variable name for API key
    api_key: str = ""      # Direct API key (from vault)
    model_prefix: str = "" # Model ID prefix for routing
    headers: dict[str, str] = Field(default_factory=dict)
    supports_streaming: bool = True
    supports_tools: bool = True
    max_retries: int = 2
    timeout_seconds: float = 30.0


# Provider configurations matching ElevenLabs' supported models
PROVIDERS: dict[str, LLMProviderConfig] = {
    "openai": LLMProviderConfig(
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        model_prefix="gpt-",
        supports_streaming=True,
        supports_tools=True,
    ),
    "anthropic": LLMProviderConfig(
        name="Anthropic",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        model_prefix="claude-",
        headers={"anthropic-version": "2023-06-01"},
        supports_streaming=True,
        supports_tools=True,
    ),
    "google": LLMProviderConfig(
        name="Google",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env="GOOGLE_API_KEY",
        model_prefix="gemini-",
        supports_streaming=True,
        supports_tools=True,
    ),
    "xai": LLMProviderConfig(
        name="xAI",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        model_prefix="grok-",
        supports_streaming=True,
        supports_tools=True,
    ),
    "local": LLMProviderConfig(
        name="Local (BRAVO)",
        base_url="http://192.168.1.11:8080/v1",  # vLLM/Ollama on BRAVO
        model_prefix="local-",
        supports_streaming=True,
        supports_tools=False,
        timeout_seconds=60.0,
    ),
    "orchestrator": LLMProviderConfig(
        name="Echo AI Orchestrator (30 free models)",
        base_url="http://localhost:3032/v1",  # Local multi-AI orchestrator
        model_prefix="orch-",
        supports_streaming=True,
        supports_tools=True,
    ),
}


# Model → Provider routing table (subset of ElevenLabs' 77 models)
MODEL_ROUTING: dict[str, str] = {
    # OpenAI
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    "gpt-4.1": "openai",
    "gpt-4.1-mini": "openai",
    "gpt-4.1-nano": "openai",
    "gpt-5": "openai",
    "gpt-5-mini": "openai",
    # Anthropic
    "claude-3-5-sonnet": "anthropic",
    "claude-sonnet-4": "anthropic",
    "claude-sonnet-4-5": "anthropic",
    "claude-sonnet-4-6": "anthropic",
    "claude-haiku-4-5": "anthropic",
    # Google
    "gemini-2.5-flash": "google",
    "gemini-2.5-flash-lite": "google",
    "gemini-2.0-flash": "google",
    "gemini-1.5-flash": "google",
    "gemini-1.5-pro": "google",
    # xAI
    "grok-beta": "xai",
    # Local
    "qwen3-4b": "local",
    "qwen3-30b-a3b": "local",
    # Custom
    "custom-llm": "custom",
}


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL SCHEMA CONVERTER
# ═══════════════════════════════════════════════════════════════════════════════

class ToolSchemaConverter:
    """Convert ElevenLabs tool definitions to provider-specific format.

    ElevenLabs uses a unified tool schema. Each LLM provider has its own
    format for tool/function definitions.
    """

    @staticmethod
    def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert to OpenAI function calling format."""
        openai_tools = []
        for tool in tools:
            if tool.get("type") in ("webhook", "client"):
                fn = {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": _extract_parameters(tool),
                    }
                }
                openai_tools.append(fn)
            elif tool.get("type") == "system":
                # System tools (end_call, transfer_to_number, etc.)
                fn = {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": f"System tool: {tool['name']}",
                        "parameters": {"type": "object", "properties": {}},
                    }
                }
                openai_tools.append(fn)
        return openai_tools

    @staticmethod
    def to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert to Anthropic tool use format."""
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") in ("webhook", "client", "system"):
                t = {
                    "name": tool["name"],
                    "description": tool.get("description", f"Tool: {tool['name']}"),
                    "input_schema": _extract_parameters(tool),
                }
                anthropic_tools.append(t)
        return anthropic_tools


def _extract_parameters(tool: dict[str, Any]) -> dict[str, Any]:
    """Extract parameter schema from tool definition."""
    schema = tool.get("api_schema", {})
    req_body = schema.get("request_body", {})
    properties = req_body.get("properties", {})

    params: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    for name, prop in properties.items():
        params["properties"][name] = {
            "type": prop.get("type", "string"),
            "description": prop.get("description", ""),
        }
        if prop.get("required", False):
            params["required"].append(name)

    return params


# ═══════════════════════════════════════════════════════════════════════════════
# LLM ORCHESTRATOR — Multi-Provider Routing + Cascade
# ═══════════════════════════════════════════════════════════════════════════════

class EchoLLMOrchestrator(LLMInterface):
    """Routes LLM requests to the appropriate provider with cascade failover.

    Matches ElevenLabs' behavior:
    1. Route request to primary LLM provider based on model_id
    2. Stream tokens back in real-time
    3. If primary fails/times out, cascade to backup LLM
    4. Handle tool calling for all providers
    5. Manage conversation context window

    Cascade logic (from ElevenLabs config):
    - cascade_timeout_seconds: default 8.0
    - backup_llm_config.preference: "default" → uses gemini-2.5-flash
    """

    def __init__(self, api_keys: dict[str, str] | None = None):
        """Initialize with API keys (from vault)."""
        self.api_keys = api_keys or {}
        self.tool_converter = ToolSchemaConverter()
        self._sessions: dict[str, aiohttp.ClientSession] = {}

    async def _get_session(self, provider: str) -> aiohttp.ClientSession:
        """Get or create an aiohttp session for a provider."""
        if provider not in self._sessions:
            self._sessions[provider] = aiohttp.ClientSession()
        return self._sessions[provider]

    def _resolve_provider(self, model: str) -> tuple[str, LLMProviderConfig]:
        """Resolve model ID to provider config."""
        provider_name = MODEL_ROUTING.get(model)
        if provider_name and provider_name in PROVIDERS:
            return provider_name, PROVIDERS[provider_name]

        # Fallback: try prefix matching
        for pname, pconfig in PROVIDERS.items():
            if pconfig.model_prefix and model.startswith(pconfig.model_prefix):
                return pname, pconfig

        # Ultimate fallback: Google (ElevenLabs default)
        return "google", PROVIDERS["google"]

    def _get_api_key(self, provider_name: str, config: LLMProviderConfig) -> str:
        """Get API key for a provider."""
        # Check direct keys first
        if config.api_key:
            return config.api_key
        # Check provided keys
        if provider_name in self.api_keys:
            return self.api_keys[provider_name]
        # Check env var name
        if config.api_key_env in self.api_keys:
            return self.api_keys[config.api_key_env]
        return ""

    # ─── OPENAI-COMPATIBLE STREAMING ──────────────────────────────────────────

    async def _stream_openai(
        self,
        session: aiohttp.ClientSession,
        config: LLMProviderConfig,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream from OpenAI-compatible API (OpenAI, xAI, local, custom)."""
        url = f"{config.base_url}/chat/completions"

        # Build messages with system prompt
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        payload: dict[str, Any] = {
            "model": model,
            "messages": full_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        if tools and config.supports_tools:
            payload["tools"] = self.tool_converter.to_openai_tools(tools)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **config.headers,
        }

        timeout = aiohttp.ClientTimeout(total=config.timeout_seconds)

        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise RuntimeError(f"LLM API error {resp.status}: {error[:200]}")

            async for line in resp.content:
                line = line.decode("utf-8").strip()
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
                except json.JSONDecodeError:
                    continue

    # ─── ANTHROPIC STREAMING ──────────────────────────────────────────────────

    async def _stream_anthropic(
        self,
        session: aiohttp.ClientSession,
        config: LLMProviderConfig,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream from Anthropic Messages API."""
        url = f"{config.base_url}/messages"

        # Anthropic uses separate system parameter
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }

        if system_prompt:
            payload["system"] = system_prompt

        if tools and config.supports_tools:
            payload["tools"] = self.tool_converter.to_anthropic_tools(tools)

        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            **config.headers,
        }

        timeout = aiohttp.ClientTimeout(total=config.timeout_seconds)

        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise RuntimeError(f"Anthropic API error {resp.status}: {error[:200]}")

            async for line in resp.content:
                line = line.decode("utf-8").strip()
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                try:
                    event = json.loads(data)
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield delta.get("text", "")
                except json.JSONDecodeError:
                    continue

    # ─── GOOGLE GEMINI STREAMING ──────────────────────────────────────────────

    async def _stream_google(
        self,
        session: aiohttp.ClientSession,
        config: LLMProviderConfig,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream from Google Gemini API."""
        url = f"{config.base_url}/models/{model}:streamGenerateContent?key={api_key}"

        # Convert messages to Gemini format
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({
                "role": role,
                "parts": [{"text": msg["content"]}],
            })

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }

        if system_prompt:
            payload["systemInstruction"] = {
                "parts": [{"text": system_prompt}]
            }

        headers = {"Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=config.timeout_seconds)

        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise RuntimeError(f"Gemini API error {resp.status}: {error[:200]}")

            buffer = ""
            async for chunk in resp.content:
                buffer += chunk.decode("utf-8")
                # Gemini streams JSON array elements
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip().rstrip(",").strip("[]")
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            for part in parts:
                                text = part.get("text", "")
                                if text:
                                    yield text
                    except json.JSONDecodeError:
                        continue

    # ─── PUBLIC INTERFACE ─────────────────────────────────────────────────────

    async def generate_response(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.85,
        max_tokens: int = 400,
        model: str = "gemini-2.5-flash",
        cascade_timeout: float = 8.0,
        backup_model: str = "gemini-2.5-flash",
    ) -> AsyncIterator[str]:
        """Stream LLM response with cascade failover.

        Args:
            messages: Conversation history
            system_prompt: System prompt text
            tools: Available tools for function calling
            temperature: Sampling temperature
            max_tokens: Max response tokens
            model: Primary LLM model ID
            cascade_timeout: Seconds before cascading to backup
            backup_model: Backup LLM model ID

        Yields:
            str: Response tokens
        """
        # Try primary model
        try:
            async for token in asyncio.wait_for(
                self._generate_from_model(
                    model, messages, system_prompt, tools, temperature, max_tokens
                ),
                timeout=cascade_timeout + 30,  # Total timeout
            ):
                yield token
            return
        except (asyncio.TimeoutError, RuntimeError) as e:
            logger.warning(f"Primary LLM ({model}) failed: {e}. Cascading to {backup_model}")

        # Cascade to backup model
        try:
            async for token in self._generate_from_model(
                backup_model, messages, system_prompt, tools, temperature, max_tokens
            ):
                yield token
        except Exception as e:
            logger.error(f"Backup LLM ({backup_model}) also failed: {e}")
            yield "I'm sorry, I'm having trouble processing your request right now."

    async def _generate_from_model(
        self,
        model: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Generate from a specific model."""
        provider_name, config = self._resolve_provider(model)
        api_key = self._get_api_key(provider_name, config)
        session = await self._get_session(provider_name)

        if provider_name == "anthropic":
            async for token in self._stream_anthropic(
                session, config, api_key, model, messages, system_prompt,
                tools, temperature, max_tokens,
            ):
                yield token
        elif provider_name == "google":
            async for token in self._stream_google(
                session, config, api_key, model, messages, system_prompt,
                tools, temperature, max_tokens,
            ):
                yield token
        else:
            # OpenAI-compatible (OpenAI, xAI, local, custom)
            async for token in self._stream_openai(
                session, config, api_key, model, messages, system_prompt,
                tools, temperature, max_tokens,
            ):
                yield token

    async def generate_tool_call(
        self,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        model: str = "gemini-2.5-flash",
    ) -> dict[str, Any] | None:
        """Check if LLM wants to call a tool (non-streaming).

        Returns tool call dict or None if LLM wants to respond with text.
        """
        provider_name, config = self._resolve_provider(model)
        api_key = self._get_api_key(provider_name, config)
        session = await self._get_session(provider_name)

        if provider_name in ("openai", "xai", "local", "orchestrator"):
            return await self._tool_call_openai(
                session, config, api_key, model, messages, system_prompt, tools,
            )
        elif provider_name == "anthropic":
            return await self._tool_call_anthropic(
                session, config, api_key, model, messages, system_prompt, tools,
            )

        return None

    async def _tool_call_openai(
        self,
        session: aiohttp.ClientSession,
        config: LLMProviderConfig,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Check for tool calls via OpenAI-compatible API."""
        url = f"{config.base_url}/chat/completions"

        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        payload = {
            "model": model,
            "messages": full_messages,
            "tools": self.tool_converter.to_openai_tools(tools),
            "tool_choice": "auto",
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **config.headers,
        }

        timeout = aiohttp.ClientTimeout(total=config.timeout_seconds)

        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                return None

            data = await resp.json()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            tool_calls = message.get("tool_calls", [])

            if tool_calls:
                tc = tool_calls[0]
                return {
                    "tool_call_id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": json.loads(tc.get("function", {}).get("arguments", "{}")),
                }

        return None

    async def _tool_call_anthropic(
        self,
        session: aiohttp.ClientSession,
        config: LLMProviderConfig,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Check for tool calls via Anthropic API."""
        url = f"{config.base_url}/messages"

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": 1024,
            "tools": self.tool_converter.to_anthropic_tools(tools),
        }
        if system_prompt:
            payload["system"] = system_prompt

        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            **config.headers,
        }

        timeout = aiohttp.ClientTimeout(total=config.timeout_seconds)

        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                return None

            data = await resp.json()
            for block in data.get("content", []):
                if block.get("type") == "tool_use":
                    return {
                        "tool_call_id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": block.get("input", {}),
                    }

        return None

    async def close(self) -> None:
        """Close all HTTP sessions."""
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()
