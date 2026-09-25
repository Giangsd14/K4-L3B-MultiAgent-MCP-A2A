from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from student_agent.contracts import Contracts
from student_agent.coordinator import MultiAgentCoordinator
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.trace import TraceWriter


@pytest.mark.anyio
async def test_coordinator_end_to_end_orchestration(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    # Mock gateway
    session_mock = AsyncMock()
    gateway = EvidenceGateway(session_mock, contracts)

    coordinator = MultiAgentCoordinator(gateway, trace)
    sample_case = {
        "case_id": "L3B_CASE_001",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "Test complaint message",
            "claimed_order_id": "af0bbb47f125381ce9f3597dc70ef07b",
            "claims": [{"claim_id": "c1", "topic": "late_delivery_logistics"}],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": ["af0bbb47f125381ce9f3597dc70ef07b", "cand-2"],
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
            "require_independent_verification": True,
        },
        "customer_unique_id_hint": "cust-unique-123",
    }

    output = await coordinator.coordinate(sample_case)

    # Validate output schema
    contracts.validate_output(output, "coordinator_output")
    assert output["case_id"] == "L3B_CASE_001"
    assert output["entity_resolution"]["status"] == "resolved"
    assert "af0bbb47f125381ce9f3597dc70ef07b" in output["entity_resolution"]["resolved_order_ids"]

    # Verify trace events sequence
    trace_events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    event_types = [e["event_type"] for e in trace_events]
    assert "task_assigned" in event_types
    assert "handoff" in event_types
    assert "verification_completed" in event_types
