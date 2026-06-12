import pytest

from adengine.generator import BATCH_FRAMEWORKS, generate_ads
from adengine.schemas import AdBatch, HookFramework
from tests.conftest import make_ad
from tests.test_extractor import FakeLLM


def make_batches(counts):
    batches, n = [], 0
    for count in counts:
        batches.append(AdBatch(ads=[make_ad(n + i) for i in range(count)]))
        n += count
    return batches


def test_generate_ads_two_batches_with_ids(brand_dna):
    llm = FakeLLM(make_batches([8, 8]))
    ads = generate_ads(brand_dna, llm, total=16)

    assert len(ads) == 16
    assert [ad.id for ad in ads] == [f"ad_{i:02d}" for i in range(1, 17)]
    assert len(llm.calls) == 2
    for call in llm.calls:
        assert call["temperature"] == 1.0
        assert call["name"] == "generate_ads"
        assert "<brand_dna>" in call["user"]
        assert brand_dna.business_name in call["user"]

    # each batch is assigned a disjoint half of the framework library
    assigned = [call["user"] for call in llm.calls]
    assert "pas" in assigned[0] and "founder_story" in assigned[1]


def test_framework_sets_are_disjoint_and_complete():
    a, b = (set(batch) for batch in BATCH_FRAMEWORKS)
    assert a & b == set()
    assert a | b == set(HookFramework)


def test_total_out_of_spec_range_rejected(brand_dna):
    llm = FakeLLM([])
    with pytest.raises(ValueError):
        generate_ads(brand_dna, llm, total=11)
    with pytest.raises(ValueError):
        generate_ads(brand_dna, llm, total=21)
