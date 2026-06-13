"""Phase 2: push launch-ready ads to Meta via the Marketing (Graph) API.

Built to drive sales, not just clicks:

- **Conversion-optimized.** The campaign objective is chosen from the brand's
  conversion path. With a pixel (`META_PIXEL_ID`) configured, web campaigns
  optimize for the actual money event (Purchase / Lead / Schedule / Contact),
  so Meta's delivery finds buyers — not just cheap clicks. Without a pixel it
  falls back to Traffic.
- **iOS / app installs.** App Store / Play listings run as Meta **App
  Promotion** campaigns optimized for installs when `META_APP_ID` is set
  (otherwise Traffic to the store page).
- **Attribution.** Destination URLs are tagged (UTM for web, Apple `ct`
  campaign token for the App Store) so the client can prove what AdEngine sold.
- **Real creative.** If an image provider is configured, each ad gets a
  generated image uploaded to Meta; otherwise the link-preview image is used.

Safety: every object (campaign, adset, ads) is created with status=PAUSED.
Nothing spends until a human activates it in Ads Manager.
"""
from __future__ import annotations

import enum
import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from pydantic import BaseModel, Field

from adengine.images import ImageProvider, provider_from_env
from adengine.scraper import is_app_store_url
from adengine.schemas import (
    AdConcept,
    ConversionPath,
    ScoredPackage,
    TargetAudienceHint,
    Verdict,
)

GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v21.0"


class CampaignObjective(str, enum.Enum):
    """Meta ODAX campaign objectives relevant to AdEngine."""

    OUTCOME_SALES = "OUTCOME_SALES"
    OUTCOME_LEADS = "OUTCOME_LEADS"
    OUTCOME_TRAFFIC = "OUTCOME_TRAFFIC"
    OUTCOME_ENGAGEMENT = "OUTCOME_ENGAGEMENT"
    OUTCOME_AWARENESS = "OUTCOME_AWARENESS"
    OUTCOME_APP_PROMOTION = "OUTCOME_APP_PROMOTION"


class OptimizationGoal(str, enum.Enum):
    OFFSITE_CONVERSIONS = "OFFSITE_CONVERSIONS"
    LINK_CLICKS = "LINK_CLICKS"
    LEAD_GENERATION = "LEAD_GENERATION"
    LANDING_PAGE_VIEWS = "LANDING_PAGE_VIEWS"
    APP_INSTALLS = "APP_INSTALLS"


# conversion_path -> (objective, optimization_goal, pixel custom_event_type).
# Used only when a pixel is configured; otherwise we fall back to Traffic.
_CONVERSION_PLAN: dict[ConversionPath, tuple[CampaignObjective, OptimizationGoal, str]] = {
    ConversionPath.purchase: (CampaignObjective.OUTCOME_SALES, OptimizationGoal.OFFSITE_CONVERSIONS, "PURCHASE"),
    ConversionPath.lead_form: (CampaignObjective.OUTCOME_LEADS, OptimizationGoal.OFFSITE_CONVERSIONS, "LEAD"),
    ConversionPath.booking: (CampaignObjective.OUTCOME_LEADS, OptimizationGoal.OFFSITE_CONVERSIONS, "SCHEDULE"),
    ConversionPath.call: (CampaignObjective.OUTCOME_LEADS, OptimizationGoal.OFFSITE_CONVERSIONS, "CONTACT"),
}

_TRAFFIC = (CampaignObjective.OUTCOME_TRAFFIC, OptimizationGoal.LINK_CLICKS)
APP_CTAS = {"INSTALL_MOBILE_APP", "USE_APP", "PLAY_GAME", "DOWNLOAD"}


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
    billing_event: str = "IMPRESSIONS"
    promoted_object: dict | None = None  # pixel/app optimization target
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
    image_prompt: str = ""


class LaunchPlan(BaseModel):
    """Campaign -> adset -> ads mapping, derived from a ScoredPackage."""

    campaign: CampaignSpec
    adsets: list[AdSetSpec] = Field(default_factory=list)
    creatives: list[AdCreativeSpec] = Field(default_factory=list)
    source_package_url: str | None = None
    notes: list[str] = Field(default_factory=list)


class LaunchResult(BaseModel):
    campaign_id: str
    objective: str
    adset_ids: list[str] = Field(default_factory=list)
    ad_ids: dict[str, str] = Field(
        default_factory=dict, description="AdConcept id -> Meta ad id"
    )
    images_generated: int = 0
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


def _add_params(url: str, params: dict[str, str]) -> str:
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query.update(params)
    return urlunparse(parts._replace(query=urlencode(query)))


def tag_destination(url: str, campaign: str, content: str) -> str:
    """Append attribution params. Apple App Store gets a `ct` campaign token;
    everything else gets standard UTM params."""
    safe_campaign = re.sub(r"[^A-Za-z0-9_-]+", "-", campaign).strip("-")[:40] or "adengine"
    if is_app_store_url(url):
        # Apple App Analytics reads `ct` (campaign text) on App Store links.
        return _add_params(url, {"ct": f"{safe_campaign}-{content}"[:40]})
    return _add_params(
        url,
        {
            "utm_source": "facebook",
            "utm_medium": "paid_social",
            "utm_campaign": safe_campaign,
            "utm_content": content,
        },
    )


def build_plan(
    package: ScoredPackage,
    conversion_path: ConversionPath,
    daily_budget_cents: int,
    destination_url: str,
    country: str = "US",
    pixel_id: str | None = None,
    app_id: str | None = None,
) -> LaunchPlan:
    """Derive a conversion-optimized LaunchPlan from the launch-ready ads."""
    ready = [s for s in package.scored_ads if s.verdict is Verdict.launch_ready]
    if not ready:
        raise MetaConfigError("No launch-ready ads in this package — nothing to launch.")
    ready.sort(key=lambda s: s.composite, reverse=True)
    ads: list[AdConcept] = [s.ad for s in ready]
    notes: list[str] = []

    # ---- objective + optimization selection (the core sales lever) ----------
    promoted_object: dict | None = None
    is_app = conversion_path is ConversionPath.app_install or is_app_store_url(destination_url)

    if is_app:
        if app_id:
            objective = CampaignObjective.OUTCOME_APP_PROMOTION
            optimization = OptimizationGoal.APP_INSTALLS
            promoted_object = {"application_id": app_id, "object_store_url": destination_url}
        else:
            objective, optimization = _TRAFFIC
            notes.append(
                "Set META_APP_ID to run install-optimized App Promotion campaigns; "
                "running Traffic to the store page for now."
            )
        force_cta = "INSTALL_MOBILE_APP"
    elif pixel_id and conversion_path in _CONVERSION_PLAN:
        objective, optimization, event = _CONVERSION_PLAN[conversion_path]
        promoted_object = {"pixel_id": pixel_id, "custom_event_type": event}
        force_cta = None
        notes.append(f"Optimizing for {event} conversions on pixel {pixel_id}.")
    else:
        objective, optimization = _TRAFFIC
        force_cta = None
        if conversion_path in _CONVERSION_PLAN:
            notes.append(
                "Set META_PIXEL_ID to optimize for conversions; running Traffic for now."
            )

    # ---- merged targeting ---------------------------------------------------
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
    creatives = []
    for ad in ads:
        message = (
            ad.primary_text
            if ad.primary_text.startswith(ad.hook)
            else f"{ad.hook}\n\n{ad.primary_text}"
        )
        creatives.append(
            AdCreativeSpec(
                ad_concept_id=ad.id or "",
                message=message,
                headline=ad.headline,
                description=ad.description,
                cta_type=force_cta or ad.cta_button.value,
                destination_url=tag_destination(destination_url, name, ad.id or "ad"),
                image_prompt=ad.creative_direction.image_prompt,
            )
        )

    return LaunchPlan(
        campaign=CampaignSpec(name=name, objective=objective),
        adsets=[
            AdSetSpec(
                name=f"{name} — adset 1",
                daily_budget_cents=daily_budget_cents,
                optimization_goal=optimization,
                promoted_object=promoted_object,
                targeting=merged,
                countries=[country],
                ad_ids=[ad.id for ad in ads if ad.id],
            )
        ],
        creatives=creatives,
        source_package_url=package.source_url,
        notes=notes,
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
        image_provider: ImageProvider | None = None,
    ) -> None:
        self.access_token = access_token
        self.ad_account_id = (
            ad_account_id if ad_account_id.startswith("act_") else f"act_{ad_account_id}"
        )
        self.page_id = page_id
        self.image_provider = image_provider
        self.pixel_id = os.environ.get("META_PIXEL_ID") or None
        self.app_id = os.environ.get("META_APP_ID") or None
        self._client = client or httpx.Client(
            base_url=f"{GRAPH_BASE}/{api_version}", timeout=30.0
        )

    @classmethod
    def from_env(
        cls,
        client: httpx.Client | None = None,
        image_provider: ImageProvider | None = None,
    ) -> "MetaLauncher":
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
        return cls(
            token,
            account,
            page,
            client=client,
            image_provider=image_provider if image_provider is not None else provider_from_env(),
        )

    # ------------------------------------------------------------- graph calls

    def _post(self, path: str, payload: dict) -> dict:
        clean = {k: v for k, v in payload.items() if v is not None}
        response = self._client.post(path, data={**clean, "access_token": self.access_token})
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
        response = self._client.get(path, params={**params, "access_token": self.access_token})
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

    def upload_image(self, data: bytes, name: str) -> str | None:
        """Upload image bytes to the ad account; returns the image_hash."""
        response = self._client.post(
            f"/{self.ad_account_id}/adimages",
            data={"access_token": self.access_token},
            files={"filename": (f"{name}.png", data, "image/png")},
        )
        if response.status_code >= 400:
            return None
        images = (response.json() or {}).get("images") or {}
        for entry in images.values():
            if entry.get("hash"):
                return entry["hash"]
        return None

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

        return self._post(
            f"/{self.ad_account_id}/adsets",
            {
                "name": spec.name,
                "campaign_id": campaign_id,
                "daily_budget": spec.daily_budget_cents,
                "optimization_goal": spec.optimization_goal.value,
                "billing_event": spec.billing_event,
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "promoted_object": json.dumps(spec.promoted_object) if spec.promoted_object else None,
                "targeting": json.dumps(targeting),
                "status": "PAUSED",
            },
        )["id"]

    def create_ad(self, adset_id: str, creative: AdCreativeSpec, image_hash: str | None) -> str:
        link_data = {
            "link": creative.destination_url,
            "message": creative.message,
            "name": creative.headline,
            "description": creative.description,
            "call_to_action": {
                "type": creative.cta_type,
                "value": {"link": creative.destination_url},
            },
        }
        if image_hash:
            link_data["image_hash"] = image_hash

        creative_body = self._post(
            f"/{self.ad_account_id}/adcreatives",
            {
                "name": f"AdEngine creative {creative.ad_concept_id}",
                "object_story_spec": json.dumps(
                    {"page_id": self.page_id, "link_data": link_data}
                ),
            },
        )
        return self._post(
            f"/{self.ad_account_id}/ads",
            {
                "name": f"AdEngine {creative.ad_concept_id}",
                "adset_id": adset_id,
                "creative": json.dumps({"creative_id": creative_body["id"]}),
                "status": "PAUSED",
            },
        )["id"]

    def launch(self, plan: LaunchPlan) -> LaunchResult:
        """Execute the full plan. Everything is created PAUSED."""
        campaign_id = self.create_campaign(plan.campaign)
        result = LaunchResult(
            campaign_id=campaign_id,
            objective=plan.campaign.objective.value,
            notes=[*plan.notes, "All objects created PAUSED — review and activate in Ads Manager."],
        )
        creatives_by_id = {c.ad_concept_id: c for c in plan.creatives}

        for adset_spec in plan.adsets:
            adset_id = self.create_adset(campaign_id, adset_spec)
            result.adset_ids.append(adset_id)
            for concept_id in adset_spec.ad_ids:
                creative = creatives_by_id.get(concept_id)
                if creative is None:
                    continue
                image_hash = self._maybe_image(creative, result)
                result.ad_ids[concept_id] = self.create_ad(adset_id, creative, image_hash)

        if self.image_provider and not result.images_generated:
            result.notes.append("Image generation produced no usable images — used link previews.")
        elif not self.image_provider:
            result.notes.append(
                "No image provider configured — ads use the link-preview image. "
                "Set ADENGINE_IMAGE_PROVIDER=openai + OPENAI_API_KEY for generated creative."
            )
        return result

    def _maybe_image(self, creative: AdCreativeSpec, result: LaunchResult) -> str | None:
        if not self.image_provider or not creative.image_prompt:
            return None
        try:
            data = self.image_provider(creative.image_prompt)
            if not data:
                return None
            image_hash = self.upload_image(data, creative.ad_concept_id or "adengine")
            if image_hash:
                result.images_generated += 1
            return image_hash
        except Exception:
            return None  # creative falls back to link preview; never blocks a launch
