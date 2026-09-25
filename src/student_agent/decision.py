from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .evidence import CaseEvidence

ISSUE_TO_CAUSE = {
    "canceled_order_paid": "CANCELED_ORDER_PAID",
    "unavailable_order_paid": "UNAVAILABLE_ORDER_PAID",
    "late_delivery_seller": "LATE_DELIVERY_SELLER",
    "late_delivery_logistics": "LATE_DELIVERY_LOGISTICS",
    "valid_split_payment": "VALID_SPLIT_PAYMENT",
    "payment_mismatch": "PAYMENT_MISMATCH",
    "duplicate_charge": "DUPLICATE_CHARGE",
    "refund_pending": "REFUND_PENDING",
    "refund_failed": "REFUND_FAILED",
    "unsupported_claim": "UNSUPPORTED_CLAIM",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
}


@dataclass
class CaseFacts:
    resolved_order_ids: list[str] = field(default_factory=list)
    rejected_candidates: list[str] = field(default_factory=list)
    entity_status: str = "not_found"
    entity_confidence: float = 0.35
    customer_history: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    products: list[dict[str, Any]] = field(default_factory=list)
    sellers: list[dict[str, Any]] = field(default_factory=list)
    shipment: dict[str, Any] | None = None
    payment: dict[str, Any] | None = None
    refund: dict[str, Any] | None = None
    refund_source: str = "unavailable"
    policy: dict[str, Any] | None = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    history_episode_selected: bool = False

    @property
    def order_id(self) -> str | None:
        return self.resolved_order_ids[0] if self.entity_status == "resolved" else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _ids(records: list[dict[str, Any]], *names: str) -> list[str]:
    values: list[str] = []
    for record in records:
        for name in names:
            value = record.get(name)
            if value is not None and value != "":
                values.append(str(value))
                break
    return list(dict.fromkeys(values))[:20]


def _payment_references(payments: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[str]:
    references = _ids(payments, "payment_reference", "payment_id", "transaction_id")
    if references:
        return references
    # Olist payment rows have a sequence but no provider-issued reference.
    # Give each observed row a stable, local reference rather than dropping it.
    if payments:
        return [f"pay-{index}" for index, _ in enumerate(payments[:20], start=1)]
    return _ids(events, "payment_reference", "payment_id", "transaction_id")


def _shipment_references(shipment: dict[str, Any], order_id: str | None) -> list[str]:
    if not shipment:
        return []
    references = _ids([shipment] + _records(shipment.get("events")), "shipment_id", "tracking_id")
    if references:
        return references
    # The summary can describe one shipment without assigning it an ID.
    return [f"ship-{order_id[:12]}"] if order_id else []


def _amount(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
        return (
            amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if amount.is_finite()
            else Decimal(0)
        )
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _known_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount >= 0:
            return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


def _first_known_amount(record: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        amount = _known_amount(record.get(key))
        if amount is not None:
            return amount
    return None


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _event_time(event: dict[str, Any]) -> str:
    for key in ("occurred_at", "event_at", "created_at", "timestamp"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    return ""


def _latest_refund_events(refund: dict[str, Any] | None) -> list[dict[str, Any]]:
    latest_by_ref: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(sorted(_records(_dict(refund).get("events")), key=_event_time)):
        refund_id = event.get("refund_id") or event.get("refund_reference") or event.get("id")
        latest_by_ref[str(refund_id) if refund_id else f"event-{index}"] = event
    return list(latest_by_ref.values())


def _shipment_finding(facts: CaseFacts, seller_ids: list[str]) -> tuple[str, list[str], bool]:
    data = facts.shipment
    if data is None:
        return "insufficient_evidence", [], False

    events = _records(data.get("events"))
    late_events = [event for event in events if event.get("event_type") == "delivered_late"]
    actors = {event.get("actor") for event in late_events}
    estimated = _date(data.get("estimated_delivery_at"))
    delivered = _date(data.get("delivered_customer_at"))
    timeline_complete = estimated is not None and delivered is not None

    if "seller" in actors and "logistics_provider" in actors:
        return "conflicting", [], timeline_complete
    if "seller" in actors:
        event_sellers = _ids(late_events, "seller_id")
        inferred_seller = seller_ids if len(seller_ids) == 1 else []
        return "seller_delay", event_sellers or inferred_seller, timeline_complete
    if "logistics_provider" in actors:
        return "logistics_delay", [], timeline_complete
    if late_events:
        return "insufficient_evidence", [], timeline_complete

    status = data.get("order_status") or _dict(facts.order).get("order_status")
    if status == "unavailable":
        return "lost", [], timeline_complete
    if status == "canceled":
        return "returned" if delivered else "insufficient_evidence", [], timeline_complete
    if estimated and delivered:
        if delivered > estimated:
            return "insufficient_evidence", [], True
        return "on_time", [], True
    return "insufficient_evidence", [], timeline_complete


def _payment_finding(
    facts: CaseFacts,
) -> tuple[str, Decimal | None, Decimal | None, Decimal | None, list[dict[str, Any]]]:
    if facts.payment is None:
        return "insufficient_evidence", None, None, None, []

    payments = _records(facts.payment.get("payments"))
    events = _records(facts.payment.get("events"))
    raw_payments = _records(_dict(facts.payment).get("payments"))
    duplicate_rows = _duplicate_payment_rows(raw_payments)
    order_status = _dict(facts.order).get("order_status")
    if payments:
        amounts = [
            _first_known_amount(payment, "payment_value", "amount_brl") for payment in payments
        ]
        captured = (
            sum(amounts, Decimal(0)) if all(amount is not None for amount in amounts) else None
        )
    else:
        captures = [event for event in events if event.get("event_type") == "captured"]
        capture_amounts = [_first_known_amount(event, "amount_brl") for event in captures]
        captured = (
            sum(capture_amounts, Decimal(0))
            if captures and all(amount is not None for amount in capture_amounts)
            else None
        )

    event_types = {event.get("event_type") for event in events}
    latest = _latest_refund_events(facts.refund)
    confirmed = [
        event
        for event in latest
        if event.get("status") == "confirmed" or event.get("event_type") == "refunded"
    ]
    refund_amounts = [_first_known_amount(event, "amount_brl") for event in confirmed]
    refunded = (
        sum(refund_amounts, Decimal(0))
        if facts.refund is not None and all(amount is not None for amount in refund_amounts)
        else None
    )
    outstanding = (
        max(Decimal(0), captured - refunded)
        if captured is not None and refunded is not None
        else None
    )

    if duplicate_rows and order_status == "delivered":
        raw_amounts = [
            _first_known_amount(row, "payment_value", "amount_brl") for row in raw_payments
        ]
        if raw_amounts and all(amount is not None for amount in raw_amounts):
            captured = sum(raw_amounts, Decimal(0))
            outstanding = max(Decimal(0), captured - refunded) if refunded is not None else None

    if "duplicate_charge" in event_types or (duplicate_rows and order_status == "delivered"):
        verdict = "duplicate_capture"
    elif "reconciliation_mismatch" in event_types:
        verdict = "capture_mismatch"
    elif any(
        event.get("status") == "failed" or event.get("event_type") == "refund_failed"
        for event in latest
    ):
        verdict = "refund_failed"
    elif any(
        event.get("status") == "pending" or event.get("event_type") == "refund_pending"
        for event in latest
    ):
        verdict = "refund_pending"
    elif confirmed:
        verdict = "refunded"
    elif captured is None:
        verdict = "insufficient_evidence"
    else:
        verdict = "reconciled"
    return verdict, captured, refunded, outstanding, payments


def _duplicate_payment_rows(payments: list[dict[str, Any]]) -> bool:
    seen: set[tuple[str, str, str, Decimal]] = set()
    for payment in payments:
        amount = _first_known_amount(payment, "payment_value", "amount_brl")
        if (
            amount is None
            or payment.get("payment_sequential") in (None, "")
            or payment.get("payment_type") in (None, "")
        ):
            continue
        fingerprint = (
            str(payment.get("payment_sequential", "")),
            str(payment.get("payment_type", "")),
            str(payment.get("payment_installments", "")),
            amount,
        )
        if fingerprint in seen:
            return True
        seen.add(fingerprint)
    return False


def _primary_issue(
    facts: CaseFacts,
    claim_topics: set[str],
    shipment_verdict: str,
    payment_verdict: str,
    captured: Decimal | None,
    outstanding: Decimal | None,
    payments: list[dict[str, Any]],
) -> tuple[str, float]:
    if facts.entity_status != "resolved":
        return "insufficient_evidence", 0.35

    order_status = _dict(facts.order).get("order_status") or _dict(facts.shipment).get(
        "order_status"
    )
    if payment_verdict == "duplicate_capture":
        return "duplicate_charge", 0.9
    if payment_verdict == "capture_mismatch":
        return "payment_mismatch", 0.9
    if payment_verdict in {"refund_failed", "refund_pending"}:
        return payment_verdict, 0.86
    if shipment_verdict == "seller_delay":
        return "late_delivery_seller", 0.86
    if shipment_verdict == "logistics_delay":
        return "late_delivery_logistics", 0.86
    if order_status in {"canceled", "unavailable"} and outstanding is not None and outstanding > 0:
        return f"{order_status}_order_paid", 0.86
    if (
        len(payments) > 1
        and payment_verdict == "reconciled"
        and claim_topics.intersection(
            {"valid_split_payment", "duplicate_charge", "payment_mismatch"}
        )
    ):
        return "valid_split_payment", 0.78
    if facts.refund is None and claim_topics.intersection(
        {
            "requested_full_refund",
            "refund_pending",
            "refund_failed",
            "canceled_order_paid",
            "unavailable_order_paid",
        }
    ):
        return "insufficient_evidence", 0.4
    if facts.order is None or facts.payment is None or facts.shipment is None:
        return "insufficient_evidence", 0.4
    return "unsupported_claim", 0.72


def _policy_decision(
    facts: CaseFacts, issue: str, outstanding: Decimal | None
) -> tuple[str, str, Decimal, list[dict[str, Any]]]:
    rule = _dict(_dict(facts.policy).get("rules")).get(issue)
    rule = _dict(rule)
    if issue == "insufficient_evidence" and not rule:
        return "needs_investigation", "request_additional_evidence", Decimal(0), []
    if not rule:
        return "needs_investigation", "review_policy_manually", Decimal(0), []

    status = str(rule.get("case_status") or "needs_investigation")
    action = str(rule.get("recommended_action") or "review_policy_manually")
    prescribed = _amount(rule.get("refund_brl")) if rule.get("refund_brl") is not None else None
    if outstanding is None:
        refund = Decimal(0)
    elif prescribed is not None:
        refund = min(max(Decimal(0), prescribed), outstanding)
    elif "refund" in action.lower() and status == "action_required":
        refund = outstanding
    else:
        refund = Decimal(0)
    return status, action, refund, _records(rule.get("responsible_parties"))


def _claim_refs(topic: str, issue: str, evidence: CaseEvidence) -> list[str]:
    if topic.startswith("late_delivery"):
        names = ["get_order", "get_shipment_summary", "get_sellers", "get_policy"]
    elif topic == "requested_full_refund" and issue.startswith("late_delivery"):
        names = ["get_order", "get_shipment_summary", "get_payment_timeline", "get_policy"]
    elif topic == "requested_full_refund" and issue in {
        "canceled_order_paid",
        "unavailable_order_paid",
    }:
        names = [
            "get_order",
            "get_shipment_summary",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_policy",
        ]
    elif topic in {"requested_full_refund", "refund_pending", "refund_failed"}:
        names = ["get_order", "get_payment_timeline", "get_refund_timeline", "get_policy"]
    elif topic in {"duplicate_charge", "payment_mismatch", "valid_split_payment"}:
        names = ["get_order", "get_order_items", "get_payment_timeline", "get_policy"]
    elif topic in {"canceled_order_paid", "unavailable_order_paid"}:
        names = [
            "get_order",
            "get_shipment_summary",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_policy",
        ]
    else:
        names = ["get_order", "get_shipment_summary", "get_payment_timeline", "get_policy"]
    return evidence.refs_for(*names)[:20]


def _supported_issues(
    facts: CaseFacts,
    shipment_verdict: str,
    payment_verdict: str,
    outstanding: Decimal | None,
    payments: list[dict[str, Any]],
) -> list[str]:
    """Identify issues independently so a lower priority claim is still assessed fairly."""
    issues: list[str] = []
    order_status = _dict(facts.order).get("order_status") or _dict(facts.shipment).get(
        "order_status"
    )
    if payment_verdict == "duplicate_capture":
        issues.append("duplicate_charge")
    if payment_verdict == "capture_mismatch":
        issues.append("payment_mismatch")
    latest = _latest_refund_events(facts.refund)
    if any(
        event.get("status") == "failed" or event.get("event_type") == "refund_failed"
        for event in latest
    ):
        issues.append("refund_failed")
    if any(
        event.get("status") == "pending" or event.get("event_type") == "refund_pending"
        for event in latest
    ):
        issues.append("refund_pending")
    if shipment_verdict == "seller_delay":
        issues.append("late_delivery_seller")
    if shipment_verdict == "logistics_delay":
        issues.append("late_delivery_logistics")
    if order_status in {"canceled", "unavailable"} and outstanding is not None and outstanding > 0:
        issues.append(f"{order_status}_order_paid")
    if len(payments) > 1 and payment_verdict not in {"duplicate_capture", "capture_mismatch"}:
        issues.append("valid_split_payment")
    return issues


def build_output(case: dict[str, Any], facts: CaseFacts, evidence: CaseEvidence) -> dict[str, Any]:
    claims = _records(_dict(case.get("customer_request")).get("claims"))
    claim_topics = {str(claim.get("topic")) for claim in claims if claim.get("topic")}
    seller_ids = _ids(facts.sellers + facts.items, "seller_id")
    shipment_verdict, late_seller_ids, timeline_complete = _shipment_finding(facts, seller_ids)
    payment_verdict, captured, refunded, outstanding, payments = _payment_finding(facts)
    supported_issues = _supported_issues(
        facts, shipment_verdict, payment_verdict, outstanding, payments
    )
    issue, confidence = _primary_issue(
        facts, claim_topics, shipment_verdict, payment_verdict, captured, outstanding, payments
    )
    first_topic = str(claims[0].get("topic", "")) if claims else ""
    if (
        facts.entity_status == "resolved"
        and first_topic in supported_issues
        and first_topic != "valid_split_payment"
        and issue != first_topic
    ):
        issue = first_topic
        confidence = 0.8
    secondary_issues = [supported for supported in supported_issues if supported != issue][:10]
    if issue != first_topic:
        confidence = min(confidence, 0.7)
    refund_dependent_issue = issue in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "refund_pending",
        "refund_failed",
    }
    if refund_dependent_issue and facts.refund_source == "inferred_absent":
        confidence = min(confidence, 0.7)
    elif refund_dependent_issue and facts.refund_source == "not_found":
        confidence = min(confidence, 0.82)
    status, action, refund, parties = _policy_decision(facts, issue, outstanding)
    if facts.policy is None and issue != "insufficient_evidence":
        confidence = min(confidence, 0.58)

    formatted_parties: list[dict[str, Any]] = []
    for party in parties:
        party_type = party.get("party_type", "unknown")
        party_id = party.get("party_id")
        responsible_sellers = late_seller_ids or seller_ids
        if (
            party_type == "seller"
            and (not party_id or str(party_id).startswith("seller-"))
            and len(responsible_sellers) == 1
        ):
            party_id = responsible_sellers[0]
        formatted_parties.append({"party_type": party_type, "party_id": party_id})
    if not formatted_parties:
        if issue in {"valid_split_payment", "unsupported_claim"}:
            fallback_type = "customer"
            fallback_id = None
        elif issue == "late_delivery_seller" and len(late_seller_ids) == 1:
            fallback_type = "seller"
            fallback_id = late_seller_ids[0]
        elif issue == "late_delivery_logistics":
            fallback_type = "logistics_provider"
            fallback_id = None
        elif facts.policy is not None and status != "needs_investigation":
            fallback_type = "platform"
            fallback_id = None
        else:
            fallback_type = "unknown"
            fallback_id = None
        formatted_parties = [{"party_type": fallback_type, "party_id": fallback_id}]

    assessed_claims = []
    for claim in claims[:5]:
        topic = str(claim.get("topic", ""))
        refs = _claim_refs(topic, issue, evidence)
        if facts.history_episode_selected:
            refs = list(dict.fromkeys([*evidence.refs_for("get_customer_history"), *refs]))[:20]
        if not refs or issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == "unsupported_claim":
            verdict = "unsupported"
        elif topic == "requested_full_refund":
            if outstanding is None:
                verdict = "insufficient_evidence"
            elif refund > 0 and refund >= outstanding:
                verdict = "supported"
            elif refund > 0:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
        elif topic in supported_issues or topic == issue:
            verdict = "supported"
        else:
            verdict = "unsupported"
        assessed_claims.append(
            {
                "claim_id": str(claim.get("claim_id", "")),
                "verdict": verdict,
                "confidence": round(
                    min(confidence, 0.85) if verdict != "insufficient_evidence" else 0.4, 2
                ),
                "evidence_refs": refs,
            }
        )

    order = _dict(facts.order)
    history = _dict(facts.customer_history)
    customer_id = order.get("customer_unique_id") or history.get("customer_unique_id")
    if not customer_id and facts.customer_history is not None:
        customer_id = case.get("customer_unique_id_hint")
    related_orders = _ids(_records(history.get("orders")), "order_id")

    payment_refs = _payment_references(payments, _records(_dict(facts.payment).get("events")))
    shipment = _dict(facts.shipment)
    shipment_refs = _shipment_references(shipment, facts.order_id)
    item_ids = _ids(facts.items, "order_item_id", "item_id")
    refund_float = float(refund)
    cause = ISSUE_TO_CAUSE[issue]
    causal_issues = [
        issue,
        *(secondary for secondary in secondary_issues if secondary != "valid_split_payment"),
    ][:5]

    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": secondary_issues,
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": facts.resolved_order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": shipment_refs,
        },
        "claim_assessments": assessed_claims,
        "entity_resolution": {
            "status": facts.entity_status,
            "resolved_order_ids": facts.resolved_order_ids,
            "rejected_candidates": facts.rejected_candidates,
            "confidence": facts.entity_confidence,
        },
        "customer_context": {
            "customer_unique_id": str(customer_id) if customer_id else None,
            "related_order_ids": related_orders,
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_seller_ids,
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": float(captured) if captured is not None else None,
            "refunded_total_brl": float(refunded) if refunded is not None else None,
            "refundable_total_brl": float(outstanding) if outstanding is not None else None,
        },
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": ISSUE_TO_CAUSE[supported], "rank": rank}
                for rank, supported in enumerate(causal_issues, start=1)
            ],
            "responsible_parties": formatted_parties[:5],
        },
        "evidence_refs": evidence.all_refs()[:30],
        "data_conflicts": facts.conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_float,
            "refund_lines": [
                {"reason_code": cause, "amount_brl": refund_float, "entity_id": facts.order_id}
            ]
            if refund > 0
            else [],
        },
        "resolution_actions": [action],
    }
