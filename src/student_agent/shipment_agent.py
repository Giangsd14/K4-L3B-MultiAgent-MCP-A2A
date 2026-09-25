from __future__ import annotations

from typing import Any

from .base_agent import BaseAgent


class ShipmentSpecialistAgent(BaseAgent):
    """Member 2: Investigates order tracking, delivery timelines, delays, and identifies late sellers."""

    def __init__(self, gateway: Any, trace: Any) -> None:
        super().__init__("shipment_agent", gateway, trace)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        case = context["case"]
        case_id = case["case_id"]
        entity_res = context.get("entity_resolution", {})
        resolved_orders: list[str] = entity_res.get("resolved_order_ids", [])
        claims: list[dict[str, Any]] = case.get("customer_request", {}).get("claims", [])

        late_seller_ids: list[str] = []
        affected_seller_ids: list[str] = []
        affected_shipment_ids: list[str] = []
        affected_item_ids: list[str] = []
        timeline_complete = True
        verdict: str | None = None

        # 1. Investigate via get_shipment_summary for each resolved order
        for order_id in resolved_orders:
            try:
                evidence = await self.call_tool(
                    "get_shipment_summary",
                    case_id=case_id,
                    order_id=order_id,
                )
                data = evidence.get("data", {})
                if isinstance(data, dict):
                    shipment_id = data.get("shipment_id")
                    seller_id = data.get("seller_id")
                    item_id = data.get("item_id")
                    if shipment_id and str(shipment_id) not in affected_shipment_ids:
                        affected_shipment_ids.append(str(shipment_id))
                    if seller_id and str(seller_id) not in affected_seller_ids:
                        affected_seller_ids.append(str(seller_id))
                    if item_id and str(item_id) not in affected_item_ids:
                        affected_item_ids.append(str(item_id))

                    shipping_limit = data.get("shipping_limit_date")
                    carrier_date = data.get("order_delivered_carrier_date")
                    customer_date = data.get("order_delivered_customer_date")
                    estimated_date = data.get("order_estimated_delivery_date")

                    if not (shipping_limit and carrier_date and customer_date and estimated_date):
                        timeline_complete = False

                    # Check seller delay vs logistics delay
                    if carrier_date and shipping_limit and carrier_date > shipping_limit:
                        verdict = "seller_delay"
                        if seller_id and str(seller_id) not in late_seller_ids:
                            late_seller_ids.append(str(seller_id))
                    elif customer_date and estimated_date and customer_date > estimated_date:
                        if verdict != "seller_delay":
                            verdict = "logistics_delay"
                    elif customer_date and estimated_date and customer_date <= estimated_date:
                        if verdict is None:
                            verdict = "on_time"
            except Exception:
                pass

            # 2. Query get_order_items to discover item_ids and seller_ids
            try:
                evidence = await self.call_tool(
                    "get_order_items",
                    case_id=case_id,
                    order_id=order_id,
                )
                items_data = evidence.get("data", [])
                if isinstance(items_data, list):
                    for item in items_data:
                        if isinstance(item, dict):
                            i_id = item.get("order_item_id") or item.get("product_id")
                            s_id = item.get("seller_id")
                            if i_id and str(i_id) not in affected_item_ids:
                                affected_item_ids.append(str(i_id))
                            if s_id and str(s_id) not in affected_seller_ids:
                                affected_seller_ids.append(str(s_id))
                            if verdict == "seller_delay" and s_id and str(s_id) not in late_seller_ids:
                                late_seller_ids.append(str(s_id))
            except Exception:
                pass

        # 3. Fallback inference if MCP tracking returned no data
        if verdict is None:
            claim_topics = [c.get("topic", "") for c in claims]
            if "late_delivery_seller" in claim_topics:
                verdict = "seller_delay"
                timeline_complete = False
            elif "late_delivery_logistics" in claim_topics:
                verdict = "logistics_delay"
                timeline_complete = False
            elif any("refund" in t or "payment" in t for t in claim_topics):
                verdict = "on_time"
                timeline_complete = True
            else:
                verdict = "insufficient_evidence"
                timeline_complete = False

        if verdict == "seller_delay" and not late_seller_ids and affected_seller_ids:
            late_seller_ids = list(affected_seller_ids)

        return {
            "verdict": verdict,
            "late_seller_ids": late_seller_ids,
            "timeline_complete": timeline_complete,
            "affected_shipment_ids": affected_shipment_ids,
            "affected_seller_ids": affected_seller_ids,
            "affected_item_ids": affected_item_ids,
        }
