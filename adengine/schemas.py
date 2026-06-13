"""All Pydantic models for the AdEngine pipeline.

Every LLM call in the pipeline validates against one of these models via
structured output (forced tool use) — freeform text is never parsed.

Phase 1 models (BrandDNA, AdConcept, ScoredPackage) are designed so Phase 2
(Meta Marketing API launch) and Phase 3 (optimization loop) can build on them
without schema changes: AdConcept ids are stable handles, CTAButton maps to
Meta CTA types, and TargetAudienceHint maps onto Meta targeting specs.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- enums

class Category(str, enum.Enum):
    b2b = "b2b"
    b2c = "b2c"
    local = "local"
    ecom = "ecom"
    saas = "saas"


class ConversionPath(str, enum.Enum):
    lead_form = "lead_form"
    purchase = "purchase"
    booking = "booking"
    call = "call"
    app_install = "app_install"


class HookFramework(str, enum.Enum):
    pas = "pas"
    aida = "aida"
    curiosity_gap = "curiosity_gap"
    social_proof = "social_proof"
    before_after_bridge = "before_after_bridge"
    question_hook = "question_hook"
    contrarian = "contrarian"
    urgency_loss_aversion = "urgency_loss_aversion"
    founder_story = "founder_story"


class AdFormat(str, enum.Enum):
    static = "static"
    carousel = "carousel"


class CTAButton(str, enum.Enum):
    """Subset of Meta ad CTA types relevant to Phase 1."""

    LEARN_MORE = "LEARN_MORE"
    SHOP_NOW = "SHOP_NOW"
    SIGN_UP = "SIGN_UP"
    GET_QUOTE = "GET_QUOTE"
    BOOK_NOW = "BOOK_NOW"
    CONTACT_US = "CONTACT_US"
    SUBSCRIBE = "SUBSCRIBE"
    DOWNLOAD = "DOWNLOAD"
    GET_OFFER = "GET_OFFER"
    APPLY_NOW = "APPLY_NOW"
    INSTALL_MOBILE_APP = "INSTALL_MOBILE_APP"
    USE_APP = "USE_APP"
    PLAY_GAME = "PLAY_GAME"


class Verdict(str, enum.Enum):
    launch_ready = "launch_ready"
    blocked = "blocked"


# --------------------------------------------------------------- Step 1: Brand DNA

class ICP(BaseModel):
    who_its_for: str = Field(description="The ideal customer the site is clearly speaking to")
    pain_points: list[str] = Field(default_factory=list)
    desired_outcome: str


class Offer(BaseModel):
    description: str = Field(description="What is actually being sold")
    pricing_signal: str | None = Field(
        default=None, description='Pricing evidence from the site, e.g. "$49/mo", "free trial"'
    )
    cta: str | None = Field(default=None, description="Primary call to action on the site")
    conversion_path: ConversionPath


class Voice(BaseModel):
    tone: list[str] = Field(default_factory=list)
    words_they_use: list[str] = Field(default_factory=list)
    words_to_avoid: list[str] = Field(default_factory=list)


class Visual(BaseModel):
    colors: list[str] = Field(default_factory=list)
    style_descriptors: list[str] = Field(default_factory=list)


class Proof(BaseModel):
    testimonials_present: bool = False
    proof_points: list[str] = Field(default_factory=list)


class Positioning(BaseModel):
    one_liner: str
    differentiators: list[str] = Field(default_factory=list)


class BrandDNA(BaseModel):
    business_name: str
    what_they_sell: str
    category: Category
    icp: ICP
    offer: Offer
    voice: Voice
    visual: Visual
    proof: Proof
    positioning: Positioning
    gaps: list[str] = Field(
        default_factory=list,
        description="Anything missing from the site that weakens ad-readiness",
    )
    source_url: str | None = None  # set by the pipeline, not the LLM


# ------------------------------------------------------------ Step 2: Ad concepts

class CreativeDirection(BaseModel):
    image_prompt: str
    format: AdFormat = AdFormat.static
    visual_notes: str = ""


class TargetAudienceHint(BaseModel):
    interests: list[str] = Field(default_factory=list)
    age_range: str = Field(default="25-54", description='e.g. "25-44"')
    geo_hint: str = ""


class AdConcept(BaseModel):
    id: str | None = None  # assigned by the pipeline, not the LLM
    framework: HookFramework
    angle: str = Field(description="One-sentence strategic angle")
    hook: str = Field(max_length=125, description="First line of primary text, <125 chars")
    primary_text: str = Field(description="Meta-limits-aware body copy")
    headline: str = Field(max_length=40)
    description: str = Field(max_length=30)
    cta_button: CTAButton
    creative_direction: CreativeDirection
    target_audience_hint: TargetAudienceHint


class AdBatch(BaseModel):
    """Container schema for one generator call."""

    ads: list[AdConcept]


# ----------------------------------------------------------- Step 3: Scoring

class AdReview(BaseModel):
    """One blind review from the adversarial scorer. Each criterion is 1-10."""

    ad_id: str
    hook_strength: int = Field(ge=1, le=10)
    message_clarity: int = Field(ge=1, le=10)
    audience_fit: int = Field(ge=1, le=10)
    offer_match: int = Field(ge=1, le=10)
    creative_quality: int = Field(ge=1, le=10)
    policy_risk: int = Field(ge=1, le=10, description="10 = clearly safe under Meta ad policy")
    rationale: str
    fix_suggestions: list[str] = Field(default_factory=list)


class ReviewBatch(BaseModel):
    """Container schema for one scorer call (up to 5 reviews)."""

    reviews: list[AdReview]


class ScoredAd(BaseModel):
    ad: AdConcept
    review: AdReview
    composite: float
    verdict: Verdict


class ScoredPackage(BaseModel):
    source_url: str | None = None
    business_name: str
    model: str | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    total_ads: int
    launch_ready_count: int
    blocked_count: int
    scored_ads: list[ScoredAd]
