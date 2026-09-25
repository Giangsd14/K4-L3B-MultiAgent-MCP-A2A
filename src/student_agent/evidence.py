from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


@dataclass(frozen=True)
class EvidenceResult:
    tool_name: str
    data: Any = None
    evidence_ref: str | None = None
    error: str | None = None
    error_type: str | None = None

    @property
    def available(self) -> bool:
        return self.evidence_ref is not None


class CaseEvidence:
    """One case's MCP results and their audit-backed references.

    A failed lookup is cached as well: an optional 404 must not become a second
    audited call when a specialist later asks for the same evidence.
    """

    def __init__(self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], EvidenceResult] = {}
        self._inflight: dict[
            tuple[str, tuple[tuple[str, str], ...]], asyncio.Task[EvidenceResult]
        ] = {}
        self._results: list[EvidenceResult] = []

    async def fetch(self, tool_name: str, *, actor: str, **arguments: str) -> EvidenceResult:
        key = (tool_name, tuple(sorted(arguments.items())))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_uncached(tool_name, actor, arguments))
            self._inflight[key] = task
        try:
            return await task
        finally:
            if task.done():
                self._inflight.pop(key, None)

    async def _fetch_uncached(
        self, tool_name: str, actor: str, arguments: dict[str, str]
    ) -> EvidenceResult:
        key = (tool_name, tuple(sorted(arguments.items())))

        try:
            response = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
            result = EvidenceResult(
                tool_name=tool_name,
                data=response["data"],
                evidence_ref=response["evidence_ref"],
            )
        except Exception as exc:
            result = EvidenceResult(
                tool_name=tool_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        self._cache[key] = result
        self._results.append(result)
        if result.evidence_ref:
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[result.evidence_ref],
            )
        return result

    def refs_for(self, *tool_names: str) -> list[str]:
        selected = set(tool_names)
        return list(
            dict.fromkeys(
                result.evidence_ref
                for result in self._results
                if result.tool_name in selected and result.evidence_ref
            )
        )

    def all_refs(self) -> list[str]:
        return list(
            dict.fromkeys(result.evidence_ref for result in self._results if result.evidence_ref)
        )
