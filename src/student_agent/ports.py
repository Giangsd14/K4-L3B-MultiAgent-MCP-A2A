from __future__ import annotations

from typing import Any, Protocol


class UnrecordedEvidenceCall(RuntimeError):
    """A replay requested evidence that was not captured in its source run."""


class EvidenceClient(Protocol):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]: ...
