from __future__ import annotations

import json
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.coordinator import MultiAgentCoordinator
from student_agent.trace import TraceWriter
from student_agent.verifier import VerifierAgent


def test_verifier_invariant_alignment(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    verifier = VerifierAgent(trace)

    raw_output = {
        "assessment": {
            "primary_issue": "late_delivery_seller",
            "secondary_issues": ["seller_delay"],
            "case_status": "action_required",
            "confidence": 0.9,
        },
        "affected_entities": {
            "order_ids": ["order-001"],
            "item_ids": ["item-001"],
            "seller_ids": ["seller-001"],
            "payment_references": ["pay-001"],
            "shipment_ids": ["ship-001"],
        },
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": ["order-001"],
            "rejected_candidates": ["order-002"],
            "confidence": 0.95,
        },
        "customer_context": {
            "customer_unique_id": "cust-001",
            "related_order_ids": ["order-001"],
        },
        "shipment_analysis": {
            "verdict": "seller_delay",
            "late_seller_ids": ["seller-001"],
            "timeline_complete": True,
        },
        "payment_analysis": {
            "verdict": "reconciled",
            "captured_total_brl": 100.0,
            "refunded_total_brl": 0.0,
            "refundable_total_brl": 100.0,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "SELLER_PROCESSING_DELAY", "rank": 1}],
            "responsible_parties": [{"party_type": "seller", "party_id": "seller-001"}],
        },
        "evidence_refs": ["ev_12345678901234567890"],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 50.0,
            "refund_lines": [
                {"reason_code": "late_penalty", "amount_brl": 50.0, "entity_id": "seller-001"}
            ],
        },
        "resolution_actions": ["issue_customer_refund"],
    }

    finalized = verifier.verify_and_align("L3B_CASE_001", raw_output)
    contracts.validate_output(finalized, "test_output")
    assert finalized["case_id"] == "L3B_CASE_001"
    assert finalized["schema_version"] == "day09-l3b-output-v2"
    assert finalized["financial_resolution"]["recommended_refund_brl"] == 50.0

    # Verify trace event emitted
    assert trace_path.exists()
    trace_events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(trace_events) == 1
    assert trace_events[0]["event_type"] == "verification_completed"
