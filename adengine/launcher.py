"""Phase 2: push launch-ready ads to Meta via the Marketing (Graph) API.

Safety model: every object (campaign, adset, ads) is created with
status=PAUSED. Nothing spends money until a human reviews and activates the
campaign in Ads Manager.

v1 scope:
- One campaign (OUTCOME_TRAFFIC) + one adset (LINK_CLICKS / IMPRESSIONS) +
  one link ad per launch-ready concept. Sales/Leads objectives need a pixel /
  lead form and land in a later phase.
- Creatives are link ads pointing at the brand's site; Meta scrapes the link
  preview image. Uploading dedicated creative images is a follow-up.
- Interest targeting is resolved best-effort via the adinterest search API;
  unmatched interests are skipped.
"""
from __future__ import annotations

import enum
import json
import os
import re

import httpx
from pydantic import BaseModel, Field

from adengine.schemas import AdConcept, ScoredPackage, TargetAudienceHint, Verdict

GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v21.0"


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
    optimization_goal: OptimizationGoal = OptimizationGoal.LINK_CLICKS
    targeting: TargetAudienceHint = Field(default_factory=TargetAudienceHint)
    countries: list[str] = Field(default_factory=lambda: ["US"])
    ad_ids: list[str] = Field(
        default_factory=list, description="AdConcept ids from the scored package"
    )


class AdCreativeSpec(BaseModel):
    """Maps to a Meta AdCreative: copy from an AdConcept + a destination link."""

    ad_concept_id: str
    message: str
    headline: str
    description: str
    cta_type: str
    destination_url: str


class LaunchPlan(BaseModel):
    """Campaign -> adset -> ads mapping, derived from a ScoredPackage."""

    campaign: CampaignSpec
    adsets: list[AdSetSpec] = Field(default_factory=list)
    creatives: list[AdCreativeSpec] = Field(default_factory=list)
    source_package_url: str | None = None


class LaunchResult(BaseModel):
    campaign_id: str
    adset_ids: list[str] = Field(default_factory=list)
    ad_ids: dict[str, str] = Field(
        default_factory=dict, description="AdConcept id -> Meta ad id"
    )
    status: str = "PAUSED"
    notes: list[str] = Field(default_factory=list)


class MetaAPIError(RuntimeError):
    pass


class MetaConfigError(RuntimeError):
    pass


def _parse_age_range(age_range: str) -> tuple[int, int]:
    match = re.match(r"\s*(\d{2})\s*[-–]\s*(\d{2})", age_range or "")
    if not match:
        return 18, 65
    low, high = int(match.group(1)), int(match.group(2))
    low, high = max(18, min(low, 65)), max(18, min(high, 65))
    return (low, high) if low <= high else (high, low)


def build_plan(
    package: ScoredPackage,
    daily_budget_cents: int,
    destination_url: str,
    country: str = "US",
) -> LaunchPlan:
    """Derive a LaunchPlan from the launch-ready ads in a scored package."""
    ready = [s for s in package.scored_ads if s.verdict is Verdict.launch_ready]
    if not ready:
        raise MetaConfigError("No launch-ready ads in this package — nothing to launch.")
    ready.sort(key=lambda s: s.composite, reverse=True)
    ads: list[AdConcept] = [s.ad for s in ready]

    # Merge targeting hints across the launch-ready ads.
    interests: list[str] = []
    age_lows: list[int] = []
    age_highs: list[int] = []
    for ad in ads:
        hint = ad.target_audience_hint
        interests.extend(i for i in hint.interests if i not in interests)
        low, high = _parse_age_range(hint.age_range)
        age_lows.append(low)
        age_highs.append(high)

    merged = TargetAudienceHint(
        interests=interests[:10],
        age_range=f"{min(age_lows)}-{max(age_highs)}",
        geo_hint=country,
    )

    name = f"AdEngine — {package.business_name}"
    return LaunchPlan(
        campaign=CampaignSpec(name=name, objective=CampaignObjective.OUTCOME_TRAFFIC),
        adsets=[
            AdSetSpec(
                name=f"{name} — adset 1",
                daily_budget_cents=daily_budget_cents,
                targeting=merged,
                countries=[country],
                ad_ids=[ad.id for ad in ads if ad.id],
            )
        ],
        creatives=[
            AdCreativeSpec(
                ad_concept_id=ad.id or "",
                message=(
                    ad.primary_text
                    if ad.primary_text.startswith(ad.hook)
                    else f"{ad.hook}\n\n{ad.primary_text}"
                ),
                headline=ad.headline,
                description=ad.description,
                cta_type=ad.cta_button.value,
                destination_url=destination_url,
            )
            for ad in ads
        ],
        source_package_url=package.source_url,
    )


class MetaLauncher:
    """Pushes a LaunchPlan to the Meta Marketing API (all objects PAUSED)."""

    def __init__(
        self,
        access_token: str,
        ad_account_id: str,
        page_id: str,
        api_version: str = DEFAULT_API_VERSION,
        client: httpx.Client | None = None,
    ) -> None:
        self.access_token = access_token
        self.ad_account_id = (
            ad_account_id if ad_account_id.startswith("act_") else f"act_{ad_account_id}"
        )
        self.page_id = page_id
        self._client = client or httpx.Client(
            base_url=f"{GRAPH_BASE}/{api_version}", timeout=30.0
        )

    @classmethod
    def from_env(cls, client: httpx.Client | None = None) -> "MetaLauncher":
        token = os.environ.get("META_ACCESS_TOKEN")
        account = os.environ.get("META_AD_ACCOUNT_ID")
        page = os.environ.get("META_PAGE_ID")
        missing = [
            name
            for name, value in (
                ("META_ACCESS_TOKEN", token),
                ("META_AD_ACCOUNT_ID", account),
                ("META_PAGE_ID", page),
            )
            if not value
        ]
        if missing:
            raise MetaConfigError(
                f"Meta launch is not configured — set {', '.join(missing)} "
                "in the environment (Railway service variables)."
            )
        return cls(token, account, page, client=client)

    # ------------------------------------------------------------- graph calls

    def _post(self, path: str, payload: dict) -> dict:
        response = self._client.post(
            path, data={**payload, "access_token": self.access_token}
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400 or "error" in body:
            error = body.get("error", {})
            raise MetaAPIError(
                error.get("error_user_msg")
                or error.get("message")
                or f"Meta API error (HTTP {response.status_code})"
            )
        return body

    def _get(self, path: str, params: dict) -> dict:
        response = self._client.get(
            path, params={**params, "access_token": self.access_token}
        )
        if response.status_code >= 400:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def resolve_interests(self, names: list[str]) -> list[dict]:
        """Best-effort interest name -> Meta interest id via adinterest search."""
        resolved = []
        for name in names:
            body = self._get("/search", {"type": "adinterest", "q": name, "limit": 1})
            data = body.get("data") or []
            if data:
                resolved.append({"id": data[0]["id"], "name": data[0].get("name", name)})
        return resolved

    def create_campaign(self, spec: CampaignSpec) -> str:
        body = self._post(
            f"/{self.ad_account_id}/campaigns",
            {
                "name": spec.name,
                "objective": spec.objective.value,
                "status": "PAUSED",
                "special_ad_categories": json.dumps(spec.special_ad_categories),
            },
        )
        return body["id"]

    def create_adset(self, campaign_id: str, spec: AdSetSpec) -> str:
        age_min, age_max = _parse_age_range(spec.targeting.age_range)
        targeting: dict = {
            "geo_locations": {"countries": spec.countries},
            "age_min": age_min,
            "age_max": age_max,
        }
        interests = self.resolve_interests(spec.targeting.interests)
        if interests:
            targeting["flexible_spec"] = [{"interests": interests}]

        body = self._post(
            f"/{self.ad_account_id}/adsets",
            {
                "name": spec.name,
                "campaign_id": campaign_id,
                "daily_budget": spec.daily_budget_cents,
                "optimization_goal": spec.optimization_goal.value,
                "billing_event": "IMPRESSIONS",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "targeting": json.dumps(targeting),
                "status": "PAUSED",
            },
        )
        return body["id"]

    def create_ad(self, adset_id: str, creative: AdCreativeSpec) -> str:
        creative_body = self._post(
            f"/{self.ad_account_id}/adcreatives",
            {
                "name": f"AdEngine creative {creative.ad_concept_id}",
                "object_story_spec": json.dumps(
                    {
                        "page_id": self.page_id,
                        "link_data": {
                            "link": creative.destination_url,
                            "message": creative.message,
                            "name": creative.headline,
                            "description": creative.description,
                            "call_to_action": {
                                "type": creative.cta_type,
                                "value": {"link": creative.destination_url},
                            },
                        },
                    }
                ),
            },
        )
        ad_body = self._post(
            f"/{self.ad_account_id}/ads",
            {
                "name": f"AdEngine {creative.ad_concept_id}",
                "adset_id": adset_id,
                "creative": json.dumps({"creative_id": creative_body["id"]}),
                "status": "PAUSED",
            },
        )
        return ad_body["id"]

    def launch(self, plan: LaunchPlan) -> LaunchResult:
        """Execute the full plan. Everything is created PAUSED."""
        campaign_id = self.create_campaign(plan.campaign)
        result = LaunchResult(
            campaign_id=campaign_id,
            notes=[
                "All objects created with status=PAUSED — review and activate in Ads Manager.",
                "Creatives use the link preview image; add dedicated images in Ads Manager for better CTR.",
            ],
        )
        creatives_by_id = {c.ad_concept_id: c for c in plan.creatives}
        for adset_spec in plan.adsets:
            adset_id = self.create_adset(campaign_id, adset_spec)
            result.adset_ids.append(adset_id)
            for concept_id in adset_spec.ad_ids:
                creative = creatives_by_id.get(concept_id)
                if creative is None:
                    continue
                result.ad_ids[concept_id] = self.create_ad(adset_id, creative)
        return result
