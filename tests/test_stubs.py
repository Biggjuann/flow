from adengine.launcher import CampaignObjective, CampaignSpec, LaunchPlan
from adengine.optimizer import OptimizationAction, PerformanceSnapshot


def test_launch_plan_schema_round_trips():
    plan = LaunchPlan(
        campaign=CampaignSpec(name="Acme — Q3", objective=CampaignObjective.OUTCOME_SALES)
    )
    assert LaunchPlan.model_validate_json(plan.model_dump_json()) == plan


def test_optimizer_action_enum_and_snapshot_defaults():
    assert {a.value for a in OptimizationAction} == {
        "kill_ad",
        "shift_budget",
        "refresh_creative",
        "expand_audience",
        "alert_human",
    }
    snapshot = PerformanceSnapshot(
        ad_id="ad_01", window_start="2026-06-01", window_end="2026-06-07"
    )
    assert snapshot.impressions == 0
