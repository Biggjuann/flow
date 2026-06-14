"""Step 3 of the pipeline: blind adversarial scoring + launch gate.

The scorer uses a different persona than the generator (adversarial reviewer)
and never sees the generator's framework labels (blind review). Ads are scored
in batches of up to 5 per LLM call; the weighted composite and the launch gate
are computed in code, deterministically.
"""
from __future__ import annotations

import json

from adengine.llm import LLMClient, LLMError
from adengine.prompts import load_prompt
from adengine.schemas import (
    AdConcept,
    AdReview,
    BrandDNA,
    ReviewBatch,
    ScoredAd,
    ScoredPackage,
    Verdict,
)

BATCH_SIZE = 5

WEIGHTS: dict[str, float] = {
    "hook_strength": 0.25,
    "message_clarity": 0.20,
    "audience_fit": 0.15,
    "offer_match": 0.15,
    "creative_quality": 0.10,
    "policy_risk": 0.15,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

COMPOSITE_GATE = 7.0
POLICY_GATE = 8


def composite_score(review: AdReview) -> float:
    return round(sum(weight * getattr(review, field) for field, weight in WEIGHTS.items()), 2)


def gate(review: AdReview) -> Verdict:
    if composite_score(review) >= COMPOSITE_GATE and review.policy_risk >= POLICY_GATE:
        return Verdict.launch_ready
    return Verdict.blocked


def blind_payload(ad: AdConcept) -> dict:
    """What the reviewer sees: the ad as a buyer would, minus strategy labels."""
    return ad.model_dump(mode="json", exclude={"framework", "angle"})


def _review_chunk(dna: BrandDNA, chunk: list[AdConcept], llm: LLMClient) -> list[AdReview]:
    system = load_prompt("score_ad")
    user = (
        "<brand_dna>\n"
        f"{dna.model_dump_json(indent=2, exclude={'source_url'})}\n"
        "</brand_dna>\n\n"
        "<ads>\n"
        f"{json.dumps([blind_payload(ad) for ad in chunk], indent=2)}\n"
        "</ads>\n\n"
        f"Review all {len(chunk)} ads above. Return one review per ad_id."
    )
    batch = llm.structured(
        ReviewBatch,
        system=system,
        user=user,
        name="score_ads",
        max_tokens=8192,
    )
    return batch.reviews


def score_ads(dna: BrandDNA, ads: list[AdConcept], llm: LLMClient) -> ScoredPackage:
    reviews: dict[str, AdReview] = {}
    for start in range(0, len(ads), BATCH_SIZE):
        chunk = ads[start : start + BATCH_SIZE]
        for review in _review_chunk(dna, chunk, llm):
            reviews[review.ad_id] = review
        # Second pass for any ad the model skipped or mislabeled.
        missing = [ad for ad in chunk if ad.id not in reviews]
        for ad in missing:
            for review in _review_chunk(dna, [ad], llm):
                reviews[review.ad_id] = review

    unscored = [ad.id for ad in ads if ad.id not in reviews]
    if unscored:
        raise LLMError(f"Scorer did not return reviews for: {unscored}")

    scored = []
    for ad in ads:
        review = reviews[ad.id]
        scored.append(
            ScoredAd(
                ad=ad,
                review=review,
                composite=composite_score(review),
                verdict=gate(review),
            )
        )

    launch_ready = sum(1 for s in scored if s.verdict is Verdict.launch_ready)
    return ScoredPackage(
        source_url=dna.source_url,
        business_name=dna.business_name,
        model=llm.model,
        total_ads=len(scored),
        launch_ready_count=launch_ready,
        blocked_count=len(scored) - launch_ready,
        scored_ads=scored,
    )
