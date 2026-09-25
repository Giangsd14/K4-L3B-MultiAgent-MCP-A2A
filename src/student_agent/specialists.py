"""Scoped MCP investigation helpers for the L3B workflow.

The tool catalog, rather than the customer's claims, controls which tools and
arguments can be used. Facts keep the server's evidence envelope intact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp.shared.exceptions import MCPError

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


@dataclass(frozen=True)
class EvidenceFact:
    case_id: str
    domain: str
    tool_name: str
    evidence_ref: str
    result_hash: str
    data: Any
    warnings: tuple[str, ...] = ()
    queried_order_id: str | None = None


@dataclass(frozen=True)
class IdentityResolution:
    status: str
    resolved_order_ids: tuple[str, ...]
    rejected_candidates: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class InvestigationResult:
    identity: IdentityResolution
    facts: tuple[EvidenceFact, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def facts_for(self, domain: str) -> tuple[EvidenceFact, ...]:
        return tuple(fact for fact in self.facts if fact.domain == domain)

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fact.evidence_ref for fact in self.facts))


@dataclass(frozen=True)
class _Tool:
    name: str
    schema: Mapping[str, Any]

    @property
    def properties(self) -> Mapping[str, Any]:
        value = self.schema.get("properties")
        return value if isinstance(value, Mapping) else {}

    def arguments(self, context: Mapping[str, str]) -> dict[str, str] | None:
        """Return declared, available args, or decline an unsupported call."""
        if "case_id" not in self.properties:
            return None
        required = self.schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(key, str) for key in required):
            return None
        if any(key != "case_id" and key not in context for key in required):
            return None
        return {
            key: value
            for key, value in context.items()
            if key != "case_id" and key in self.properties and isinstance(value, str) and value
        }


def _catalog_tools(catalog: Mapping[str, Any] | Sequence[Any]) -> tuple[_Tool, ...]:
    entries: Sequence[Any]
    if isinstance(catalog, Mapping):
        entries = tuple(catalog.values())
    elif isinstance(catalog, Sequence) and not isinstance(catalog, (str, bytes)):
        entries = catalog
    else:
        return ()
    tools: list[_Tool] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        schema = entry.get("inputSchema", entry.get("input_schema"))
        if isinstance(name, str) and name and isinstance(schema, Mapping):
            tools.append(_Tool(name, schema))
    return tuple(tools)


def _purpose_score(name: str, purpose: str) -> int:
    name = name.lower()
    if purpose == "order":
        if "order" not in name or any(
            word in name for word in ("item", "payment", "refund", "ship", "delivery", "history")
        ):
            return 0
        if name == "get_order":
            return 100
        return 80 if "lookup" in name or "search" in name else 60
    terms = {
        "item": ("item",),
        "product": ("product",),
        "seller": ("seller",),
        "shipment": ("shipment", "shipping", "delivery", "tracking"),
        "payment": ("payment", "capture", "transaction"),
        "refund": ("refund",),
        "customer": ("customer",),
        "policy": ("policy",),
    }
    if not any(word in name for word in terms[purpose]):
        return 0
    if purpose == "payment" and "refund" in name:
        return 0
    if purpose == "item" and "order" not in name:
        return 0
    if purpose == "customer" and "history" in name:
        return 100
    if purpose == "shipment" and "shipment" in name:
        return 100
    if purpose == "payment" and "timeline" in name:
        return 100
    if purpose == "product" and "context" in name:
        return 100
    if purpose == "seller" and "sellers" in name:
        return 100
    return 80 if name.startswith(("get_", "list_")) else 60


def _choose_tool(
    tools: tuple[_Tool, ...], purpose: str, context: Mapping[str, str]
) -> tuple[_Tool, dict[str, str]] | None:
    ranked: list[tuple[int, _Tool, dict[str, str]]] = []
    for tool in tools:
        score = _purpose_score(tool.name, purpose)
        if not score:
            continue
        args = tool.arguments(context)
        if args is None:
            continue
        # A domain tool must be scoped to an entity or a policy version.
        identifiers = {
            "policy": ("policy_version",),
            "customer": ("customer_unique_id",),
            "product": ("order_id", "product_id"),
            "seller": ("order_id", "seller_id"),
        }.get(purpose, ("order_id",))
        if not any(identifier in args for identifier in identifiers):
            continue
        ranked.append((score, tool, args))
    if not ranked:
        return None
    ranked.sort(key=lambda entry: (-entry[0], entry[1].name))
    _, tool, args = ranked[0]
    return tool, args


def _values_for_key(data: Any, key: str) -> set[str]:
    """Read identifiers from nested tool data without interpreting claims."""
    result: set[str] = set()
    if isinstance(data, Mapping):
        value = data.get(key)
        if isinstance(value, str) and value:
            result.add(value)
        for child in data.values():
            if isinstance(child, (Mapping, list, tuple)):
                result.update(_values_for_key(child, key))
    elif isinstance(data, (list, tuple)):
        for child in data:
            result.update(_values_for_key(child, key))
    return result


def _is_missing(data: Any) -> bool:
    if data is None:
        return True
    if not isinstance(data, Mapping):
        return False
    if data.get("found") is False or data.get("exists") is False:
        return True
    status = data.get("status")
    return isinstance(status, str) and status in {"not_found", "missing"}


def _not_found_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "not found" in message or "not_found" in message or "404" in message


class SpecialistInvestigator:
    def __init__(
        self,
        case: dict[str, Any],
        gateway: EvidenceGateway,
        trace: TraceWriter,
        catalog: Mapping[str, Any] | Sequence[Any],
        priority_domains: Sequence[str] = (),
    ) -> None:
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case_id is required for specialist investigation")
        self.case = case
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self.tools = _catalog_tools(catalog)
        # Claim topics can change investigation order, never tool scope or truth.
        self.priority_domains = tuple(
            domain
            for domain in dict.fromkeys(priority_domains)
            if domain in {"shipment", "payment", "refund"}
        )
        self.facts: list[EvidenceFact] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self._seen_refs: set[str] = set()

    def _dispatch(self, target: str, decision_code: str) -> None:
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=target,
        )
        self.trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor="coordinator",
            target=target,
            decision_code=decision_code,
        )

    async def _call(
        self, purpose: str, context: Mapping[str, str]
    ) -> tuple[EvidenceFact | None, bool]:
        selected = _choose_tool(self.tools, purpose, context)
        if selected is None:
            self.warnings.append(
                f"No discovered {purpose} tool accepts the available scoped arguments"
            )
            return None, False
        tool, args = selected
        try:
            evidence = await self.gateway.call(tool.name, case_id=self.case_id, **args)
        except (RuntimeError, ValueError, OSError, TimeoutError, httpx2.HTTPError, MCPError) as exc:
            if _not_found_error(exc):
                return None, True
            self.errors.append(f"{purpose} tool {tool.name} failed ({type(exc).__name__})")
            return None, False
        if not isinstance(evidence, Mapping):
            self.errors.append(f"{purpose} tool {tool.name} returned no evidence object")
            return None, False
        ref = evidence.get("evidence_ref")
        domain = evidence.get("domain")
        result_hash = evidence.get("result_hash")
        if (
            not isinstance(ref, str)
            or not ref
            or not isinstance(domain, str)
            or not isinstance(result_hash, str)
            or "data" not in evidence
        ):
            self.errors.append(f"{purpose} tool {tool.name} returned an incomplete envelope")
            return None, False
        raw_warnings = evidence.get("warnings", [])
        warnings = tuple(raw_warnings) if isinstance(raw_warnings, list) else ()
        fact = EvidenceFact(
            case_id=self.case_id,
            domain=domain,
            tool_name=tool.name,
            evidence_ref=ref,
            result_hash=result_hash,
            data=evidence["data"],
            warnings=warnings,
            queried_order_id=args.get("order_id"),
        )
        if ref not in self._seen_refs:
            # A response is consumed when inspected for identity or downstream facts.
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=f"{purpose}-agent",
                tool_name=tool.name,
                evidence_refs=[ref],
            )
            self.facts.append(fact)
            self._seen_refs.add(ref)
        return fact, _is_missing(fact.data)

    async def run(self) -> InvestigationResult:
        request = self.case.get("customer_request")
        claimed = request.get("claimed_order_id") if isinstance(request, Mapping) else None
        candidates = self.case.get("candidate_order_ids", [])
        order_ids: list[str] = []
        for value in (claimed, *(candidates if isinstance(candidates, list) else [])):
            if isinstance(value, str) and value and value not in order_ids:
                order_ids.append(value)
        if len(order_ids) > 8:
            self.warnings.append(
                "Candidate order list exceeds eight; remaining candidates were not queried"
            )
        unqueried = order_ids[8:]
        order_ids = order_ids[:8]
        verified: list[str] = []
        rejected: list[str] = []
        unknown: list[str] = list(unqueried)
        order_facts: dict[str, EvidenceFact] = {}
        for order_id in order_ids:
            fact, missing = await self._call("order", {"order_id": order_id})
            if missing:
                rejected.append(order_id)
            elif fact is None:
                unknown.append(order_id)
            elif order_id in _values_for_key(fact.data, "order_id"):
                verified.append(order_id)
                order_facts[order_id] = fact
            elif _values_for_key(fact.data, "order_id"):
                rejected.append(order_id)
                self.warnings.append(
                    f"Order lookup returned a different order for candidate {order_id}"
                )
            else:
                unknown.append(order_id)
                self.warnings.append(f"Order lookup did not identify candidate {order_id}")

        if len(verified) == 1:
            identity = IdentityResolution(
                "resolved", tuple(verified), tuple(rejected), 0.95 if not unknown else 0.75
            )
        elif verified:
            identity = IdentityResolution("ambiguous", tuple(verified), tuple(rejected), 0.4)
        elif unknown or not order_ids:
            identity = IdentityResolution("ambiguous", (), tuple(rejected), 0.0)
        else:
            identity = IdentityResolution("not_found", (), tuple(rejected), 0.0)
        if identity.status != "resolved":
            return self._result(identity)

        order_id = verified[0]
        order_data = order_facts[order_id].data
        context: dict[str, str] = {"order_id": order_id}
        customer_ids = _values_for_key(order_data, "customer_unique_id")
        hinted = self.case.get("customer_unique_id_hint")
        if customer_ids:
            context["customer_unique_id"] = sorted(customer_ids)[0]
            if isinstance(hinted, str) and hinted and hinted not in customer_ids:
                self.warnings.append("Customer hint differs from authoritative order evidence")
        elif isinstance(hinted, str) and hinted:
            context["customer_unique_id"] = hinted
            self.warnings.append("Customer history lookup uses an unverified input hint")
        for key in ("customer_id", "policy_version"):
            values = _values_for_key(order_data, key)
            if values:
                context[key] = sorted(values)[0]
        policy_version = self.case.get("policy_version")
        if isinstance(policy_version, str) and policy_version:
            context["policy_version"] = policy_version

        scope = self.case.get("investigation_scope")
        scope = scope if isinstance(scope, Mapping) else {}
        self._dispatch("order-item-agent", "COLLECT_ORDER_AND_ITEM_FACTS")
        item_fact, _ = await self._call("item", context)
        dispatched: set[str] = set()
        for domain in dict.fromkeys((*self.priority_domains, "shipment", "payment", "refund")):
            if domain == "shipment" and "shipment-agent" not in dispatched:
                self._dispatch("shipment-agent", "INVESTIGATE_SHIPMENT")
                dispatched.add("shipment-agent")
            if domain in {"payment", "refund"} and "payment-agent" not in dispatched:
                self._dispatch("payment-agent", "INVESTIGATE_PAYMENT_AND_REFUND")
                dispatched.add("payment-agent")
            await self._call(domain, context)
        if scope.get("include_customer_history") is True and "customer_unique_id" in context:
            self._dispatch("customer-agent", "FETCH_CUSTOMER_HISTORY")
            await self._call("customer", context)
        if scope.get("include_product_context") is True:
            product_ids = (
                sorted(_values_for_key(item_fact.data, "product_id"))
                if item_fact is not None
                else []
            )
            seller_ids = (
                sorted(_values_for_key(item_fact.data, "seller_id"))
                if item_fact is not None
                else []
            )
            self._dispatch("product-agent", "FETCH_ORDER_PRODUCT_CONTEXT")
            for purpose, field, identifiers in (
                ("product", "product_id", product_ids),
                ("seller", "seller_id", seller_ids),
            ):
                order_scoped = _choose_tool(self.tools, purpose, context)
                if order_scoped is not None and "order_id" in order_scoped[1]:
                    await self._call(purpose, context)
                else:
                    for identifier in identifiers[:3]:
                        await self._call(purpose, {**context, field: identifier})
                    if len(identifiers) > 3:
                        self.warnings.append(f"{purpose} lookup limited to three IDs")
        if "policy_version" in context:
            self._dispatch("policy-agent", "FETCH_APPLICABLE_POLICY")
            await self._call("policy", context)
        return self._result(identity)

    def _result(self, identity: IdentityResolution) -> InvestigationResult:
        return InvestigationResult(
            identity, tuple(self.facts), tuple(self.errors), tuple(self.warnings)
        )


async def investigate(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    catalog: Mapping[str, Any] | Sequence[Any] | None = None,
    *,
    priority_domains: Sequence[str] = (),
) -> InvestigationResult:
    """Investigate one case with catalog-declared, case-scoped MCP calls."""
    if catalog is None:
        catalog_method = getattr(gateway, "tool_catalog", None)
        catalog = await catalog_method() if callable(catalog_method) else {}
    return await SpecialistInvestigator(case, gateway, trace, catalog, priority_domains).run()
