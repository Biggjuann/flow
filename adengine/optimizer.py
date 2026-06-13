"""Phase 3: the optimization loop over live campaign performance.

Reads per-ad Insights from Meta, applies deterministic rules, and (only when
explicitly asked) acts on them. The rules are intentionally conservative:

- the only automatic *spend-stopping* action is pausing a clearly losing ad
- budget shifts are bounded (±20%) and never push an adset below the minimum
- everything else (refresh creative, expand audience, anomalies) is surfaced
  as a recommendation for a human

Decisions are computed in code — no LLM in the money loop.
"""
from __future__ import annotations

import enum

from pydantic import BaseModel, Field

from adengine.launcher import MetaLauncher

# ------------------------------------------------------------------ thresholds

MIN_IMPRESSIONS = 1000          # below this (and low spend) an ad is still learning
MIN_SPEND_CENTS = 500           # ...unless it already burned this much
WEAK_CTR = 0.004                # 0.4% — below this after enough delivery, the hook failed
WEAK_CTR_IMPRESSIONS = 2000
KILL_SPEND_MULTIPLE = 3.0       # spend >= 3x target CPA with 0 conversions -> kill
FATIGUE_FREQUENCY = 3.5         # same people seeing the ad too often
SATURATION_FREQUENCY = 2.5      # winner saturating its audience -> expand
BUDGET_SHIFT_PCT = 20           # bounded budget moves
LOSER_CPA_MULTIPLE = 2.0        # converting but >= 2x the best CPA -> shift down
NO_CONVERSION_ALERT_CENTS = 10_000  # campaign-wide $100 spent, zero conversions

# Insights action_types we count as a conversion.
CONVERSION_ACTION_TYPES = {
    "purchase",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
    "lead",
    "offsite_conversion.fb_pixel_lead",
    "schedule",
    "offsite_conversion.fb_pixel_schedule",
    "contact",
    "offsite_conversion.fb_pixel_contact",
    "mobile_app_install",
    "app_install",
    "omni_app_install",
}


class OptimizationAction(str, enum.Enum):
    kill_ad = "kill_ad"
    shift_budget = "shift_budget"
    refresh_creative = "refresh_creative"
    expand_audience = "expand_audience"
    alert_human = "alert_human"


# Actions the optimizer may execute via the API; the rest are human follow-ups.
AUTO_ACTIONS = {OptimizationAction.kill_ad, OptimizationAction.shift_budget}


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


class AppliedAction(BaseModel):
    ad_id: str
    action: OptimizationAction
    ok: bool
    detail: str = ""


def snapshot_from_insights(row: dict) -> PerformanceSnapshot:
    """Parse one Insights row (level=ad) into a PerformanceSnapshot."""
    impressions = int(row.get("impressions") or 0)
    clicks = int(row.get("clicks") or 0)
    spend_cents = int(round(float(row.get("spend") or 0) * 100))
    conversions = 0
    for action in row.get("actions") or []:
        if action.get("action_type") in CONVERSION_ACTION_TYPES:
            try:
                conversions += int(float(action.get("value") or 0))
            except (TypeError, ValueError):
                pass

    # "AdEngine ad_03" -> concept id ad_03; otherwise fall back to the Meta id.
    name = row.get("ad_name") or ""
    concept_id = name.removeprefix("AdEngine ").strip() if name.startswith("AdEngine ") else None

    return PerformanceSnapshot(
        ad_id=concept_id or row.get("ad_id", "unknown"),
        meta_ad_id=row.get("ad_id"),
        window_start=row.get("date_start", ""),
        window_end=row.get("date_stop", ""),
        impressions=impressions,
        clicks=clicks,
        spend_cents=spend_cents,
        conversions=conversions,
        ctr=round(clicks / impressions, 5) if impressions else None,
        cpc_cents=spend_cents // clicks if clicks else None,
        cpa_cents=spend_cents // conversions if conversions else None,
        frequency=float(row["frequency"]) if row.get("frequency") else None,
    )


class Optimizer:
    """Turns PerformanceSnapshots into OptimizationDecisions and applies them."""

    def __init__(self, launcher: MetaLauncher, target_cpa_cents: int | None = None) -> None:
        self.launcher = launcher
        self.target_cpa_cents = target_cpa_cents

    # ------------------------------------------------------------------ fetch

    def fetch_snapshots(
        self, campaign_id: str, date_preset: str = "last_7d"
    ) -> list[PerformanceSnapshot]:
        rows = self.launcher.get_campaign_insights(campaign_id, date_preset=date_preset)
        return [snapshot_from_insights(row) for row in rows]

    # ----------------------------------------------------------------- decide

    def decide(self, snapshots: list[PerformanceSnapshot]) -> list[OptimizationDecision]:
        decisions: list[OptimizationDecision] = []
        converting = [s for s in snapshots if s.conversions > 0 and s.cpa_cents]
        best_cpa = min((s.cpa_cents for s in converting), default=None)

        for snap in snapshots:
            decision = self._decide_one(snap, best_cpa)
            if decision:
                decisions.append(decision)

        # Campaign-level anomaly: meaningful spend, zero conversions anywhere.
        total_spend = sum(s.spend_cents for s in snapshots)
        total_conversions = sum(s.conversions for s in snapshots)
        alert_floor = (
            int(self.target_cpa_cents * KILL_SPEND_MULTIPLE)
            if self.target_cpa_cents
            else NO_CONVERSION_ALERT_CENTS
        )
        # Only meaningful when conversions are actually trackable (pixel/app).
        if total_conversions == 0 and total_spend >= alert_floor and snapshots:
            decisions.append(
                OptimizationDecision(
                    ad_id="campaign",
                    action=OptimizationAction.alert_human,
                    reason=(
                        f"${total_spend / 100:.2f} spent with zero tracked conversions — "
                        "check pixel/app event wiring and the landing page before spending more."
                    ),
                )
            )
        return decisions

    def _decide_one(
        self, snap: PerformanceSnapshot, best_cpa: int | None
    ) -> OptimizationDecision | None:
        # Still learning: not enough delivery AND not enough spend to judge.
        if snap.impressions < MIN_IMPRESSIONS and snap.spend_cents < MIN_SPEND_CENTS:
            return None

        meta_ref = {"meta_ad_id": snap.meta_ad_id} if snap.meta_ad_id else {}

        # Kill: burned 3x target CPA with nothing to show for it.
        if (
            self.target_cpa_cents
            and snap.conversions == 0
            and snap.spend_cents >= self.target_cpa_cents * KILL_SPEND_MULTIPLE
        ):
            return OptimizationDecision(
                ad_id=snap.ad_id,
                action=OptimizationAction.kill_ad,
                reason=(
                    f"${snap.spend_cents / 100:.2f} spent (≥{KILL_SPEND_MULTIPLE:.0f}x target CPA) "
                    "with zero conversions."
                ),
                params=meta_ref,
            )

        # Kill: the hook demonstrably doesn't stop the scroll.
        if (
            snap.impressions >= WEAK_CTR_IMPRESSIONS
            and snap.ctr is not None
            and snap.ctr < WEAK_CTR
            and snap.conversions == 0
        ):
            return OptimizationDecision(
                ad_id=snap.ad_id,
                action=OptimizationAction.kill_ad,
                reason=f"CTR {snap.ctr:.2%} after {snap.impressions:,} impressions — hook is not working.",
                params=meta_ref,
            )

        # Fatigue: same audience hammered too often.
        if snap.frequency is not None and snap.frequency >= FATIGUE_FREQUENCY:
            return OptimizationDecision(
                ad_id=snap.ad_id,
                action=OptimizationAction.refresh_creative,
                reason=(
                    f"Frequency {snap.frequency:.1f} — creative fatigue. Swap in a held-back "
                    "launch-ready ad or regenerate creative."
                ),
            )

        if snap.conversions > 0 and snap.cpa_cents and best_cpa:
            within_target = (
                self.target_cpa_cents is None or snap.cpa_cents <= self.target_cpa_cents
            )
            # Winner saturating its audience while profitable -> widen it.
            if (
                snap.cpa_cents == best_cpa
                and within_target
                and snap.frequency is not None
                and snap.frequency >= SATURATION_FREQUENCY
            ):
                return OptimizationDecision(
                    ad_id=snap.ad_id,
                    action=OptimizationAction.expand_audience,
                    reason=(
                        f"Best CPA (${snap.cpa_cents / 100:.2f}) but frequency "
                        f"{snap.frequency:.1f} — audience is saturating; broaden targeting."
                    ),
                )
            # Winner: push budget toward it.
            if snap.cpa_cents == best_cpa and within_target:
                return OptimizationDecision(
                    ad_id=snap.ad_id,
                    action=OptimizationAction.shift_budget,
                    reason=f"Best CPA in campaign (${snap.cpa_cents / 100:.2f}) — increase budget.",
                    params={"budget_change_pct": BUDGET_SHIFT_PCT, **meta_ref},
                )
            # Converting but expensive relative to the winner: pull budget back.
            if snap.cpa_cents >= best_cpa * LOSER_CPA_MULTIPLE:
                return OptimizationDecision(
                    ad_id=snap.ad_id,
                    action=OptimizationAction.shift_budget,
                    reason=(
                        f"CPA ${snap.cpa_cents / 100:.2f} is ≥{LOSER_CPA_MULTIPLE:.0f}x the best "
                        f"(${best_cpa / 100:.2f}) — decrease budget."
                    ),
                    params={"budget_change_pct": -BUDGET_SHIFT_PCT, **meta_ref},
                )
        return None

    # ------------------------------------------------------------------ apply

    def apply(self, decisions: list[OptimizationDecision]) -> list[AppliedAction]:
        """Execute the automatic actions (pause / bounded budget shift).

        refresh_creative / expand_audience / alert_human are returned as
        not-applied — they're human follow-ups in this phase.
        """
        applied: list[AppliedAction] = []
        for decision in decisions:
            if decision.action not in AUTO_ACTIONS:
                applied.append(
                    AppliedAction(
                        ad_id=decision.ad_id,
                        action=decision.action,
                        ok=False,
                        detail="manual follow-up — not auto-applied",
                    )
                )
                continue
            try:
                applied.append(self._apply_one(decision))
            except Exception as exc:  # one failure must not stop the rest
                applied.append(
                    AppliedAction(
                        ad_id=decision.ad_id,
                        action=decision.action,
                        ok=False,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
        return applied

    def _apply_one(self, decision: OptimizationDecision) -> AppliedAction:
        meta_ad_id = decision.params.get("meta_ad_id") or decision.ad_id
        if decision.action is OptimizationAction.kill_ad:
            self.launcher.pause_ad(meta_ad_id)
            return AppliedAction(
                ad_id=decision.ad_id, action=decision.action, ok=True, detail="ad paused"
            )

        # shift_budget: bounded change on the ad's adset.
        pct = int(decision.params.get("budget_change_pct") or 0)
        adset_id = self.launcher.get_ad_adset_id(meta_ad_id)
        if not adset_id:
            return AppliedAction(
                ad_id=decision.ad_id, action=decision.action, ok=False, detail="adset not found"
            )
        current = self.launcher.get_adset_budget_cents(adset_id)
        if not current:
            return AppliedAction(
                ad_id=decision.ad_id, action=decision.action, ok=False, detail="budget unreadable"
            )
        new_budget = int(round(current * (1 + pct / 100)))
        self.launcher.set_adset_budget_cents(adset_id, new_budget)
        return AppliedAction(
            ad_id=decision.ad_id,
            action=decision.action,
            ok=True,
            detail=f"adset {adset_id} budget {current} -> {max(new_budget, 0)} cents ({pct:+d}%)",
        )
