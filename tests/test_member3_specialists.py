from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from student_agent.conflict_resolver import ConflictResolverAgent
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.payment_agent import PaymentSpecialistAgent
from student_agent.trace import TraceWriter


@pytest.mark.anyio
async def test_payment_specialist_duplicate_capture(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    session_mock = AsyncMock()
    gateway = EvidenceGateway(session_mock, contracts)

    # Mock gateway to return payment records with duplicate sequential
    async def mock_call(tool_name: str, **kwargs: str):
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_payment123456789012345678",
            "source": "payment_provider",
            "retrieved_at": "2026-09-25T00:00:00Z",
            "data": {
                "order_id": "b04477ada8d2ad7fa9d95358baaa785d",
                "payment_reference": "pay-dup-001",
                "payment_sequential": 1,
                "payment_value": 150.0,
                "payment_type": "credit_card",
            },
        }

    gateway.call = mock_call  # type: ignore[assignment]
    payment_agent = PaymentSpecialistAgent(gateway, trace)

    context = {
        "case": {
            "case_id": "L3B_CASE_004",
            "customer_request": {
                "claims": [{"claim_id": "c1", "topic": "duplicate_charge"}],
            },
        },
        "entity_resolution": {
            "resolved_order_ids": ["b04477ada8d2ad7fa9d95358baaa785d"],
        },
    }

    result = await payment_agent.run(context)
    assert result["verdict"] == "duplicate_capture"
    assert result["captured_total_brl"] == 150.0
    assert result["recommended_refund_brl"] == 75.0
    assert len(result["refund_lines"]) == 1
    assert result["refund_lines"][0]["reason_code"] == "duplicate_charge_reversal"
    assert "pay-dup-001" in result["affected_payment_references"]


@pytest.mark.anyio
async def test_conflict_resolver_root_cause_and_actions(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)

    session_mock = AsyncMock()
    gateway = EvidenceGateway(session_mock, contracts)
    conflict_agent = ConflictResolverAgent(gateway, trace)

    context = {
        "case": {
            "case_id": "L3B_CASE_001",
            "customer_request": {
                "claims": [
                    {"claim_id": "c1", "topic": "late_delivery_logistics"},
                    {"claim_id": "c2", "topic": "requested_full_refund"},
                ],
            },
        },
        "shipment_analysis": {
            "verdict": "seller_delay",
            "late_seller_ids": ["seller-001"],
            "affected_seller_ids": ["seller-001"],
        },
        "payment_analysis": {
            "verdict": "reconciled",
            "captured_total_brl": 120.0,
            "refundable_total_brl": 120.0,
        },
    }

    result = await conflict_agent.run(context)
    # The conflict resolver detects that customer claimed logistics delay but evidence showed seller delay
    assert result["primary_issue"] == "late_delivery_seller"
    assert len(result["data_conflicts"]) >= 1
    conflict = result["data_conflicts"][0]
    assert conflict["field"] == "delay_attribution"
    assert "seller_dispatch_log" in conflict["sources"]
    assert conflict["selected_source"] == "seller_dispatch_log"

    # Responsible party check
    assert result["responsible_parties"][0]["party_type"] == "seller"
    assert result["responsible_parties"][0]["party_id"] == "seller-001"
    assert "notify_seller_delay_penalty" in result["resolution_actions"]
