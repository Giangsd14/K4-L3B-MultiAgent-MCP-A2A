from __future__ import annotations

from typing import Any

from .decision import CaseFacts


def _events(source: dict[str, Any] | None) -> list[dict[str, Any]]:
    value = (source or {}).get("events")
    return [event for event in value if isinstance(event, dict)] if isinstance(value, list) else []


def _repeated_payment_row(source: dict[str, Any] | None) -> bool:
    payments = (source or {}).get("payments")
    if not isinstance(payments, list):
        return False
    seen: set[tuple[str, str, str, str]] = set()
    for row in payments:
        if (
            not isinstance(row, dict)
            or row.get("payment_value") is None
            or row.get("payment_sequential") in (None, "")
            or row.get("payment_type") in (None, "")
        ):
            continue
        signature = tuple(
            str(row.get(key, ""))
            for key in (
                "payment_sequential",
                "payment_type",
                "payment_installments",
                "payment_value",
            )
        )
        if signature in seen:
            return True
        seen.add(signature)
    return False


def verify_semantics(output: dict[str, Any], facts: CaseFacts) -> None:
    """Check that high-impact verdicts have an observed source-level signal.

    This gate intentionally does not reuse the classifiers in decision.py. A
    classifier regression should fail here before it reaches a submission.
    """
    case_id = output["case_id"]
    issue = output["assessment"]["primary_issue"]
    payment_verdict = output["payment_analysis"]["verdict"]
    shipment_verdict = output["shipment_analysis"]["verdict"]
    payment_event_types = {event.get("event_type") for event in _events(facts.payment)}
    shipment_events = _events(facts.shipment)
    refund_events = _events(facts.refund)

    if (
        payment_verdict == "capture_mismatch"
        and "reconciliation_mismatch" not in payment_event_types
    ):
        raise ValueError(f"{case_id}: payment mismatch lacks payment-timeline evidence")
    if (
        payment_verdict == "duplicate_capture"
        and "duplicate_charge" not in payment_event_types
        and not _repeated_payment_row(facts.payment)
    ):
        raise ValueError(f"{case_id}: duplicate capture lacks payment-timeline evidence")
    if issue == "payment_mismatch" and payment_verdict != "capture_mismatch":
        raise ValueError(f"{case_id}: payment issue contradicts payment analysis")
    if issue == "duplicate_charge" and payment_verdict != "duplicate_capture":
        raise ValueError(f"{case_id}: duplicate issue contradicts payment analysis")

    late_actors = {
        event.get("actor")
        for event in shipment_events
        if event.get("event_type") == "delivered_late"
    }
    if shipment_verdict == "seller_delay" and "seller" not in late_actors:
        raise ValueError(f"{case_id}: seller delay lacks shipment evidence")
    if shipment_verdict == "logistics_delay" and "logistics_provider" not in late_actors:
        raise ValueError(f"{case_id}: logistics delay lacks shipment evidence")
    if issue == "late_delivery_seller" and shipment_verdict != "seller_delay":
        raise ValueError(f"{case_id}: seller issue contradicts shipment analysis")
    if issue == "late_delivery_logistics" and shipment_verdict != "logistics_delay":
        raise ValueError(f"{case_id}: logistics issue contradicts shipment analysis")

    if issue in {"refund_pending", "refund_failed"}:
        wanted = "pending" if issue == "refund_pending" else "failed"
        if not any(
            event.get("status") == wanted or event.get("event_type") == f"refund_{wanted}"
            for event in refund_events
        ):
            raise ValueError(f"{case_id}: {issue} lacks refund-timeline evidence")
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        wanted_status = "canceled" if issue == "canceled_order_paid" else "unavailable"
        actual_status = (facts.order or {}).get("order_status") or (facts.shipment or {}).get(
            "order_status"
        )
        if actual_status != wanted_status:
            raise ValueError(f"{case_id}: {issue} contradicts order status")
        if (output["payment_analysis"]["refundable_total_brl"] or 0) <= 0:
            raise ValueError(f"{case_id}: {issue} lacks unrefunded payment")
    if issue == "valid_split_payment":
        payments = (facts.payment or {}).get("payments")
        if not isinstance(payments, list) or len(payments) < 2:
            raise ValueError(f"{case_id}: split payment lacks multiple payment records")
        if payment_event_types.intersection({"duplicate_charge", "reconciliation_mismatch"}):
            raise ValueError(f"{case_id}: split payment has an explicit payment anomaly")

    if (
        output["assessment"]["case_status"] == "no_action"
        and output["financial_resolution"]["recommended_refund_brl"] > 0
    ):
        raise ValueError(f"{case_id}: no-action case recommends a refund")
