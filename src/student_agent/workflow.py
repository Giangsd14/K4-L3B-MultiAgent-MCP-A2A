from __future__ import annotations

from typing import Any

from .coordinator import MultiAgentCoordinator
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent investigation workflow for a given case."""
    coordinator = MultiAgentCoordinator(gateway, trace)
    return await coordinator.coordinate(case)
