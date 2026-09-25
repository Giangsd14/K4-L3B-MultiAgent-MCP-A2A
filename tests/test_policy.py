from __future__ import annotations

from dataclasses import dataclass

import pytest

from student_agent.policy import decide_policy, normalize_policy_facts

CASE = {"case_id": "L3B_CASE_001", "policy_version": "EC_POLICY_V2"}
POLICY_REF = "ev_" + "p" * 24


def facts(**changes: object) -> dict:
    base = {
        "entity_resolution": {"status": "resolved", "resolved_order_ids": ["order-1"]},
        "order_status": "delivered",
        "affected_seller_ids": ["seller-1"],
        "shipment_analysis": {
            "verdict": "on_time",
            "late_seller_ids": [],
            "timeline_complete": True,
        },
        "payment_analysis": {
            "verdict": "reconciled",
            "captured_total_brl": 100.0,
            "refunded_total_brl": 0.0,
            "refundable_total_brl": 100.0,
        },
        "policy_decision": {
            "evidence_ref": POLICY_REF,
            "currency": "BRL",
            "rules": {
                "canceled_order_paid": {
                    "case_status": "action_required",
                    "recommended_action": "issue_refund",
                    "refund_brl": 79.0,
                    "responsible_parties": [{"party_type": "platform", "party_id": None}],
                },
                "unavailable_order_paid": {
                    "case_status": "action_required",
                    "recommended_action": "issue_refund",
                    "refund_brl": 89.0,
                    "responsible_parties": [{"party_type": "seller", "party_id": "seller-1"}],
                },
                "duplicate_charge": {
                    "case_status": "action_required",
                    "recommended_action": "refund_duplicate_charge",
                    "refund_brl": 64.0,
                    "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
                },
                "payment_mismatch": {
                    "case_status": "action_required",
                    "recommended_action": "reconcile_payment",
                    "refund_brl": 35.0,
                    "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
                },
                "refund_failed": {
                    "case_status": "action_required",
                    "recommended_action": "retry_refund",
                    "refund_brl": 52.0,
                    "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
                },
                "refund_pending": {
                    "case_status": "needs_investigation",
                    "recommended_action": "monitor_refund",
                    "refund_brl": 0.0,
                    "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
                },
                "late_delivery_seller": {
                    "case_status": "action_required",
                    "recommended_action": "refund_freight",
                    "refund_brl": 18.0,
                    "responsible_parties": [{"party_type": "seller", "party_id": "seller-1"}],
                },
                "late_delivery_logistics": {
                    "case_status": "action_required",
                    "recommended_action": "refund_freight",
                    "refund_brl": 16.0,
                    "responsible_parties": [{"party_type": "logistics_provider", "party_id": None}],
                },
                "valid_split_payment": {
                    "case_status": "no_action",
                    "recommended_action": "document_no_action",
                    "refund_brl": 0.0,
                    "responsible_parties": [{"party_type": "customer", "party_id": None}],
                },
                "unsupported_claim": {
                    "case_status": "no_action",
                    "recommended_action": "document_no_action",
                    "refund_brl": 0.0,
                    "responsible_parties": [{"party_type": "customer", "party_id": None}],
                },
            },
        },
        "valid_split_payment": False,
        "claims_resolved_unsupported": False,
        "data_conflicts": [],
        "warnings": [],
    }
    base.update(changes)
    return base


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"order_status": "canceled"}, "canceled_order_paid"),
        ({"order_status": "unavailable"}, "unavailable_order_paid"),
        ({"payment_analysis": {"verdict": "duplicate_capture"}}, "duplicate_charge"),
        ({"payment_analysis": {"verdict": "capture_mismatch"}}, "payment_mismatch"),
        ({"payment_analysis": {"verdict": "refund_failed"}}, "refund_failed"),
        ({"payment_analysis": {"verdict": "refund_pending"}}, "refund_pending"),
        ({"shipment_analysis": {"verdict": "seller_delay"}}, "late_delivery_seller"),
        ({"shipment_analysis": {"verdict": "logistics_delay"}}, "late_delivery_logistics"),
        ({"payment_analysis": {"verdict": "insufficient_evidence"}}, "insufficient_evidence"),
        ({"valid_split_payment": True}, "valid_split_payment"),
        ({"claims_resolved_unsupported": True}, "unsupported_claim"),
    ],
)
def test_issue_precedence_and_safe_unknowns(changes: dict, expected: str) -> None:
    decision = decide_policy(CASE, facts(**changes))
    assert decision["assessment"]["primary_issue"] == expected
    expected_refunds = {
        "canceled_order_paid": 79.0,
        "unavailable_order_paid": 89.0,
        "late_delivery_seller": 18.0,
        "late_delivery_logistics": 16.0,
    }
    assert decision["financial_resolution"]["recommended_refund_brl"] == (
        expected_refunds.get(expected, 0.0)
    )


def test_canceled_paid_outweighs_late_shipment() -> None:
    decision = decide_policy(
        CASE,
        facts(
            order_status="canceled",
            shipment_analysis={
                "verdict": "logistics_delay",
            },
        ),
    )
    assert decision["assessment"]["primary_issue"] == "canceled_order_paid"


def test_identity_ambiguity_and_conflict_prevent_strong_issue() -> None:
    ambiguous = decide_policy(CASE, facts(entity_resolution={"status": "ambiguous"}))
    conflicting = decide_policy(CASE, facts(data_conflicts=[{"field": "order_status"}]))
    assert ambiguous["assessment"]["primary_issue"] == "insufficient_evidence"
    assert conflicting["assessment"]["case_status"] == "needs_investigation"


def test_missing_refund_total_does_not_establish_unpaid_cancellation() -> None:
    decision = decide_policy(
        CASE,
        facts(
            order_status="canceled",
            payment_analysis={
                "verdict": "insufficient_evidence",
                "captured_total_brl": 100.0,
                "refunded_total_brl": None,
            },
        ),
    )
    assert decision["assessment"]["primary_issue"] == "insufficient_evidence"


def test_refund_requires_explicit_policy_and_known_balance() -> None:
    policy = {
        "evidence_ref": POLICY_REF,
        "eligible": True,
        "rules": {},
        "recommended_refund_brl": 12.35,
        "refund_lines": [
            {"reason_code": "CANCELED_ITEM", "amount_brl": 10.25, "entity_id": "item-1"},
            {"reason_code": "SHIPPING", "amount_brl": 2.10, "entity_id": "order-1"},
        ],
    }
    decision = decide_policy(CASE, facts(order_status="canceled", policy_decision=policy))
    assert decision["financial_resolution"]["recommended_refund_brl"] == 12.35
    assert (
        sum(line["amount_brl"] for line in decision["financial_resolution"]["refund_lines"])
        == 12.35
    )
    assert "issue_authorized_refund" in decision["resolution_actions"]

    excessive = decide_policy(
        CASE,
        facts(
            order_status="canceled",
            policy_decision={
                **policy,
                "recommended_refund_brl": 120.35,
            },
        ),
    )
    missing_balance = decide_policy(
        CASE,
        facts(
            order_status="canceled",
            policy_decision=policy,
            payment_analysis={"verdict": "reconciled", "captured_total_brl": 100.0},
        ),
    )
    assert excessive["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert missing_balance["financial_resolution"]["refund_lines"] == []


@dataclass(frozen=True)
class Fact:
    case_id: str
    domain: str
    tool_name: str
    evidence_ref: str
    data: object
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Identity:
    status: str
    resolved_order_ids: tuple[str, ...]
    rejected_candidates: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class Investigation:
    identity: Identity
    facts: tuple[Fact, ...]
    warnings: tuple[str, ...] = ()


def test_normalizer_keeps_refs_and_parses_only_known_fields() -> None:
    investigation = Investigation(
        Identity("resolved", ("order-1",), ("wrong-order",), 0.95),
        (
            Fact(
                CASE["case_id"],
                "order",
                "get_order",
                "ev_" + "o" * 24,
                {"order_status": "canceled"},
            ),
            Fact(
                CASE["case_id"],
                "item",
                "get_items",
                "ev_" + "i" * 24,
                {"items": [{"seller_id": "seller-1"}]},
            ),
            Fact(
                CASE["case_id"],
                "payment",
                "get_payments",
                "ev_" + "m" * 24,
                {
                    "captured_total_brl": 100.0,
                    "refunded_total_brl": 0.0,
                    "refundable_total_brl": 100.0,
                    "verdict": "reconciled",
                },
            ),
            Fact(
                CASE["case_id"],
                "policy",
                "get_policy",
                POLICY_REF,
                {
                    "policy_version": "EC_POLICY_V2",
                    "eligible": True,
                    "recommended_refund_brl": 10.0,
                    "refund_lines": [
                        {"reason_code": "CANCELED", "amount_brl": 10.0, "entity_id": "order-1"},
                    ],
                },
            ),
        ),
    )
    normalized = normalize_policy_facts(CASE, investigation)
    assert normalized["order_status"] == "canceled"
    assert normalized["affected_seller_ids"] == ["seller-1"]
    assert normalized["policy_decision"]["evidence_ref"] == POLICY_REF
    assert decide_policy(CASE, normalized)["financial_resolution"]["recommended_refund_brl"] == 10.0
