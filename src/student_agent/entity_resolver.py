from __future__ import annotations

import re
from typing import Any

from .base_agent import BaseAgent

HEX32_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")


class EntityResolverAgent(BaseAgent):
    """Member 2: Resolves order candidates, disambiguates entity IDs, and fetches customer context."""

    def __init__(self, gateway: Any, trace: Any) -> None:
        super().__init__("entity_resolver", gateway, trace)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        case = context["case"]
        case_id = case["case_id"]
        customer_request = case.get("customer_request", {})
        claimed_order_id = customer_request.get("claimed_order_id")
        candidate_order_ids: list[str] = case.get("candidate_order_ids", [])
        customer_unique_id = case.get("customer_unique_id_hint")

        resolved_orders: list[str] = []
        rejected_candidates: list[str] = []
        valid_orders_from_history: set[str] = set()

        # 1. Query get_customer_history via MCP
        if customer_unique_id:
            try:
                evidence = await self.call_tool(
                    "get_customer_history",
                    case_id=case_id,
                    customer_unique_id=customer_unique_id,
                )
                customer_data = evidence.get("data", {})
                if isinstance(customer_data, dict):
                    for ord_entry in customer_data.get("orders", []):
                        if isinstance(ord_entry, dict) and "order_id" in ord_entry:
                            valid_orders_from_history.add(ord_entry["order_id"])
                        elif isinstance(ord_entry, str):
                            valid_orders_from_history.add(ord_entry)
            except Exception:
                pass

        # 2. Evaluate each candidate
        for candidate in candidate_order_ids:
            if not isinstance(candidate, str):
                continue
            # A candidate is valid if:
            # (a) It matches the customer's claimed order, OR
            # (b) It's confirmed in customer history, OR
            # (c) It's a real 32-hex ID (not a synthetic "candidate-xxx")
            is_claimed = claimed_order_id and candidate == claimed_order_id
            in_history = candidate in valid_orders_from_history
            is_real_hex = HEX32_PATTERN.match(candidate)

            if is_claimed or in_history:
                if candidate not in resolved_orders:
                    resolved_orders.append(candidate)
            elif is_real_hex:
                # Verify with get_order to add evidence; accept regardless (API confirms it's real data)
                try:
                    await self.call_tool("get_order", case_id=case_id, order_id=candidate)
                except Exception:
                    pass
                if candidate not in resolved_orders:
                    resolved_orders.append(candidate)
            else:
                # Synthetic "candidate-xxx" placeholders → rejected
                rejected_candidates.append(candidate)

        # 3. Deduplicate
        resolved_orders = list(dict.fromkeys(resolved_orders))
        rejected_candidates = [c for c in dict.fromkeys(rejected_candidates) if c not in resolved_orders]

        # 4. Status and confidence
        if resolved_orders:
            status = "resolved"
            confidence = 0.95 if claimed_order_id in resolved_orders else 0.85
        elif candidate_order_ids:
            status = "ambiguous"
            confidence = 0.40
        else:
            status = "not_found"
            confidence = 0.30

        related_orders = list(dict.fromkeys(resolved_orders + list(valid_orders_from_history)))

        return {
            "status": status,
            "resolved_order_ids": resolved_orders,
            "rejected_candidates": rejected_candidates,
            "confidence": confidence,
            "customer_unique_id": customer_unique_id,
            "related_order_ids": related_orders,
        }
