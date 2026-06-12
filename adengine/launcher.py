"""Phase 2 stub: launching a scored package via the Meta Marketing API.

Interfaces only — no implementation. The LaunchPlan schema maps Phase 1
artifacts (ScoredPackage / AdConcept ids) onto Meta Marketing API objects
(Campaign -> AdSet -> Ad).
"""
from __future__ import annotations

import enum

from pydantic import BaseModel, Field

from adengine.schemas import ScoredPackage, TargetAudienceHint


class CampaignObjective(str, enum.Enum):
    """Meta ODAX campaign objectives relevant to AdEngine."""

    OUTCOME_SALES = "OUTCOME_SALES"
    OUTCOME_LEADS = "OUTCOME_LEADS"
    OUTCOME_TRAFFIC = "OUTCOME_TRAFFIC"
    OUTCOME_ENGAGEMENT = "OUTCOME_ENGAGEMENT"
    OUTCOME_AWARENESS = "OUTCOME_AWARENESS"


class OptimizationGoal(str, enum.Enum):
    OFFSITE_CONVERSIONS = "OFFSITE_CONVERSIONS"
    LINK_CLICKS = "LINK_CLICKS"
    LEAD_GENERATION = "LEAD_GENERATION"
    LANDING_PAGE_VIEWS = "LANDING_PAGE_VIEWS"


class CampaignSpec(BaseModel):
    """Maps to a Meta Marketing API Campaign object."""

    name: str
    objective: CampaignObjective
    special_ad_categories: list[str] = Field(default_factory=list)
    daily_budget_cents: int | None = None  # set here for CBO, or per-adset below


class AdSetSpec(BaseModel):
    """Maps to a Meta Marketing API AdSet object."""

    name: str
    daily_budget_cents: int | None = None
    optimization_goal: OptimizationGoal
    targeting: TargetAudienceHint
    ad_ids: list[str] = Field(
        default_factory=list, description="AdConcept ids from the scored package"
    )


class AdCreativeSpec(BaseModel):
    """Maps to a Meta AdCreative: copy from AdConcept + an uploaded asset."""

    ad_concept_id: str
    page_id: str | None = None
    image_asset_url: str | None = None
    destination_url: str | None = None


class LaunchPlan(BaseModel):
    """Campaign -> adset -> ads mapping, derived from a ScoredPackage."""

    campaign: CampaignSpec
    adsets: list[AdSetSpec] = Field(default_factory=list)
    creatives: list[AdCreativeSpec] = Field(default_factory=list)
    source_package_url: str | None = None


class MetaLauncher:
    """Phase 2: pushes a LaunchPlan to the Meta Marketing API. Not implemented."""

    def __init__(self, access_token: str, ad_account_id: str) -> None:
        self.access_token = access_token
        self.ad_account_id = ad_account_id

    def build_plan(self, package: ScoredPackage, daily_budget_cents: int) -> LaunchPlan:
        """Derive a LaunchPlan from the launch-ready ads in a scored package."""
        raise NotImplementedError("Phase 2")

    def create_campaign(self, spec: CampaignSpec) -> str:
        """Create the campaign; returns the Meta campaign id."""
        raise NotImplementedError("Phase 2")

    def create_adset(self, campaign_id: str, spec: AdSetSpec) -> str:
        """Create an adset under a campaign; returns the Meta adset id."""
        raise NotImplementedError("Phase 2")

    def create_ad(self, adset_id: str, creative: AdCreativeSpec) -> str:
        """Create an ad + creative under an adset; returns the Meta ad id."""
        raise NotImplementedError("Phase 2")

    def launch(self, plan: LaunchPlan) -> dict[str, str]:
        """Execute the full plan; returns mapping of plan objects to Meta ids."""
        raise NotImplementedError("Phase 2")
