"""Phase 3 stub: the optimization loop over live campaign performance.

Interfaces only — no implementation.
"""
from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class OptimizationAction(str, enum.Enum):
    kill_ad = "kill_ad"
    shift_budget = "shift_budget"
    refresh_creative = "refresh_creative"
    expand_audience = "expand_audience"
    alert_human = "alert_human"


class PerformanceSnapshot(BaseModel):
    """One ad's performance over a reporting window, from the Meta Insights API."""

    ad_id: str = Field(description="AdConcept id (Phase 1) / Meta ad id (Phase 2+)")
    meta_ad_id: str | None = None
    window_start: str
    window_end: str
    impressions: int = 0
    clicks: int = 0
    spend_cents: int = 0
    conversions: int = 0
    ctr: float | None = None
    cpc_cents: int | None = None
    cpa_cents: int | None = None
    frequency: float | None = None


class OptimizationDecision(BaseModel):
    ad_id: str
    action: OptimizationAction
    reason: str
    params: dict = Field(default_factory=dict)


class Optimizer:
    """Phase 3: turns PerformanceSnapshots into OptimizationDecisions. Not implemented."""

    def fetch_snapshots(self, campaign_id: str) -> list[PerformanceSnapshot]:
        raise NotImplementedError("Phase 3")

    def decide(self, snapshots: list[PerformanceSnapshot]) -> list[OptimizationDecision]:
        raise NotImplementedError("Phase 3")

    def apply(self, decisions: list[OptimizationDecision]) -> None:
        raise NotImplementedError("Phase 3")
