from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from .contracts import Contracts

MAX_TRANSIENT_RETRIES = 1
RETRY_DELAY_SECONDS = 0.2
TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _is_transient_error(error: Exception) -> bool:
    if isinstance(error, httpx2.HTTPStatusError):
        return error.response.status_code in TRANSIENT_HTTP_STATUSES
    if isinstance(error, MCPError):
        return error.code == -32603 or error.code in TRANSIENT_HTTP_STATUSES
    return isinstance(error, (httpx2.TransportError, ConnectionError, TimeoutError))


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._catalog: dict[str, dict[str, Any]] | None = None
        self._cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def list_tools(self) -> list[str]:
        return sorted(await self.tool_catalog())

    async def tool_catalog(self) -> dict[str, dict[str, Any]]:
        """Return the discovered MCP tool names, descriptions and input schemas."""
        if self._catalog is None:
            response = await self._session.list_tools()
            catalog: dict[str, dict[str, Any]] = {}
            for tool in response.tools:
                if tool.name in catalog:
                    raise ValueError(f"MCP returned duplicate tool name: {tool.name}")
                catalog[tool.name] = {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": copy.deepcopy(tool.input_schema),
                }
            self._catalog = catalog
        return copy.deepcopy(self._catalog)

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("MCP case_id must be a non-empty string")
        try:
            canonical = json.dumps(
                arguments,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("MCP arguments must be JSON serializable") from exc
        cache_key = (case_id, tool_name, canonical)
        if cache_key in self._cache:
            return copy.deepcopy(self._cache[cache_key])

        # Construct the payload from the canonical JSON so mutable caller arguments
        # cannot change the request after the cache key has been computed.
        payload = json.loads(canonical)
        payload["case_id"] = case_id
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            try:
                result = await self._session.call_tool(tool_name, arguments=payload)
                break
            except Exception as exc:
                if attempt == MAX_TRANSIENT_RETRIES or not _is_transient_error(exc):
                    raise
                await asyncio.sleep(RETRY_DELAY_SECONDS)

        if result.is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = result.structured_content
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        self._cache[cache_key] = copy.deepcopy(evidence)
        return copy.deepcopy(evidence)


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
