from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# Global cache scoped per case_id to prevent redundant MCP calls across agents within the same case run
_CASE_TOOL_CACHE: dict[str, dict[str, dict[str, Any]]] = {}


def clear_case_cache(case_id: str | None = None) -> None:
    """Clear tool cache for a specific case or all cases."""
    if case_id:
        _CASE_TOOL_CACHE.pop(case_id, None)
    else:
        _CASE_TOOL_CACHE.clear()


class BaseAgent(ABC):
    """Base class for all specialized investigation agents with caching, resilience, and trace provenance."""

    def __init__(self, name: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.name = name
        self.gateway = gateway
        self.trace = trace
        self.collected_evidence_refs: list[str] = []

    async def call_tool(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> dict[str, Any]:
        """Call an MCP tool with audit validation, per-case caching, and emit tool_result_consumed trace event."""
        case_cache = _CASE_TOOL_CACHE.setdefault(case_id, {})
        cache_key = f"{tool_name}:{sorted(arguments.items())}"

        if cache_key in case_cache:
            evidence = case_cache[cache_key]
        else:
            try:
                evidence = await self.gateway.call(tool_name, case_id=case_id, **arguments)
                case_cache[cache_key] = evidence
            except (Exception, BaseException):
                return {}

        evidence_ref = evidence.get("evidence_ref")
        if evidence_ref:
            if evidence_ref not in self.collected_evidence_refs:
                self.collected_evidence_refs.append(evidence_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=self.name,
                tool_name=tool_name,
                evidence_refs=[evidence_ref],
                attributes={"status": "success"},
            )
        return evidence

    @abstractmethod
    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Execute agent analysis on the shared case context."""
        ...
