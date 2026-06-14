"""Render the human-readable report.md from a scored package + Brand DNA."""
from __future__ import annotations

from adengine.schemas import BrandDNA, ScoredAd, ScoredPackage, Verdict

# Rough CPM heuristics for budget tiers. Labeled as estimates in the report.
CPM_LOW_USD = 25
CPM_HIGH_USD = 45
EST_CTR = 0.01
BUDGET_TIERS_USD = (20, 50, 100)


def _bullets(items: list[str], empty: str = "_none found_") -> str:
    return "\n".join(f"- {item}" for item in items) if items else empty


def _ad_copy_block(scored: ScoredAd) -> str:
    ad = scored.ad
    review = scored.review
    return "\n".join(
        [
            f"### {ad.id} — {ad.framework.value} (composite {scored.composite:.2f}, policy {review.policy_risk}/10)",
            "",
            f"**Angle:** {ad.angle}",
            "",
            f"> **Hook:** {ad.hook}",
            ">",
            "> **Primary text:**",
            *(f"> {line}" for line in ad.primary_text.splitlines()),
            ">",
            f"> **Headline:** {ad.headline}",
            f"> **Description:** {ad.description}",
            f"> **CTA button:** {ad.cta_button.value}",
            "",
            f"**Creative ({ad.creative_direction.format.value}):** {ad.creative_direction.image_prompt}",
            f"**Visual notes:** {ad.creative_direction.visual_notes}",
            f"**Audience hint:** {', '.join(ad.target_audience_hint.interests) or 'n/a'} | "
            f"ages {ad.target_audience_hint.age_range} | {ad.target_audience_hint.geo_hint or 'n/a'}",
            "",
            f"**Reviewer:** {review.rationale}",
        ]
    )


def _budget_table() -> str:
    rows = [
        "| Daily budget | Est. daily impressions* | Est. daily clicks* | What it buys |",
        "|---|---|---|---|",
    ]
    notes = {
        20: "Testing tier — validate 2-3 hooks against each other",
        50: "Learning tier — enough volume for Meta to start optimizing",
        100: "Scaling tier — run winners + keep 1-2 fresh tests live",
    }
    for budget in BUDGET_TIERS_USD:
        imp_low = int(budget / CPM_HIGH_USD * 1000)
        imp_high = int(budget / CPM_LOW_USD * 1000)
        clicks_low = int(imp_low * EST_CTR)
        clicks_high = int(imp_high * EST_CTR)
        rows.append(
            f"| ${budget}/day | {imp_low:,}–{imp_high:,} | {clicks_low}–{clicks_high} | {notes[budget]} |"
        )
    rows.append("")
    rows.append(
        f"\\*Rough estimates assuming ${CPM_LOW_USD}–${CPM_HIGH_USD} CPM and ~{EST_CTR:.0%} CTR; "
        "actual costs vary by audience, placement, and season."
    )
    return "\n".join(rows)


def render_report(package: ScoredPackage, dna: BrandDNA) -> str:
    launch_ready = sorted(
        (s for s in package.scored_ads if s.verdict is Verdict.launch_ready),
        key=lambda s: s.composite,
        reverse=True,
    )
    blocked = sorted(
        (s for s in package.scored_ads if s.verdict is Verdict.blocked),
        key=lambda s: s.composite,
        reverse=True,
    )

    sections: list[str] = []
    sections.append(f"# AdEngine launch package — {package.business_name}")
    sections.append(
        f"Source: {package.source_url or 'n/a'}  \n"
        f"Generated: {package.created_at}  \n"
        f"Model: {package.model or 'n/a'}  \n"
        f"Ads: **{package.total_ads}** total · "
        f"**{package.launch_ready_count} launch-ready** · "
        f"**{package.blocked_count} blocked**"
    )

    sections.append("## Brand DNA summary")
    sections.append(
        "\n".join(
            [
                f"- **What they sell:** {dna.what_they_sell} ({dna.category.value})",
                f"- **Positioning:** {dna.positioning.one_liner}",
                f"- **ICP:** {dna.icp.who_its_for}",
                f"- **Desired outcome:** {dna.icp.desired_outcome}",
                f"- **Offer:** {dna.offer.description}"
                + (f" — {dna.offer.pricing_signal}" if dna.offer.pricing_signal else ""),
                f"- **Conversion path:** {dna.offer.conversion_path.value}",
                f"- **Voice:** {', '.join(dna.voice.tone) or 'n/a'}",
                f"- **Proof:** {', '.join(dna.proof.proof_points) or 'none found'}",
            ]
        )
    )

    sections.append("## Site readiness check")
    if dna.gaps:
        sections.append(
            "These gaps weaken ad performance — fix them before scaling spend:\n\n"
            + _bullets(dna.gaps)
        )
    else:
        sections.append("No major gaps found. The site looks ad-ready.")

    sections.append(f"## Top launch-ready ads ({min(5, len(launch_ready))} of {len(launch_ready)})")
    if launch_ready:
        sections.extend(_ad_copy_block(s) for s in launch_ready[:5])
    else:
        sections.append(
            "_No ads cleared the launch gate (composite >= 7.0 and policy >= 8). "
            "See blocked ads below for fixes._"
        )

    sections.append(f"## Blocked ads ({len(blocked)})")
    if blocked:
        for scored in blocked:
            fixes = _bullets(scored.review.fix_suggestions, empty="- _no specific fixes suggested_")
            sections.append(
                f"### {scored.ad.id} — composite {scored.composite:.2f}, "
                f"policy {scored.review.policy_risk}/10\n\n"
                f"**Hook:** {scored.ad.hook}\n\n"
                f"**Why blocked:** {scored.review.rationale}\n\n"
                f"**Fixes:**\n{fixes}"
            )
    else:
        sections.append("_Nothing blocked._")

    sections.append("## Suggested daily budget tiers")
    sections.append(_budget_table())

    return "\n\n".join(sections) + "\n"
