"""Shared fixtures: fake Anthropic client, sample BrandDNA / ads.

pytest must pass with no network — all LLM and HTTP calls are mocked.
"""
from __future__ import annotations

from typing import Any

import pytest

from adengine.schemas import (
    ICP,
    AdConcept,
    BrandDNA,
    Category,
    ConversionPath,
    CreativeDirection,
    CTAButton,
    HookFramework,
    Offer,
    Positioning,
    Proof,
    TargetAudienceHint,
    Visual,
    Voice,
)


# ----------------------------------------------------------- fake Anthropic SDK

class FakeUsage:
    def __init__(self, input_tokens: int = 100, output_tokens: int = 50) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, input: dict[str, Any]) -> None:
        self.input = input


class FakeResponse:
    def __init__(self, tool_input: dict[str, Any] | None, model: str = "fake-model") -> None:
        self.content = [FakeToolUseBlock(tool_input)] if tool_input is not None else []
        self.usage = FakeUsage()
        self.model = model
        self.stop_reason = "tool_use"


class FakeMessages:
    """Pops queued responses; records every create() call's kwargs."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessages ran out of queued responses")
        return self._responses.pop(0)


class FakeAnthropic:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.messages = FakeMessages(responses)


@pytest.fixture
def fake_anthropic():
    return FakeAnthropic


@pytest.fixture
def fake_response():
    return FakeResponse


# -------------------------------------------------------------- sample objects

@pytest.fixture
def brand_dna() -> BrandDNA:
    return BrandDNA(
        business_name="Acme Coffee Club",
        what_they_sell="Specialty coffee subscription delivered monthly",
        category=Category.ecom,
        icp=ICP(
            who_its_for="Home coffee enthusiasts who hate stale supermarket beans",
            pain_points=["Stale beans", "Decision fatigue picking roasters"],
            desired_outcome="Cafe-quality coffee at home without the hunt",
        ),
        offer=Offer(
            description="Monthly curated coffee subscription",
            pricing_signal="$24/mo",
            cta="Start your subscription",
            conversion_path=ConversionPath.purchase,
        ),
        voice=Voice(
            tone=["warm", "confident"],
            words_they_use=["freshly roasted", "small-batch"],
            words_to_avoid=["cheap", "gourmet"],
        ),
        visual=Visual(colors=["#2B1B12", "#E8C39E"], style_descriptors=["warm", "editorial"]),
        proof=Proof(testimonials_present=True, proof_points=["4.8 stars from 2,100 reviews"]),
        positioning=Positioning(
            one_liner="Cafe-quality beans from indie roasters, fresh to your door",
            differentiators=["Roast-date guarantee", "Indie roaster rotation"],
        ),
        gaps=["No visible shipping cost", "Weak above-the-fold CTA"],
        source_url="https://acmecoffee.example",
    )


def make_ad(i: int, framework: HookFramework = HookFramework.pas) -> AdConcept:
    return AdConcept(
        id=f"ad_{i:02d}",
        framework=framework,
        angle=f"Angle number {i}",
        hook=f"Hook {i}: your beans went stale before you opened the bag.",
        primary_text="Line one.\nLine two with the offer.\nStart today.",
        headline=f"Fresh beans, monthly #{i}",
        description="Roast-date guaranteed",
        cta_button=CTAButton.SHOP_NOW,
        creative_direction=CreativeDirection(
            image_prompt="Overhead shot of fresh coffee beans spilling from a kraft bag",
            visual_notes="Warm light, brand palette",
        ),
        target_audience_hint=TargetAudienceHint(
            interests=["Specialty coffee", "Espresso"],
            age_range="25-44",
            geo_hint="US national",
        ),
    )


@pytest.fixture
def ads() -> list[AdConcept]:
    frameworks = list(HookFramework)
    return [make_ad(i, frameworks[i % len(frameworks)]) for i in range(1, 13)]
