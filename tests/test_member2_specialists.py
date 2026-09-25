from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from student_agent.contracts import Contracts
from student_agent.entity_resolver import EntityResolverAgent
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.shipment_agent import ShipmentSpecialistAgent
from student_agent.trace import TraceWriter


@pytest.mark.anyio
async def test_entity_resolver_candidate_disambiguation(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    session_mock = AsyncMock()
    gateway = EvidenceGateway(session_mock, contracts)
    resolver = EntityResolverAgent(gateway, trace)

    case = {
        "case_id": "L3B_CASE_001",
        "customer_request": {
            "claimed_order_id": "af0bbb47f125381ce9f3597dc70ef07b",
        },
        "candidate_order_ids": [
            "af0bbb47f125381ce9f3597dc70ef07b",
            "candidate-001",
            "candidate-invalid",
        ],
        "customer_unique_id_hint": "customer-597dc70ef07b",
    }

    result = await resolver.run({"case": case})
    assert result["status"] == "resolved"
    assert result["resolved_order_ids"] == ["af0bbb47f125381ce9f3597dc70ef07b"]
    assert "candidate-001" in result["rejected_candidates"]
    assert "candidate-invalid" in result["rejected_candidates"]
    assert set(result["resolved_order_ids"]).isdisjoint(set(result["rejected_candidates"]))
    assert result["customer_unique_id"] == "customer-597dc70ef07b"


@pytest.mark.anyio
async def test_shipment_specialist_seller_delay(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    # Mock gateway to return a tracking record with carrier dispatch after shipping limit date
    session_mock = AsyncMock()
    gateway = EvidenceGateway(session_mock, contracts)

    # Mock call_tool on gateway
    async def mock_call(tool_name: str, **kwargs: str):
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_tracking123456789012345678",
            "source": "logistics_provider",
            "retrieved_at": "2026-09-25T00:00:00Z",
            "data": {
                "order_id": "af0bbb47f125381ce9f3597dc70ef07b",
                "seller_id": "seller-001",
                "shipment_id": "ship-001",
                "item_id": "item-001",
                "shipping_limit_date": "2018-01-05T00:00:00Z",
                "order_delivered_carrier_date": "2018-01-08T00:00:00Z",  # Late!
                "order_delivered_customer_date": "2018-01-15T00:00:00Z",
                "order_estimated_delivery_date": "2018-01-12T00:00:00Z",
            },
        }

    gateway.call = mock_call  # type: ignore[assignment]
    shipment_agent = ShipmentSpecialistAgent(gateway, trace)

    context = {
        "case": {
            "case_id": "L3B_CASE_001",
            "customer_request": {"claims": [{"topic": "late_delivery_seller"}]},
        },
        "entity_resolution": {
            "resolved_order_ids": ["af0bbb47f125381ce9f3597dc70ef07b"],
        },
    }

    result = await shipment_agent.run(context)
    assert result["verdict"] == "seller_delay"
    assert "seller-001" in result["late_seller_ids"]
    assert "seller-001" in result["affected_seller_ids"]
    assert "ship-001" in result["affected_shipment_ids"]
    assert "item-001" in result["affected_item_ids"]
    assert result["timeline_complete"] is True

    # Verify tool_result_consumed trace event
    trace_events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    consumed_events = [e for e in trace_events if e["event_type"] == "tool_result_consumed"]
    assert len(consumed_events) >= 1
    assert consumed_events[0]["actor"] == "shipment_agent"
    assert "ev_tracking123456789012345678" in consumed_events[0]["evidence_refs"]
