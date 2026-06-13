import pytest

from adengine.optimizer import (
    OptimizationAction,
    Optimizer,
    PerformanceSnapshot,
    snapshot_from_insights,
)


def snap(ad_id="ad_01", **overrides):
    fields = dict(
        ad_id=ad_id,
        meta_ad_id=f"meta_{ad_id}",
        window_start="2026-06-06",
        window_end="2026-06-12",
        impressions=5000,
        clicks=50,
        spend_cents=2000,
        conversions=0,
        ctr=0.01,
        frequency=1.5,
    )
    fields.update(overrides)
    if fields["conversions"]:
        fields.setdefault("cpa_cents", fields["spend_cents"] // fields["conversions"])
    return PerformanceSnapshot(**fields)


# ---------------------------------------------------------------- parsing

def test_snapshot_from_insights_parses_row():
    row = {
        "ad_id": "120210000001",
        "ad_name": "AdEngine ad_03",
        "impressions": "4521",
        "clicks": "61",
        "spend": "18.43",
        "frequency": "2.1",
        "date_start": "2026-06-06",
        "date_stop": "2026-06-12",
        "actions": [
            {"action_type": "link_click", "value": "61"},
            {"action_type": "purchase", "value": "3"},
            {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "2"},
        ],
    }
    s = snapshot_from_insights(row)
    assert s.ad_id == "ad_03"  # concept id recovered from ad name
    assert s.meta_ad_id == "120210000001"
    assert s.impressions == 4521
    assert s.spend_cents == 1843
    assert s.conversions == 5  # purchase + pixel purchase; link_click ignored
    assert s.ctr == round(61 / 4521, 5)
    assert s.cpa_cents == 1843 // 5
    assert s.frequency == 2.1


# ---------------------------------------------------------------- decisions

class NoopLauncher:
    pass


def optimizer(target_cpa_cents=3000):
    return Optimizer(NoopLauncher(), target_cpa_cents=target_cpa_cents)


def test_learning_phase_is_left_alone():
    decisions = optimizer().decide([snap(impressions=400, spend_cents=200, clicks=4)])
    assert decisions == []


def test_kill_on_spend_without_conversions():
    # 3x $30 target = $90 spent, zero conversions
    decisions = optimizer().decide([snap(spend_cents=9000, conversions=0)])
    assert decisions[0].action is OptimizationAction.kill_ad
    assert decisions[0].params["meta_ad_id"] == "meta_ad_01"


def test_kill_on_weak_ctr():
    decisions = optimizer(target_cpa_cents=None).decide(
        [snap(impressions=3000, clicks=6, ctr=0.002, spend_cents=900)]
    )
    assert decisions[0].action is OptimizationAction.kill_ad
    assert "hook" in decisions[0].reason


def test_fatigue_triggers_refresh():
    decisions = optimizer(target_cpa_cents=None).decide(
        [snap(frequency=4.0, spend_cents=900)]
    )
    assert decisions[0].action is OptimizationAction.refresh_creative


def test_budget_shifts_toward_winner_and_away_from_loser():
    winner = snap("ad_01", conversions=4, spend_cents=4000)   # CPA $10
    loser = snap("ad_02", conversions=1, spend_cents=2500)    # CPA $25 >= 2x
    decisions = optimizer().decide([winner, loser])
    by_ad = {d.ad_id: d for d in decisions}
    assert by_ad["ad_01"].action is OptimizationAction.shift_budget
    assert by_ad["ad_01"].params["budget_change_pct"] == 20
    assert by_ad["ad_02"].action is OptimizationAction.shift_budget
    assert by_ad["ad_02"].params["budget_change_pct"] == -20


def test_saturated_winner_expands_audience():
    winner = snap("ad_01", conversions=4, spend_cents=4000, frequency=3.0)
    decisions = optimizer().decide([winner])
    assert decisions[0].action is OptimizationAction.expand_audience


def test_campaign_alert_on_spend_without_any_conversions():
    snaps = [snap("ad_01", spend_cents=5000), snap("ad_02", spend_cents=5000)]
    decisions = optimizer().decide(snaps)
    alert = [d for d in decisions if d.action is OptimizationAction.alert_human]
    assert len(alert) == 1
    assert alert[0].ad_id == "campaign"
    assert "pixel" in alert[0].reason


# ------------------------------------------------------------------- apply

class FakeGraphLauncher:
    def __init__(self):
        self.paused = []
        self.budgets = {"as_1": 1000}
        self.adset_of = {"meta_ad_01": "as_1"}

    def pause_ad(self, meta_ad_id):
        self.paused.append(meta_ad_id)

    def get_ad_adset_id(self, meta_ad_id):
        return self.adset_of.get(meta_ad_id)

    def get_adset_budget_cents(self, adset_id):
        return self.budgets.get(adset_id)

    def set_adset_budget_cents(self, adset_id, cents):
        self.budgets[adset_id] = cents


def test_apply_pauses_and_shifts():
    launcher = FakeGraphLauncher()
    opt = Optimizer(launcher, target_cpa_cents=3000)
    from adengine.optimizer import OptimizationDecision

    decisions = [
        OptimizationDecision(
            ad_id="ad_09",
            action=OptimizationAction.kill_ad,
            reason="r",
            params={"meta_ad_id": "meta_ad_09"},
        ),
        OptimizationDecision(
            ad_id="ad_01",
            action=OptimizationAction.shift_budget,
            reason="r",
            params={"budget_change_pct": 20, "meta_ad_id": "meta_ad_01"},
        ),
        OptimizationDecision(
            ad_id="ad_02", action=OptimizationAction.refresh_creative, reason="r"
        ),
    ]
    applied = opt.apply(decisions)

    assert launcher.paused == ["meta_ad_09"]
    assert launcher.budgets["as_1"] == 1200  # +20%
    by_ad = {a.ad_id: a for a in applied}
    assert by_ad["ad_09"].ok and by_ad["ad_01"].ok
    assert not by_ad["ad_02"].ok  # manual follow-up, never auto-applied
    assert "manual" in by_ad["ad_02"].detail


def test_apply_survives_individual_failures():
    class ExplodingLauncher(FakeGraphLauncher):
        def pause_ad(self, meta_ad_id):
            raise RuntimeError("graph down")

    from adengine.optimizer import OptimizationDecision

    opt = Optimizer(ExplodingLauncher())
    applied = opt.apply(
        [
            OptimizationDecision(
                ad_id="a", action=OptimizationAction.kill_ad, reason="r",
                params={"meta_ad_id": "m"},
            ),
            OptimizationDecision(
                ad_id="b", action=OptimizationAction.shift_budget, reason="r",
                params={"budget_change_pct": 20, "meta_ad_id": "meta_ad_01"},
            ),
        ]
    )
    assert not applied[0].ok and "graph down" in applied[0].detail
    assert applied[1].ok  # second action still ran
