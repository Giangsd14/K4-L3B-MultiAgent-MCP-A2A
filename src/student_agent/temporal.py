from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .decision import CaseFacts


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _money(row: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            amount = Decimal(str(value))
            if amount.is_finite() and amount >= 0:
                return amount.quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError, TypeError):
            continue
    return None


def _event_time(row: dict[str, Any]) -> datetime | None:
    for key in ("event_at", "occurred_at", "created_at", "timestamp"):
        value = _time(row.get(key))
        if value is not None:
            return value
    return None


def _closest_items(items: list[dict[str, Any]], purchase: datetime) -> list[dict[str, Any]]:
    selected: dict[str, tuple[float, int, dict[str, Any]]] = {}
    for index, row in enumerate(items):
        identity = str(row.get("order_item_id") or row.get("item_id") or f"row-{index}")
        limit = _time(row.get("shipping_limit_date"))
        distance = abs((limit - purchase).total_seconds()) if limit else float("inf")
        if identity not in selected or distance < selected[identity][0]:
            selected[identity] = (distance, index, row)
    return [value[2] for value in sorted(selected.values(), key=lambda value: value[1])]


def _first_payment_sequence(payments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A reset to sequence 1 starts another payment cohort for the same order ID."""
    if not payments:
        return []
    selected: list[dict[str, Any]] = []
    expected = 1
    for row in payments:
        try:
            sequential = int(str(row["payment_sequential"]))
        except (KeyError, TypeError, ValueError):
            return payments
        if sequential != expected:
            break
        selected.append(row)
        expected += 1
    return selected or payments


def normalize_temporal_facts(facts: CaseFacts) -> None:
    """Keep records consistent with the verified order's purchase and delivery timeline.

    The gateway may return several rows with the same order/item ID from distinct
    periods. The order timestamp anchors the transaction; raw envelopes remain
    available for source-level verification and duplicate-charge detection.
    """
    order = facts.order or {}
    purchase = _time(order.get("order_purchase_timestamp"))
    if purchase is None:
        return
    approved = _time(order.get("order_approved_at")) or purchase
    facts.payment_raw = facts.payment
    facts.shipment_raw = facts.shipment
    facts.refund_raw = facts.refund
    facts.items = _closest_items(facts.items, purchase)

    if facts.payment is not None:
        payment = facts.payment
        events = _rows(payment.get("events"))
        captures = [event for event in events if event.get("event_type") == "captured"]
        payment_rows = _rows(payment.get("payments"))
        selected_payments = _first_payment_sequence(payment_rows)
        selected_captures: list[dict[str, Any]] = []
        unused = list(captures)
        for row in selected_payments:
            amount = _money(row, "payment_value", "amount_brl")
            match = next((event for event in unused if _money(event, "amount_brl") == amount), None)
            if match is not None:
                selected_captures.append(match)
                unused.remove(match)
        if (
            selected_payments == payment_rows
            and captures
            and any("payment_sequential" not in row for row in payment_rows)
        ):
            near_captures = [
                event
                for event in captures
                if (when := _event_time(event)) is not None
                and abs(when - approved) <= timedelta(days=3)
            ]
            if near_captures:
                selected_captures = near_captures
                amounts = Counter(
                    amount
                    for event in selected_captures
                    if (amount := _money(event, "amount_brl")) is not None
                )
                selected_payments = []
                for row in payment_rows:
                    amount = _money(row, "payment_value", "amount_brl")
                    if amount is not None and amounts[amount] > 0:
                        selected_payments.append(row)
                        amounts[amount] -= 1
        selected_amounts = {
            amount
            for event in selected_captures
            if (amount := _money(event, "amount_brl")) is not None
        }
        selected_capture_ids = {id(event) for event in selected_captures}
        selected_events: list[dict[str, Any]] = []
        for event in events:
            if event.get("event_type") == "captured":
                if id(event) in selected_capture_ids:
                    selected_events.append(event)
                continue
            when = _event_time(event)
            amount = _money(event, "amount_brl")
            if when is not None and when < purchase:
                continue
            if amount is not None and selected_amounts and amount not in selected_amounts:
                continue
            selected_events.append(event)
        facts.payment = {**payment, "payments": selected_payments, "events": selected_events}

    if facts.shipment is not None:
        shipment = facts.shipment
        delivered = _time(shipment.get("delivered_customer_at"))
        selected_events = []
        for event in _rows(shipment.get("events")):
            when = _event_time(event)
            if when is not None and when < purchase:
                continue
            if (
                event.get("event_type") == "delivered_late"
                and shipment.get("delivered_customer_at") is None
            ):
                continue
            if (
                event.get("event_type") == "delivered_late"
                and when is not None
                and delivered is not None
                and abs(when - delivered) > timedelta(days=2)
            ):
                continue
            selected_events.append(event)
        facts.shipment = {**shipment, "events": selected_events}

    if facts.refund is not None:
        refund = facts.refund
        captures = _rows((facts.payment or {}).get("payments"))
        captured = [_money(row, "payment_value", "amount_brl") for row in captures]
        total = (
            sum(captured, Decimal(0))
            if captured and all(amount is not None for amount in captured)
            else None
        )
        selected_capture_amounts = {
            amount
            for row in _rows((facts.payment or {}).get("events"))
            if row.get("event_type") == "captured"
            and (amount := _money(row, "amount_brl")) is not None
        }
        selected_event_ids = {id(row) for row in _rows((facts.payment or {}).get("events"))}
        stale_capture_amounts = {
            amount
            for row in _rows((facts.payment_raw or {}).get("events"))
            if row.get("event_type") == "captured"
            and id(row) not in selected_event_ids
            and (amount := _money(row, "amount_brl")) is not None
        }
        selected_events = []
        for event in _rows(refund.get("events")):
            when = _event_time(event)
            amount = _money(event, "amount_brl")
            if when is not None and when < purchase:
                continue
            if total is not None and amount is not None and amount > total:
                continue
            if (
                amount is not None
                and amount in stale_capture_amounts
                and amount not in selected_capture_amounts
            ):
                continue
            selected_events.append(event)
        facts.refund = {**refund, "events": selected_events}
