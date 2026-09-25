from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .decision import CaseFacts


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _event_time(event: dict[str, Any]) -> datetime | None:
    for key in ("event_at", "occurred_at", "created_at", "timestamp"):
        when = _time(event.get(key))
        if when is not None:
            return when
    return None


def _money(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _episode_candidates(case: dict[str, Any], facts: CaseFacts) -> list[dict[str, Any]]:
    opened = _time(case.get("opened_at"))
    history = facts.customer_history or {}
    if opened is None or facts.order_id is None:
        return []
    return [
        row
        for row in _rows(history.get("orders"))
        if row.get("order_id") == facts.order_id
        and (purchase := _time(row.get("order_purchase_timestamp"))) is not None
        and purchase <= opened
    ]


def _captures_near(payment: dict[str, Any] | None, purchase: datetime) -> list[dict[str, Any]]:
    return [
        event
        for event in _rows((payment or {}).get("events"))
        if event.get("event_type") == "captured"
        and (when := _event_time(event)) is not None
        and timedelta(0) <= when - purchase <= timedelta(days=1)
    ]


def _select_payment_rows(
    payment: dict[str, Any], captures: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    amounts = Counter(
        amount for event in captures if (amount := _money(event.get("amount_brl"))) is not None
    )
    selected: list[dict[str, Any]] = []
    for row in _rows(payment.get("payments")):
        amount = _money(row.get("payment_value", row.get("amount_brl")))
        if amount is not None and amounts[amount] > 0:
            selected.append(row)
            amounts[amount] -= 1
    return selected if selected and not any(amounts.values()) else []


def _filter_refund(facts: CaseFacts, captures: list[dict[str, Any]]) -> None:
    if facts.refund is None:
        return
    component_amounts = {
        amount for event in captures if (amount := _money(event.get("amount_brl"))) is not None
    }
    total = sum(
        (amount for event in captures if (amount := _money(event.get("amount_brl"))) is not None),
        Decimal(0),
    )
    facts.refund = {
        **facts.refund,
        "events": [
            event
            for event in _rows(facts.refund.get("events"))
            if (amount := _money(event.get("amount_brl"))) is None
            or amount in component_amounts
            or amount == total
        ],
    }


def _apply_episode(
    facts: CaseFacts, episode: dict[str, Any], captures: list[dict[str, Any]]
) -> None:
    payment = facts.payment
    if payment is None:
        return
    selected_payments = _select_payment_rows(payment, captures)
    if not selected_payments:
        return

    facts.order = {**(facts.order or {}), **episode}
    facts.history_episode_selected = True
    if facts.shipment is not None:
        shipment = facts.shipment
        delivered = _time(episode.get("order_delivered_customer_date"))
        events = [
            event
            for event in _rows(shipment.get("events"))
            if delivered is not None
            and (when := _event_time(event)) is not None
            and abs(when - delivered) <= timedelta(days=1)
        ]
        facts.shipment = {
            **shipment,
            "order_status": episode.get("order_status"),
            "delivered_customer_at": episode.get("order_delivered_customer_date"),
            "estimated_delivery_at": episode.get("order_estimated_delivery_date"),
            "events": events,
        }
    target_ids = {id(event) for event in captures}
    facts.payment = {
        **payment,
        "payments": selected_payments,
        "events": [
            event
            for event in _rows(payment.get("events"))
            if event.get("event_type") == "captured" and id(event) in target_ids
        ],
    }
    _filter_refund(facts, captures)


def _split_captures(payment: dict[str, Any]) -> list[dict[str, Any]]:
    """Find one two-method payment cohort among mixed, colliding order rows."""
    rows = _rows(payment.get("payments"))
    if len(rows) < 3:
        return []
    pairs = [
        (first, second)
        for first in rows
        for second in rows
        if first is not second
        and str(first.get("payment_sequential")) == "1"
        and str(second.get("payment_sequential")) == "2"
        and first.get("payment_type") != second.get("payment_type")
        and (amount := _money(first.get("payment_value"))) is not None
        and amount == _money(second.get("payment_value"))
    ]
    if len(pairs) != 1:
        return []
    amount = _money(pairs[0][0].get("payment_value"))
    captures = [
        event
        for event in _rows(payment.get("events"))
        if event.get("event_type") == "captured" and _money(event.get("amount_brl")) == amount
    ]
    return captures if len(captures) == 2 else []


def reconcile_claim_episode(case: dict[str, Any], facts: CaseFacts) -> None:
    """Bind conflicting history and timeline rows to one supported transaction.

    A reused order ID can merge two purchases in the gateway response. Only
    reconcile when the claim and independently timestamped captures identify
    a unique cohort; otherwise retain the original facts.
    """
    claims = _rows((case.get("customer_request") or {}).get("claims"))
    topic = claims[0].get("topic") if claims else None
    candidates = _episode_candidates(case, facts)
    if (
        not candidates
        or len(_rows((facts.customer_history or {}).get("orders"))) < 2
        or facts.payment is None
    ):
        return

    if topic in {"late_delivery_seller", "late_delivery_logistics"}:
        actor = "seller" if topic == "late_delivery_seller" else "logistics_provider"
        late_events = [
            event
            for event in _rows((facts.shipment or {}).get("events"))
            if event.get("event_type") == "delivered_late" and event.get("actor") == actor
        ]
        matches = [
            row
            for row in candidates
            if (delivered := _time(row.get("order_delivered_customer_date"))) is not None
            and (estimated := _time(row.get("order_estimated_delivery_date"))) is not None
            and delivered > estimated
            and any(
                (when := _event_time(event)) is not None
                and abs(when - delivered) <= timedelta(days=1)
                for event in late_events
            )
        ]
        if len(matches) == 1:
            purchase = _time(matches[0].get("order_purchase_timestamp"))
            captures = _captures_near(facts.payment, purchase) if purchase else []
            if captures:
                _apply_episode(facts, matches[0], captures)
        return

    if topic == "refund_pending":
        pending = [
            event
            for event in _rows((facts.refund or {}).get("events"))
            if event.get("status") == "pending" or event.get("event_type") == "refund_pending"
        ]
        if len(pending) != 1:
            return
        amount = _money(pending[0].get("amount_brl"))
        matched_captures = [
            event
            for event in _rows(facts.payment.get("events"))
            if event.get("event_type") == "captured"
            and amount is not None
            and _money(event.get("amount_brl")) == amount
        ]
        if len(matched_captures) != 1:
            return
        capture_time = _event_time(matched_captures[0])
        matches = [
            row
            for row in candidates
            if capture_time is not None
            and (purchase := _time(row.get("order_purchase_timestamp"))) is not None
            and timedelta(0) <= capture_time - purchase <= timedelta(days=1)
        ]
        if len(matches) == 1:
            purchase = _time(matches[0].get("order_purchase_timestamp"))
            captures = _captures_near(facts.payment, purchase) if purchase else []
            if len(captures) == 1:
                _apply_episode(facts, matches[0], captures)
        return

    if topic == "valid_split_payment":
        captures = _split_captures(facts.payment)
        if not captures:
            return
        when = _event_time(captures[0])
        matches = [
            row
            for row in candidates
            if when is not None
            and (purchase := _time(row.get("order_purchase_timestamp"))) is not None
            and timedelta(0) <= when - purchase <= timedelta(days=1)
        ]
        if len(matches) == 1:
            _apply_episode(facts, matches[0], captures)
        else:
            selected_payments = _select_payment_rows(facts.payment, captures)
            if selected_payments:
                facts.payment = {
                    **facts.payment,
                    "payments": selected_payments,
                    "events": captures,
                }
                _filter_refund(facts, captures)
        return

    if topic == "unsupported_claim":
        matches = [
            row
            for row in candidates
            if (delivered := _time(row.get("order_delivered_customer_date"))) is not None
            and (estimated := _time(row.get("order_estimated_delivery_date"))) is not None
            and delivered <= estimated
        ]
    elif topic == "canceled_order_paid":
        matches = [row for row in candidates if row.get("order_status") == "canceled"]
    else:
        return
    if len(matches) != 1:
        return
    purchase = _time(matches[0].get("order_purchase_timestamp"))
    if purchase is None:
        return
    captures = _captures_near(facts.payment, purchase)
    if captures:
        _apply_episode(facts, matches[0], captures)
