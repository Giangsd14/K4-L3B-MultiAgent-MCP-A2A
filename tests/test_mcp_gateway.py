from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from mcp import types
from mcp.shared.exceptions import MCPError

from student_agent.contracts import ContractError, Contracts
from student_agent.mcp_gateway import EvidenceGateway


def evidence() -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_12345678901234567890",
        "result_hash": "sha256:" + "a" * 64,
        "domain": "order",
        "data": {"order_id": "order-1", "status": "delivered"},
    }


def result(value: dict[str, Any], *, structured: bool = True) -> types.CallToolResult:
    return types.CallToolResult(
        isError=False,
        structuredContent=value if structured else None,
        content=[] if structured else [types.TextContent(text=json.dumps(value))],
    )


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.list_calls = 0

    async def list_tools(self) -> SimpleNamespace:
        self.list_calls += 1
        return SimpleNamespace(
            tools=[
                types.Tool(
                    name="get_order",
                    description="Get authoritative order data",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "case_id": {"type": "string"},
                            "order_id": {"type": "string"},
                        },
                        "required": ["case_id", "order_id"],
                    },
                )
            ]
        )

    async def call_tool(self, name: str, *, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, copy.deepcopy(arguments)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def gateway(session: FakeSession) -> EvidenceGateway:
    root = Path(__file__).resolve().parents[1]
    return EvidenceGateway(session, Contracts(root / "contracts" / "schemas"))


def test_catalog_returns_schema_and_keeps_list_tools_compatible() -> None:
    async def check() -> None:
        session = FakeSession([])
        client = gateway(session)
        catalog = await client.tool_catalog()
        assert catalog["get_order"]["description"] == "Get authoritative order data"
        assert catalog["get_order"]["inputSchema"]["required"] == ["case_id", "order_id"]
        catalog["get_order"]["inputSchema"]["required"].clear()
        assert (await client.tool_catalog())["get_order"]["inputSchema"]["required"] == [
            "case_id",
            "order_id",
        ]
        assert await client.list_tools() == ["get_order"]
        assert session.list_calls == 1

    asyncio.run(check())


def test_cache_is_canonical_per_case_and_preserves_envelope() -> None:
    async def check() -> None:
        original = evidence()
        session = FakeSession([result(original), result(original)])
        client = gateway(session)
        first = await client.call(
            "get_order", case_id="CASE_A", order_id="order-1", options={"x": 1}
        )
        assert first["evidence_ref"] == original["evidence_ref"]
        assert first["result_hash"] == original["result_hash"]
        first["data"]["status"] = "mutated by caller"

        second = await client.call(
            "get_order", case_id="CASE_A", options={"x": 1}, order_id="order-1"
        )
        assert second["data"]["status"] == "delivered"
        assert len(session.calls) == 1
        await client.call("get_order", case_id="CASE_B", order_id="order-1", options={"x": 1})
        assert len(session.calls) == 2
        assert session.calls[0][1]["case_id"] == "CASE_A"
        assert session.calls[1][1]["case_id"] == "CASE_B"

        with pytest.raises(TypeError):
            await client.call("get_order", case_id="CASE_A", **{"case_id": "CASE_B"})
        assert len(session.calls) == 2

    asyncio.run(check())


def test_retries_transient_error_once_and_does_not_cache_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr("student_agent.mcp_gateway.asyncio.sleep", no_delay)

    async def check() -> None:
        session = FakeSession([TimeoutError("temporary timeout"), result(evidence())])
        client = gateway(session)
        assert (await client.call("get_order", case_id="CASE_A", order_id="order-1"))[
            "evidence_ref"
        ] == evidence()["evidence_ref"]
        assert len(session.calls) == 2
        await client.call("get_order", case_id="CASE_A", order_id="order-1")
        assert len(session.calls) == 2

        failure = FakeSession([MCPError(-32603, "transient"), MCPError(-32603, "still down")])
        with pytest.raises(MCPError):
            await gateway(failure).call("get_order", case_id="CASE_A", order_id="order-1")
        assert len(failure.calls) == 2

    asyncio.run(check())


def test_business_4xx_and_tool_error_are_not_retried() -> None:
    async def check() -> None:
        request = httpx2.Request("POST", "https://example.test/mcp")
        response = httpx2.Response(403, request=request)
        forbidden = httpx2.HTTPStatusError("Forbidden", request=request, response=response)
        session = FakeSession([forbidden])
        with pytest.raises(httpx2.HTTPStatusError):
            await gateway(session).call("get_order", case_id="CASE_A", order_id="order-1")
        assert len(session.calls) == 1

        business = FakeSession(
            [types.CallToolResult(isError=True, content=[types.TextContent(text="unknown order")])]
        )
        with pytest.raises(RuntimeError, match="unknown order"):
            await gateway(business).call("get_order", case_id="CASE_A", order_id="order-1")
        assert len(business.calls) == 1

    asyncio.run(check())


def test_invalid_envelope_is_rejected_without_retry_or_cache() -> None:
    async def check() -> None:
        invalid = evidence()
        invalid["evidence_ref"] = "invented-ref"
        session = FakeSession([result(invalid), result(evidence(), structured=False)])
        client = gateway(session)
        with pytest.raises(ContractError):
            await client.call("get_order", case_id="CASE_A", order_id="order-1")
        assert len(session.calls) == 1
        actual = await client.call("get_order", case_id="CASE_A", order_id="order-1")
        assert actual == evidence()
        assert len(session.calls) == 2

    asyncio.run(check())
