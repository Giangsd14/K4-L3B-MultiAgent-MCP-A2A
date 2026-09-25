from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.verifier import VerificationError, verify_output

CASE_ID = "L3B_CASE_001"
ORDER_REF = "ev_" + "o" * 24
POLICY_REF = "ev_" + "p" * 24


@pytest.fixture(scope="module")
def contracts() -> Contracts:
    return Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")


def sample() -> dict:
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": CASE_ID,
        "assessment": {
            "primary_issue": "unsupported_claim",
            "secondary_issues": [],
            "case_status": "no_action",
            "confidence": 0.99,
        },
        "affected_entities": {
            "order_ids": ["order-1"],
            "item_ids": [],
            "seller_ids": ["seller-1"],
            "payment_references": [],
            "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": ["order-1"],
            "rejected_candidates": ["wrong-order"],
            "confidence": 0.9,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
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
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "CLAIM_UNSUPPORTED", "rank": 1}],
            "responsible_parties": [],
        },
        "evidence_refs": [ORDER_REF],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }


def records(*, warning: bool = False) -> list[dict]:
    return [
        {
            "case_id": CASE_ID,
            "evidence_ref": ORDER_REF,
            "domain": "order",
            "warnings": ["partial data"] if warning else [],
        }
    ]


def verify(
    output: dict,
    contracts: Contracts,
    evidence: list[dict] | None = None,
    consumed: set[str] | None = None,
) -> dict:
    return verify_output(
        output,
        case_id=CASE_ID,
        evidence_records=records() if evidence is None else evidence,
        contracts=contracts,
        consumed_evidence_refs={ORDER_REF} if consumed is None else consumed,
    )


def test_valid_output_calibrated_without_mutating_input(contracts: Contracts) -> None:
    output = sample()
    verified = verify(output, contracts)
    assert verified["assessment"]["confidence"] == 0.9
    assert output["assessment"]["confidence"] == 0.99


def test_schema_and_case_scope_rejected(contracts: Contracts) -> None:
    output = sample()
    output["extra"] = True
    with pytest.raises(VerificationError, match="Additional properties"):
        verify(output, contracts)
    output = sample()
    output["case_id"] = "L3B_CASE_002"
    with pytest.raises(VerificationError, match="case_id"):
        verify(output, contracts)


def test_unknown_cross_case_and_unconsumed_refs_rejected(contracts: Contracts) -> None:
    output = sample()
    with pytest.raises(VerificationError, match="unknown or cross-case"):
        verify(output, contracts, evidence=[])
    cross_case = [{**records()[0], "case_id": "L3B_CASE_002"}]
    with pytest.raises(VerificationError, match="another case"):
        verify(output, contracts, evidence=cross_case)
    with pytest.raises(VerificationError, match="not consumed"):
        verify(output, contracts, consumed=set())


def test_entity_resolution_and_seller_consistency(contracts: Contracts) -> None:
    output = sample()
    output["entity_resolution"]["rejected_candidates"] = ["order-1"]
    with pytest.raises(VerificationError, match="overlap"):
        verify(output, contracts)
    output = sample()
    output["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "seller", "party_id": "other-seller"},
    ]
    with pytest.raises(VerificationError, match="not affected"):
        verify(output, contracts)
    output = sample()
    output["shipment_analysis"]["late_seller_ids"] = ["seller-1"]
    with pytest.raises(VerificationError, match="requires seller_delay"):
        verify(output, contracts)


def test_issue_verdict_and_action_consistency(contracts: Contracts) -> None:
    output = sample()
    output["assessment"].update(primary_issue="late_delivery_seller", case_status="action_required")
    output["resolution_actions"] = ["review_seller_delay"]
    with pytest.raises(VerificationError, match="shipment verdict"):
        verify(output, contracts)
    output = sample()
    output["resolution_actions"] = ["refund"]
    with pytest.raises(VerificationError, match="no_action has actions"):
        verify(output, contracts)


def test_issue_responsibility_status_and_refund_action_consistency(
    contracts: Contracts,
) -> None:
    output = sample()
    output["assessment"].update(
        primary_issue="late_delivery_logistics", case_status="action_required"
    )
    output["shipment_analysis"].update(verdict="logistics_delay")
    output["resolution_actions"] = ["review_logistics_delay"]
    with pytest.raises(VerificationError, match="no responsible provider"):
        verify(output, contracts)

    output["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "logistics_provider", "party_id": None},
    ]
    assert verify(output, contracts)["assessment"]["case_status"] == "action_required"

    output["assessment"]["case_status"] = "no_action"
    with pytest.raises(VerificationError, match="status contradicts"):
        verify(output, contracts)

    output, evidence, consumed = refunded_sample()
    output["resolution_actions"] = ["review_refund_for_canceled_order"]
    with pytest.raises(VerificationError, match="amount and action disagree"):
        verify(output, contracts, evidence, consumed)


def refunded_sample() -> tuple[dict, list[dict], set[str]]:
    output = sample()
    output["assessment"].update(primary_issue="canceled_order_paid", case_status="action_required")
    output["root_cause_analysis"]["ranked_causes"] = [
        {"cause_code": "CANCELED_ORDER_PAID", "rank": 1},
    ]
    output["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "unknown", "party_id": None},
    ]
    output["evidence_refs"].append(POLICY_REF)
    output["financial_resolution"] = {
        "currency": "BRL",
        "recommended_refund_brl": 12.35,
        "refund_lines": [
            {"reason_code": "CANCELED", "amount_brl": 10.25, "entity_id": "order-1"},
            {"reason_code": "SHIPPING", "amount_brl": 2.10, "entity_id": "order-1"},
        ],
    }
    output["resolution_actions"] = ["issue_authorized_refund"]
    evidence = records() + [
        {
            "case_id": CASE_ID,
            "evidence_ref": POLICY_REF,
            "domain": "policy",
            "warnings": [],
        }
    ]
    return output, evidence, {ORDER_REF, POLICY_REF}


def test_refund_totals_balance_and_policy_evidence(contracts: Contracts) -> None:
    output, evidence, consumed = refunded_sample()
    assert (
        verify(output, contracts, evidence, consumed)["financial_resolution"][
            "recommended_refund_brl"
        ]
        == 12.35
    )
    broken = deepcopy(output)
    broken["financial_resolution"]["refund_lines"][0]["amount_brl"] = 10.24
    with pytest.raises(VerificationError, match="do not sum"):
        verify(broken, contracts, evidence, consumed)
    broken = deepcopy(output)
    broken["payment_analysis"]["refundable_total_brl"] = 10.0
    with pytest.raises(VerificationError, match="exceeds balance"):
        verify(broken, contracts, evidence, consumed)
    without_policy = [{**item, "domain": "order"} for item in evidence]
    with pytest.raises(VerificationError, match="policy evidence"):
        verify(output, contracts, without_policy, consumed)


def test_confidence_reduced_for_ambiguity_conflict_and_warnings(contracts: Contracts) -> None:
    output = sample()
    output["entity_resolution"].update(status="ambiguous", resolved_order_ids=[])
    output["assessment"].update(
        primary_issue="insufficient_evidence", case_status="needs_investigation"
    )
    output["resolution_actions"] = ["investigate_missing_evidence"]
    output["data_conflicts"] = [
        {
            "field": "order_status",
            "sources": ["order", "shipment"],
            "selected_source": None,
            "resolution_code": "UNRESOLVED_CONFLICT",
        }
    ]
    verified = verify(output, contracts, records(warning=True))
    assert verified["assessment"]["confidence"] <= 0.35


def test_confidence_is_capped_by_entity_resolution(contracts: Contracts) -> None:
    output = sample()
    output["entity_resolution"]["confidence"] = 0.4
    output["assessment"]["confidence"] = 0.99
    assert verify(output, contracts)["assessment"]["confidence"] == 0.4
