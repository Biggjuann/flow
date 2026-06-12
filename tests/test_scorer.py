import json

import pytest

from adengine.llm import LLMError
from adengine.schemas import AdReview, ReviewBatch, Verdict
from adengine.scorer import blind_payload, composite_score, gate, score_ads
from tests.test_extractor import FakeLLM


def make_review(ad_id, score=8, policy=9, **overrides):
    fields = dict(
        ad_id=ad_id,
        hook_strength=score,
        message_clarity=score,
        audience_fit=score,
        offer_match=score,
        creative_quality=score,
        policy_risk=policy,
        rationale="Solid hook, weak proof.",
        fix_suggestions=["Add the review count to the hook"],
    )
    fields.update(overrides)
    return AdReview(**fields)


def test_composite_is_weighted_average():
    review = make_review("ad_01", score=8, policy=9)
    # 0.85 weight at 8 + 0.15 weight at 9
    assert composite_score(review) == round(8 * 0.85 + 9 * 0.15, 2)


def test_gate_requires_both_thresholds():
    assert gate(make_review("a", score=8, policy=9)) is Verdict.launch_ready
    # composite high but policy risk below 8 -> blocked
    assert gate(make_review("a", score=10, policy=7)) is Verdict.blocked
    # policy fine but composite below 7.0 -> blocked
    assert gate(make_review("a", score=5, policy=10)) is Verdict.blocked


def test_blind_payload_hides_framework_and_angle(ads):
    payload = blind_payload(ads[0])
    assert "framework" not in payload
    assert "angle" not in payload
    assert payload["hook"] == ads[0].hook


def test_score_ads_batches_of_five(brand_dna, ads):
    # 12 ads -> 3 calls of [5, 5, 2]
    batches = [
        ReviewBatch(reviews=[make_review(ad.id) for ad in ads[0:5]]),
        ReviewBatch(reviews=[make_review(ad.id) for ad in ads[5:10]]),
        ReviewBatch(reviews=[make_review(ad.id, score=5) for ad in ads[10:12]]),
    ]
    llm = FakeLLM(batches)
    package = score_ads(brand_dna, ads, llm)

    assert len(llm.calls) == 3
    assert package.total_ads == 12
    assert package.launch_ready_count == 10
    assert package.blocked_count == 2
    assert package.business_name == brand_dna.business_name

    # framework labels never reach the scorer (blind review)
    for call in llm.calls:
        sent_ads = json.loads(call["user"].split("<ads>\n")[1].split("\n</ads>")[0])
        assert all("framework" not in ad for ad in sent_ads)


def test_score_ads_retries_missing_reviews_individually(brand_dna, ads):
    five = ads[:5]
    first = ReviewBatch(reviews=[make_review(ad.id) for ad in five[:4]])  # skips one
    followup = ReviewBatch(reviews=[make_review(five[4].id)])
    extra = [
        ReviewBatch(reviews=[make_review(ad.id) for ad in ads[5:10]]),
        ReviewBatch(reviews=[make_review(ad.id) for ad in ads[10:12]]),
    ]
    llm = FakeLLM([first, followup] + extra)
    package = score_ads(brand_dna, ads, llm)
    assert package.total_ads == 12
    assert len(llm.calls) == 4


def test_score_ads_raises_when_review_never_arrives(brand_dna, ads):
    five = ads[:5]
    incomplete = ReviewBatch(reviews=[make_review(ad.id) for ad in five[:4]])
    still_wrong = ReviewBatch(reviews=[make_review("ad_does_not_exist")])
    llm = FakeLLM([incomplete, still_wrong])
    with pytest.raises(LLMError, match="did not return reviews"):
        score_ads(brand_dna, five, llm)
