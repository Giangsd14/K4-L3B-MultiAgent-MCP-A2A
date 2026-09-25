from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.decision import CaseFacts
from student_agent.evidence import CaseEvidence
from student_agent.trace import TraceWriter
from student_agent.verifier import verify_semantics
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str | None]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        assert case_id == "L3B_CASE_001"
        self.calls.append((tool_name, arguments.get("order_id")))
        if tool_name not in self.responses:
            raise RuntimeError(f"{tool_name} unavailable")
        value = self.responses[tool_name]
        if tool_name == "get_order":
            value = value[arguments["order_id"]]
        if isinstance(value, Exception):
            raise value
        index = len(self.calls)
        return {"data": value, "evidence_ref": f"ev_{index:024d}"}


def case(topic: str = "valid_split_payment") -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_001",
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": ["order-1", "unrelated-order"],
        "customer_unique_id_hint": "customer-1",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-1", "topic": topic},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
        },
    }


def responses() -> dict[str, Any]:
    return {
        "get_customer_history": {"orders": [{"order_id": "order-1"}]},
        "get_order": {
            "order-1": {
                "order_id": "order-1",
                "customer_unique_id": "customer-1",
                "order_status": "delivered",
            }
        },
        "get_order_items": [{"order_item_id": 1, "seller_id": "seller-1"}],
        "get_product_context": [{"product_id": "product-1"}],
        "get_shipment_summary": {
            "order_status": "delivered",
            "estimated_delivery_at": "2018-01-10T12:00:00-03:00",
            "delivered_customer_at": "2018-01-09T12:00:00-03:00",
            "events": [],
        },
        "get_payment_timeline": {
            "payments": [
                {"payment_reference": "payment-1", "payment_value": 40},
                {"payment_reference": "payment-2", "payment_value": 60},
            ],
            "events": [],
        },
        "get_refund_timeline": {"events": []},
        "get_sellers": [{"seller_id": "seller-1"}],
        "get_policy": {
            "rules": {
                "valid_split_payment": {
                    "case_status": "no_action",
                    "recommended_action": "document_no_action",
                    "refund_brl": 0,
                },
                "late_delivery_seller": {
                    "case_status": "action_required",
                    "recommended_action": "contact_seller",
                    "refund_brl": 0,
                    "responsible_parties": [{"party_type": "seller", "party_id": None}],
                },
                "unsupported_claim": {
                    "case_status": "no_action",
                    "recommended_action": "document_no_action",
                    "refund_brl": 0,
                },
            }
        },
    }


def run_case(tmp_path: Path, input_case: dict[str, Any], gateway: FakeGateway) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    return asyncio.run(solve_case(input_case, gateway, trace))


def test_customer_history_prunes_unrelated_candidate_and_split_is_not_duplicate(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway(responses())
    output = run_case(tmp_path, case("duplicate_charge"), gateway)

    assert output["entity_resolution"]["resolved_order_ids"] == ["order-1"]
    assert output["entity_resolution"]["rejected_candidates"] == ["unrelated-order"]
    assert output["entity_resolution"]["confidence"] == 1.0
    assert gateway.calls.count(("get_order", "order-1")) == 1
    assert ("get_order", "unrelated-order") not in gateway.calls
    assert not any(name == "get_sellers" for name, _ in gateway.calls)
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
    assert output["affected_entities"]["payment_references"] == ["payment-1", "payment-2"]
    assert output["affected_entities"]["shipment_ids"] == ["ship-order-1"]
    assert output["data_conflicts"] == []


def test_missing_timelines_do_not_become_reconciled_or_on_time(tmp_path: Path) -> None:
    data = responses()
    del data["get_shipment_summary"]
    del data["get_payment_timeline"]
    gateway = FakeGateway(data)
    output = run_case(tmp_path, case(), gateway)

    assert output["shipment_analysis"]["verdict"] == "insufficient_evidence"
    assert output["payment_analysis"]["verdict"] == "insufficient_evidence"
    assert output["payment_analysis"]["captured_total_brl"] is None
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert sum(name == "get_shipment_summary" for name, _ in gateway.calls) == 1
    assert sum(name == "get_payment_timeline" for name, _ in gateway.calls) == 1


def test_payment_row_without_amount_does_not_become_zero_capture(tmp_path: Path) -> None:
    data = responses()
    data["get_payment_timeline"]["payments"] = [{"payment_reference": "payment-1"}]
    output = run_case(tmp_path, case(), FakeGateway(data))

    assert output["payment_analysis"]["captured_total_brl"] is None
    assert output["payment_analysis"]["verdict"] == "insufficient_evidence"


def test_seller_delay_uses_seller_evidence_and_claim_specific_refs(tmp_path: Path) -> None:
    data = responses()
    data["get_shipment_summary"]["events"] = [
        {"event_type": "delivered_late", "actor": "seller", "seller_id": "seller-1"}
    ]
    gateway = FakeGateway(data)
    output = run_case(tmp_path, case("late_delivery_seller"), gateway)

    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["shipment_analysis"]["late_seller_ids"] == ["seller-1"]
    assert sum(name == "get_sellers" for name, _ in gateway.calls) == 1
    assert len(output["claim_assessments"][0]["evidence_refs"]) < len(output["evidence_refs"])
    assert (
        len(
            set(output["claim_assessments"][0]["evidence_refs"])
            & set(output["claim_assessments"][1]["evidence_refs"])
        )
        >= 3
    )


def test_refunds_sum_distinct_confirmed_transactions(tmp_path: Path) -> None:
    data = responses()
    data["get_payment_timeline"]["payments"] = [
        {"payment_reference": "payment-1", "payment_value": 100}
    ]
    data["get_refund_timeline"]["events"] = [
        {"refund_id": "r1", "status": "pending", "amount_brl": 10, "created_at": "2018-01-01"},
        {"refund_id": "r1", "status": "confirmed", "amount_brl": 10, "created_at": "2018-01-02"},
        {"refund_id": "r2", "status": "confirmed", "amount_brl": 20, "created_at": "2018-01-03"},
    ]
    output = run_case(tmp_path, case("unsupported_claim"), FakeGateway(data))

    assert output["payment_analysis"]["refunded_total_brl"] == 30
    assert output["payment_analysis"]["refundable_total_brl"] == 70


def test_multiple_verified_orders_remain_ambiguous(tmp_path: Path) -> None:
    data = responses()
    data["get_customer_history"]["orders"].append({"order_id": "unrelated-order"})
    data["get_order"]["unrelated-order"] = {
        "order_id": "unrelated-order",
        "customer_unique_id": "customer-1",
        "order_status": "delivered",
    }
    gateway = FakeGateway(data)
    output = run_case(tmp_path, case(), gateway)

    assert output["entity_resolution"]["status"] == "ambiguous"
    assert output["entity_resolution"]["resolved_order_ids"] == ["order-1", "unrelated-order"]
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert not any(name == "get_order_items" for name, _ in gateway.calls)


def test_canceled_order_refund_is_limited_to_unrefunded_capture(tmp_path: Path) -> None:
    data = responses()
    data["get_order"]["order-1"]["order_status"] = "canceled"
    data["get_shipment_summary"]["order_status"] = "canceled"
    data["get_payment_timeline"]["payments"] = [
        {"payment_reference": "payment-1", "payment_value": 100}
    ]
    data["get_refund_timeline"]["events"] = [
        {"refund_id": "r1", "status": "confirmed", "amount_brl": 20}
    ]
    data["get_policy"]["rules"]["canceled_order_paid"] = {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 100,
    }
    output = run_case(tmp_path, case("canceled_order_paid"), FakeGateway(data))

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 80
    assert output["financial_resolution"]["refund_lines"][0]["amount_brl"] == 80


def test_case_evidence_shares_concurrent_identical_calls(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(root / "contracts" / "schemas"))
    gateway = FakeGateway(responses())
    evidence = CaseEvidence("L3B_CASE_001", gateway, trace)

    async def fetch_twice() -> None:
        first, second = await asyncio.gather(
            evidence.fetch("get_order", actor="entity_agent", order_id="order-1"),
            evidence.fetch("get_order", actor="entity_agent", order_id="order-1"),
        )
        assert first is second

    asyncio.run(fetch_twice())
    assert gateway.calls == [("get_order", "order-1")]


def test_unavailable_order_with_no_refund_record_keeps_zero_refund(tmp_path: Path) -> None:
    data = responses()
    data["get_order"]["order-1"]["order_status"] = "unavailable"
    data["get_shipment_summary"]["order_status"] = "unavailable"
    data["get_payment_timeline"]["payments"] = [{"payment_value": 100}]
    data["get_refund_timeline"] = RuntimeError(
        "MCP tool get_refund_timeline failed: refund timeline unavailable"
    )
    data["get_policy"]["rules"]["unavailable_order_paid"] = {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 100,
    }
    output = run_case(tmp_path, case("unavailable_order_paid"), FakeGateway(data))

    assert output["assessment"]["primary_issue"] == "unavailable_order_paid"
    assert output["assessment"]["confidence"] == 0.82
    assert output["payment_analysis"]["refunded_total_brl"] == 0
    assert output["payment_analysis"]["refundable_total_brl"] == 100
    assert output["financial_resolution"]["recommended_refund_brl"] == 100
    assert output["affected_entities"]["payment_references"] == ["pay-1"]


def test_transport_error_does_not_imply_no_refund(tmp_path: Path) -> None:
    data = responses()
    data["get_refund_timeline"] = ConnectionError("connection reset")
    output = run_case(tmp_path, case(), FakeGateway(data))

    assert output["payment_analysis"]["refunded_total_brl"] is None
    assert output["assessment"]["primary_issue"] == "valid_split_payment"


def test_unknown_refund_application_error_is_low_confidence_inference(tmp_path: Path) -> None:
    data = responses()
    data["get_order"]["order-1"]["order_status"] = "unavailable"
    data["get_shipment_summary"]["order_status"] = "unavailable"
    data["get_refund_timeline"] = RuntimeError(
        "MCP tool get_refund_timeline failed: timeline empty"
    )
    data["get_policy"]["rules"]["unavailable_order_paid"] = {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "refund_brl": 100,
    }
    output = run_case(tmp_path, case("unavailable_order_paid"), FakeGateway(data))

    assert output["assessment"]["primary_issue"] == "unavailable_order_paid"
    assert output["assessment"]["confidence"] == 0.7


def test_refund_permission_error_keeps_missing_data(tmp_path: Path) -> None:
    data = responses()
    data["get_order"]["order-1"]["order_status"] = "unavailable"
    data["get_shipment_summary"]["order_status"] = "unavailable"
    data["get_refund_timeline"] = RuntimeError(
        "MCP tool get_refund_timeline failed: permission denied"
    )
    output = run_case(tmp_path, case("unavailable_order_paid"), FakeGateway(data))

    assert output["payment_analysis"]["refunded_total_brl"] is None
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"


def test_explicit_duplicate_evidence_overrides_conflicting_claim_topic(tmp_path: Path) -> None:
    data = responses()
    data["get_payment_timeline"]["events"] = [{"event_type": "duplicate_charge"}]
    data["get_shipment_summary"]["shipment_id"] = "shipment-actual"

    split_claim = run_case(tmp_path / "split", case("valid_split_payment"), FakeGateway(data))
    duplicate_claim = run_case(tmp_path / "duplicate", case("duplicate_charge"), FakeGateway(data))

    assert split_claim["assessment"]["primary_issue"] == "duplicate_charge"
    assert duplicate_claim["assessment"]["primary_issue"] == "duplicate_charge"
    assert split_claim["claim_assessments"][0]["verdict"] == "unsupported"
    assert duplicate_claim["claim_assessments"][0]["verdict"] == "supported"
    assert duplicate_claim["affected_entities"]["shipment_ids"] == ["shipment-actual"]


def test_explicit_seller_delay_precedes_canceled_status(tmp_path: Path) -> None:
    data = responses()
    data["get_order"]["order-1"]["order_status"] = "canceled"
    data["get_shipment_summary"]["order_status"] = "canceled"
    data["get_shipment_summary"]["events"] = [
        {"event_type": "delivered_late", "actor": "seller", "seller_id": "seller-1"}
    ]
    data["get_refund_timeline"] = RuntimeError(
        "MCP tool get_refund_timeline failed: refund timeline unavailable"
    )
    output = run_case(tmp_path, case("late_delivery_seller"), FakeGateway(data))

    assert output["assessment"]["primary_issue"] == "late_delivery_seller"


def test_item_total_difference_is_not_a_payment_mismatch(tmp_path: Path) -> None:
    data = responses()
    data["get_order_items"] = [
        {"order_item_id": 1, "seller_id": "seller-1", "price": 90, "freight_value": 10}
    ]
    data["get_payment_timeline"]["payments"] = [
        {"payment_reference": "payment-1", "payment_value": 100},
        {"payment_reference": "payment-2", "payment_value": 100},
    ]
    output = run_case(tmp_path, case("valid_split_payment"), FakeGateway(data))

    assert output["payment_analysis"]["verdict"] == "reconciled"
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["claim_assessments"][0]["verdict"] == "supported"


def test_semantic_verifier_rejects_unbacked_payment_mismatch(tmp_path: Path) -> None:
    output = run_case(tmp_path, case(), FakeGateway(responses()))
    output["payment_analysis"]["verdict"] = "capture_mismatch"

    with pytest.raises(ValueError, match="lacks payment-timeline evidence"):
        verify_semantics(output, CaseFacts(payment={"events": []}))


def test_normal_split_is_not_a_secondary_issue_for_failed_refund(tmp_path: Path) -> None:
    data = responses()
    data["get_refund_timeline"] = {
        "events": [{"refund_id": "r1", "status": "failed", "amount_brl": 20}]
    }
    output = run_case(tmp_path, case("valid_split_payment"), FakeGateway(data))

    assert output["assessment"]["primary_issue"] == "refund_failed"
    assert "valid_split_payment" not in output["assessment"]["secondary_issues"]
    assert output["claim_assessments"][0]["verdict"] == "supported"


def test_claimed_refund_pending_is_not_masked_by_payment_mismatch(tmp_path: Path) -> None:
    data = responses()
    data["get_payment_timeline"]["events"] = [{"event_type": "reconciliation_mismatch"}]
    data["get_refund_timeline"] = {
        "events": [{"refund_id": "r1", "status": "pending", "amount_brl": 20}]
    }
    output = run_case(tmp_path, case("refund_pending"), FakeGateway(data))

    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert "payment_mismatch" in output["assessment"]["secondary_issues"]
    assert output["claim_assessments"][0]["verdict"] == "supported"


def test_authoritative_customer_disagreement_is_a_real_conflict(tmp_path: Path) -> None:
    data = responses()
    data["get_customer_history"]["orders"].append({"order_id": "unrelated-order"})
    data["get_order"]["unrelated-order"] = {
        "order_id": "unrelated-order",
        "customer_unique_id": "another-customer",
    }
    output = run_case(tmp_path, case(), FakeGateway(data))

    assert output["entity_resolution"]["rejected_candidates"] == ["unrelated-order"]
    assert output["data_conflicts"][0]["resolution_code"] == "unresolvable_candidate_rejected"
