import pytest
from pydantic import ValidationError

from adengine.schemas import AdConcept, AdReview, BrandDNA


def test_brand_dna_round_trip(brand_dna):
    raw = brand_dna.model_dump_json()
    again = BrandDNA.model_validate_json(raw)
    assert again == brand_dna


def test_ad_concept_enforces_meta_char_limits(ads):
    base = ads[0].model_dump()
    base["headline"] = "x" * 41
    with pytest.raises(ValidationError):
        AdConcept.model_validate(base)

    base = ads[0].model_dump()
    base["hook"] = "x" * 126
    with pytest.raises(ValidationError):
        AdConcept.model_validate(base)

    base = ads[0].model_dump()
    base["description"] = "x" * 31
    with pytest.raises(ValidationError):
        AdConcept.model_validate(base)


def test_ad_review_bounds():
    base = dict(
        ad_id="ad_01",
        hook_strength=5,
        message_clarity=5,
        audience_fit=5,
        offer_match=5,
        creative_quality=5,
        policy_risk=5,
        rationale="ok",
    )
    AdReview.model_validate(base)
    with pytest.raises(ValidationError):
        AdReview.model_validate({**base, "policy_risk": 11})
    with pytest.raises(ValidationError):
        AdReview.model_validate({**base, "hook_strength": 0})
