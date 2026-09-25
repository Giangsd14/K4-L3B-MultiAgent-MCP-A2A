"""L3B output verification and evidence-aware confidence calibration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import ContractError, Contracts


class VerificationError(ValueError):
    """A public output contradicts its evidence, schema, or other fields."""


def _record(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {
        key: getattr(value, key)
        for key in (
            "case_id",
            "evidence_ref",
            "domain",
            "warnings",
        )
        if hasattr(value, key)
    }


def _amount(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise VerificationError(f"{label}: Boolean is not an amount")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
            raise VerificationError(f"{label}: amount must be nonnegative BRL cents")
    except (InvalidOperation, ValueError, TypeError) as exc:
        if isinstance(exc, VerificationError):
            raise
        raise VerificationError(f"{label}: invalid BRL amount") from exc
    return amount


def _fail(condition: bool, message: str) -> None:
    if condition:
        raise VerificationError(message)


def verify_output(
    output: dict[str, Any],
    *,
    case_id: str,
    evidence_records: Sequence[dict[str, Any]],
    contracts: Contracts,
    consumed_evidence_refs: set[str] | None = None,
) -> dict[str, Any]:
    """Validate, reduce unsupported confidence, and return a clean copy.

    ``evidence_records`` must be from this case's MCP calls. Each record needs
    ``case_id``, ``evidence_ref``, and ``domain``. Pass refs from actual
    ``tool_result_consumed`` trace events in ``consumed_evidence_refs``.
    """
    checked = deepcopy(output)
    try:
        contracts.validate_output(checked, f"outputs/{case_id}.json")
    except ContractError as exc:
        raise VerificationError(str(exc)) from exc
    _fail(checked["case_id"] != case_id, "output case_id does not match current case")

    records = [_record(value) for value in evidence_records]
    known_refs: set[str] = set()
    domains_by_ref: dict[str, str] = {}
    warning_present = False
    for record in records:
        ref = record.get("evidence_ref")
        _fail(record.get("case_id") != case_id, "evidence record belongs to another case")
        _fail(not isinstance(ref, str), "evidence record has no evidence_ref")
        if ref in known_refs:
            _fail(
                domains_by_ref[ref] != record.get("domain"), "evidence ref has conflicting domains"
            )
        known_refs.add(ref)
        domains_by_ref[ref] = str(record.get("domain", ""))
        warning_present |= bool(record.get("warnings"))

    cited_refs = set(checked["evidence_refs"])
    _fail(bool(cited_refs - known_refs), "output cites an unknown or cross-case evidence ref")
    if consumed_evidence_refs is not None:
        _fail(
            bool(cited_refs - consumed_evidence_refs), "output cites evidence not consumed in trace"
        )
    for claim in checked.get("claim_assessments", []):
        _fail(
            bool(set(claim["evidence_refs"]) - cited_refs),
            f"claim {claim['claim_id']} cites a ref absent from output evidence_refs",
        )

    affected = checked["affected_entities"]
    identity = checked["entity_resolution"]
    shipment = checked["shipment_analysis"]
    payment = checked["payment_analysis"]
    issue = checked["assessment"]["primary_issue"]
    status = checked["assessment"]["case_status"]
    resolved = set(identity["resolved_order_ids"])
    rejected = set(identity["rejected_candidates"])
    _fail(bool(resolved & rejected), "resolved orders overlap rejected candidates")
    _fail(bool(resolved - set(affected["order_ids"])), "resolved order is not affected")
    _fail(identity["status"] == "resolved" and not resolved, "resolved identity has no order")
    _fail(identity["status"] == "not_found" and bool(resolved), "not_found identity has orders")

    late_sellers = set(shipment["late_seller_ids"])
    sellers = set(affected["seller_ids"])
    _fail(bool(late_sellers - sellers), "late seller is outside affected sellers")
    _fail(
        shipment["verdict"] != "seller_delay" and bool(late_sellers),
        "late_seller_ids requires seller_delay verdict",
    )
    for party in checked["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller":
            _fail(party["party_id"] not in sellers, "responsible seller is not affected")

    required_shipment = {
        "late_delivery_seller": "seller_delay",
        "late_delivery_logistics": "logistics_delay",
    }
    if issue in required_shipment:
        _fail(
            shipment["verdict"] != required_shipment[issue],
            "primary issue contradicts shipment verdict",
        )
    required_payment = {
        "duplicate_charge": "duplicate_capture",
        "payment_mismatch": "capture_mismatch",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }
    if issue in required_payment:
        _fail(
            payment["verdict"] != required_payment[issue],
            "primary issue contradicts payment verdict",
        )
    parties = {
        party["party_type"] for party in checked["root_cause_analysis"]["responsible_parties"]
    }
    expected_status = (
        "needs_investigation"
        if issue == "insufficient_evidence"
        else "needs_investigation"
        if issue == "refund_pending"
        else "no_action"
        if issue in {"unsupported_claim", "valid_split_payment"}
        else "action_required"
    )
    _fail(status != expected_status, "case status contradicts primary issue")
    if issue == "late_delivery_seller":
        _fail("seller" not in parties, "seller delay has no responsible seller")
    if issue == "late_delivery_logistics":
        _fail("logistics_provider" not in parties, "logistics delay has no responsible provider")
    if issue in {"duplicate_charge", "payment_mismatch", "refund_failed", "refund_pending"}:
        _fail("payment_provider" not in parties, "payment issue has no responsible provider")
    _fail(
        issue == "late_delivery_seller" and "logistics_provider" in parties,
        "logistics provider cannot be responsible for a seller delay",
    )
    _fail(
        issue == "late_delivery_logistics" and "seller" in parties,
        "seller cannot be responsible for a logistics delay",
    )
    _fail(
        issue in {"unsupported_claim", "valid_split_payment"} and status != "no_action",
        "supported no-action issue has action-required status",
    )
    _fail(
        issue == "insufficient_evidence" and status != "needs_investigation",
        "insufficient_evidence requires needs_investigation",
    )
    _fail(status == "no_action" and bool(checked["resolution_actions"]), "no_action has actions")
    _fail(
        status == "action_required" and not checked["resolution_actions"],
        "action required has no action",
    )

    finance = checked["financial_resolution"]
    recommended = _amount(finance["recommended_refund_brl"], "recommended_refund_brl")
    line_total = Decimal(0)
    for line in finance["refund_lines"]:
        line_total += _amount(line["amount_brl"], "refund line")
    _fail(line_total != recommended, "refund lines do not sum to recommended refund")
    _fail(status == "no_action" and recommended > 0, "no_action recommends a refund")
    _fail(
        status == "needs_investigation" and recommended > 0,
        "unverified case recommends a refund",
    )
    has_refund_action = "issue_authorized_refund" in checked["resolution_actions"]
    _fail((recommended > 0) != has_refund_action, "refund amount and action disagree")
    _fail(
        payment["verdict"] == "refunded" and recommended > 0,
        "already refunded payment recommends refund",
    )
    refundable_raw = payment["refundable_total_brl"]
    if recommended > 0:
        _fail(refundable_raw is None, "positive refund has no proven refundable balance")
        _fail(
            recommended > _amount(refundable_raw, "refundable_total_brl"), "refund exceeds balance"
        )
        _fail(
            not any(domains_by_ref[ref] == "policy" for ref in cited_refs),
            "positive refund lacks cited policy evidence",
        )
    captured_raw = payment["captured_total_brl"]
    refunded_raw = payment["refunded_total_brl"]
    if captured_raw is not None and refunded_raw is not None:
        captured = _amount(captured_raw, "captured_total_brl")
        refunded = _amount(refunded_raw, "refunded_total_brl")
        _fail(refunded > captured, "refunded total exceeds captured total")
        if refundable_raw is not None:
            _fail(
                _amount(refundable_raw, "refundable_total_brl") > captured - refunded,
                "refundable balance exceeds uncancelled capture",
            )

    cap = 0.92
    cap = min(cap, identity["confidence"])
    if issue == "insufficient_evidence":
        cap = min(cap, 0.45)
    if identity["status"] != "resolved":
        cap = min(cap, 0.35)
    if shipment["verdict"] == "insufficient_evidence":
        cap = min(cap, 0.75)
    if payment["verdict"] == "insufficient_evidence":
        cap = min(cap, 0.75)
    if shipment["verdict"] == payment["verdict"] == "insufficient_evidence":
        cap = min(cap, 0.55)
    if checked["data_conflicts"]:
        cap = min(cap, 0.5)
    if warning_present:
        cap = min(cap, 0.7)
    if not cited_refs:
        cap = min(cap, 0.2)
    if any(
        claim["verdict"] == "insufficient_evidence"
        for claim in checked.get("claim_assessments", [])
    ):
        cap = min(cap, 0.65)
    checked["assessment"]["confidence"] = min(checked["assessment"]["confidence"], cap)
    try:
        contracts.validate_output(checked, f"outputs/{case_id}.json")
    except ContractError as exc:
        raise VerificationError(str(exc)) from exc
    return checked
