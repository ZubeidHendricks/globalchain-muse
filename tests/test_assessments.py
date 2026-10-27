from globalchain.demo import parse_budget_usd, recommend, shop
from globalchain.lexicon import find_terms, only_irreversible_payments, COUNTERFEIT_TERMS
from globalchain.references import match_reference
from globalchain.schemas import Listing, Seller
from globalchain.seller import compute_signals
from globalchain.verify import _validate


def test_lexicon_matches_whole_terms_only():
    assert find_terms("Mirror quality 1:1 replica", COUNTERFEIT_TERMS) == ["replica", "1:1", "mirror quality"]
    assert find_terms("Represented by the brand", COUNTERFEIT_TERMS) == []


def test_irreversible_payments():
    assert only_irreversible_payments(["EFT"])
    assert not only_irreversible_payments(["EFT", "card"])
    assert not only_irreversible_payments([])


def test_reference_matching_needs_the_brand():
    assert match_reference(Listing(title="Coach Tabby 26", price=1)).get("id") == "coach-tabby-26"
    assert match_reference(Listing(title="Tabby style bag", price=1))["id"] == "generic-handbag"


def test_budget_parsing(settings):
    assert parse_budget_usd("handbag under $800", 18) == 800
    assert parse_budget_usd("bag under R9,000", 18) == 500
    assert parse_budget_usd("any handbag", 18) is None


def test_scam_seller_signals(demo_items, settings):
    listing, seller = demo_items[1]
    signals = {s.name: s for s in compute_signals(listing, seller, settings)}
    for name in ("price_vs_resale", "account_age", "duplicate_reviews", "counterfeit_language",
                 "off_platform_pressure", "payment_methods", "stock_dumping"):
        assert signals[name].weight > 0, name
    assert [s.id for s in signals.values()] == [f"S{i}" for i in range(1, len(signals) + 1)]


def test_established_seller_has_no_risk(demo_items, settings):
    listing, seller = demo_items[0]
    assert sum(s.weight for s in compute_signals(listing, seller, settings)) == 0


def test_validation_drops_uncited_findings_and_clamps():
    fallback = {"verdict": "inconclusive", "authenticity_score": 50, "confidence": "low",
                "summary": "fb", "findings": [], "missing_evidence": []}
    raw = {"verdict": "likely_authentic", "authenticity_score": 140, "confidence": "certain",
           "findings": [{"evidence_id": "E1", "observation": "ok", "supports": "authentic"},
                        {"evidence_id": "E9", "observation": "made up", "supports": "authentic"},
                        {"evidence_id": "E1", "observation": "bad label", "supports": "maybe"}]}
    out = _validate(raw, {"E1", "price"}, fallback)
    assert out["authenticity_score"] == 100
    assert out["confidence"] == "low"
    assert [f["evidence_id"] for f in out["findings"]] == ["E1"]


async def test_verify_product_segments_and_persists(service, demo_items):
    listing, _ = demo_items[1]
    result = await service.verify_product(listing)
    assert result.verdict == "likely_counterfeit"
    assert result.engine == "mock"
    assert {e.id for e in result.evidence} == {"E1", "E2", "E3", "E4"}
    assert service.store.get_assessment(result.assessment_id, "product")


async def test_verify_without_photos_is_never_high_confidence(service):
    result = await service.verify_product(Listing(title="Coach Tabby 26", price=300, brand="Coach"))
    assert result.evidence == []
    assert result.confidence != "high"


async def test_seller_adjustment_is_bounded(service, demo_items):
    from globalchain.engine import MockEngine

    class Overreaching(MockEngine):
        async def reason_json(self, task, developer, content, fallback, schema=None):
            return {"adjustment": -90, "explanation": "trust me", "cited_signals": ["S1", "S99"]}, False

    service.engine = Overreaching()
    listing, seller = demo_items[1]
    risk = await service.assess_seller_risk(listing, seller)
    assert risk.adjustment == -15
    assert risk.risk_score == risk.base_score - 15
    assert risk.cited_signals == ["S1"]
    assert risk.fallback_used is False


async def test_mock_engine_results_are_flagged_as_fallback(service, demo_items):
    listing, seller = demo_items[1]
    assert (await service.assess_seller_risk(listing, seller)).fallback_used is True


async def test_demo_shop_ranks_and_filters(service):
    out = await shop("find me an authentic designer handbag under $800", service)
    assert [r["recommendation"] for r in out["results"]][-1] == "avoid"
    assert len(out["skipped_over_budget"]) == 1
    assert out["results"][0]["listing"]["image_urls"][0].startswith("/static/")


def test_recommend_rules():
    class P:
        verdict = "likely_authentic"

    class R:
        risk_level = "low"

    assert recommend(P, R)[0] == "recommended"
    R.risk_level = "high"
    assert recommend(P, R)[0] == "avoid"


def test_seller_model_accepts_minimal_profile():
    assert Seller(seller_id="x").payment_methods == []
