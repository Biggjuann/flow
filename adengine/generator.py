"""Step 2 of the pipeline: BrandDNA -> 12-20 AdConcepts across hook frameworks.

Generation runs as two parallel batches at temperature 1.0, each assigned a
disjoint half of the framework library, for variety.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from adengine.llm import LLMClient
from adengine.prompts import load_prompt
from adengine.schemas import AdBatch, AdConcept, BrandDNA, HookFramework

# Two disjoint framework sets, one per parallel batch.
BATCH_FRAMEWORKS: list[list[HookFramework]] = [
    [
        HookFramework.pas,
        HookFramework.aida,
        HookFramework.curiosity_gap,
        HookFramework.social_proof,
        HookFramework.before_after_bridge,
    ],
    [
        HookFramework.question_hook,
        HookFramework.contrarian,
        HookFramework.urgency_loss_aversion,
        HookFramework.founder_story,
    ],
]

DEFAULT_TOTAL = 16  # spec: 12-20


def _generate_batch(
    dna: BrandDNA, llm: LLMClient, frameworks: list[HookFramework], count: int
) -> list[AdConcept]:
    system = load_prompt("generate_ads")
    framework_list = ", ".join(f.value for f in frameworks)
    user = (
        "<brand_dna>\n"
        f"{dna.model_dump_json(indent=2, exclude={'source_url'})}\n"
        "</brand_dna>\n\n"
        f"Assigned hook frameworks: {framework_list}\n"
        f"Produce exactly {count} ad concepts, spread across the assigned "
        "frameworks (at least one per framework)."
    )
    batch = llm.structured(
        AdBatch,
        system=system,
        user=user,
        name="generate_ads",
        temperature=1.0,
        max_tokens=8192,
    )
    return batch.ads


def generate_ads(
    dna: BrandDNA, llm: LLMClient, total: int = DEFAULT_TOTAL
) -> list[AdConcept]:
    if not 12 <= total <= 20:
        raise ValueError(f"total must be 12-20 per spec, got {total}")
    counts = [total - total // 2, total // 2]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_generate_batch, dna, llm, frameworks, count)
            for frameworks, count in zip(BATCH_FRAMEWORKS, counts)
        ]
        ads = [ad for future in futures for ad in future.result()]

    for i, ad in enumerate(ads, start=1):
        ad.id = f"ad_{i:02d}"
    return ads
