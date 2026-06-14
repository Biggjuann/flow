from adengine.report import render_report
from adengine.schemas import ScoredAd, ScoredPackage, Verdict
from adengine.scorer import composite_score, gate
from tests.test_scorer import make_review


def make_package(brand_dna, ads):
    scored = []
    for i, ad in enumerate(ads):
        # alternate strong / weak reviews
        review = make_review(ad.id, score=9, policy=9) if i % 2 == 0 else make_review(ad.id, score=4, policy=6)
        scored.append(
            ScoredAd(ad=ad, review=review, composite=composite_score(review), verdict=gate(review))
        )
    ready = sum(1 for s in scored if s.verdict is Verdict.launch_ready)
    return ScoredPackage(
        source_url=brand_dna.source_url,
        business_name=brand_dna.business_name,
        model="test-model",
        total_ads=len(scored),
        launch_ready_count=ready,
        blocked_count=len(scored) - ready,
        scored_ads=scored,
    )


def test_report_sections(brand_dna, ads):
    package = make_package(brand_dna, ads)
    report = render_report(package, brand_dna)

    assert f"# AdEngine launch package — {brand_dna.business_name}" in report
    assert "## Brand DNA summary" in report
    assert brand_dna.positioning.one_liner in report
    assert "## Site readiness check" in report
    for gap in brand_dna.gaps:
        assert gap in report
    assert "## Top launch-ready ads (5 of 6)" in report
    assert "## Blocked ads (6)" in report
    assert "Add the review count to the hook" in report  # fix suggestions surface
    assert "## Suggested daily budget tiers" in report
    assert "$20/day" in report and "$50/day" in report and "$100/day" in report
    assert "estimates" in report  # heuristics labeled as estimates


def test_report_top_ads_sorted_and_full_copy(brand_dna, ads):
    package = make_package(brand_dna, ads)
    report = render_report(package, brand_dna)
    top = report.split("## Top launch-ready ads")[1].split("## Blocked ads")[0]
    # full copy present for a launch-ready ad
    ready = [s for s in package.scored_ads if s.verdict is Verdict.launch_ready][0]
    assert ready.ad.hook in top
    assert ready.ad.headline in top
    assert ready.ad.cta_button.value in top


def test_report_handles_no_launch_ready(brand_dna, ads):
    package = make_package(brand_dna, ads)
    for scored in package.scored_ads:
        scored.verdict = Verdict.blocked
    package.launch_ready_count = 0
    package.blocked_count = package.total_ads
    report = render_report(package, brand_dna)
    assert "No ads cleared the launch gate" in report
