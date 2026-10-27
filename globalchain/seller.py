"""assess_seller_risk: rule-based signals, then a bounded, cited adjustment from Muse Spark."""

import json
import re

from .config import Settings
from .engine import Engine
from .lexicon import COUNTERFEIT_TERMS, OFF_PLATFORM_TERMS, URGENCY_TERMS, find_terms, only_irreversible_payments
from .references import match_reference, price_in_usd
from .schemas import Listing, RiskSignal, Seller, SellerRisk
from .store import Store, new_id

MAX_ADJUSTMENT = 15
COMPLAINT_TERMS = ["fake", "not original", "counterfeit", "never arrived", "scam", "not genuine", "replica"]

DEVELOPER_PROMPT = f"""You are VeriChainX's seller risk analyst, working for a shopping agent.
You receive a seller profile, the listing, and rule-based risk signals (S1, S2, ...) with a base score.

Your job: read the reviews and listing language for context the rules miss (for example reviews
that read as copy-pasted, or a plausible explanation for a low price), then adjust the base score
by at most ±{MAX_ADJUSTMENT} points. Cite the signal ids your explanation relies on. Do not invent facts.

Reply with JSON only:
{{"adjustment": integer between -{MAX_ADJUSTMENT} and {MAX_ADJUSTMENT},
 "explanation": "two or three sentences a shopper can read",
 "cited_signals": ["S1", ...]}}"""


OUTPUT_SCHEMA = {
    "name": "seller_risk_adjustment",
    "schema": {
        "type": "object",
        "properties": {
            "adjustment": {"type": "integer", "minimum": -MAX_ADJUSTMENT, "maximum": MAX_ADJUSTMENT},
            "explanation": {"type": "string"},
            "cited_signals": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["adjustment", "explanation", "cited_signals"],
    },
}

def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def compute_signals(listing: Listing, seller: Seller, settings: Settings) -> list[RiskSignal]:
    raw: list[tuple[str, str, int, str]] = []
    ref = match_reference(listing)
    usd = price_in_usd(listing, settings.usd_zar_rate)

    if ref.get("resale_usd"):
        floor = ref["resale_usd"][0]
        ratio = usd / floor
        weight = 35 if ratio < 0.35 else 20 if ratio < 0.6 else 0
        raw.append(("price_vs_resale", f"{ratio:.2f}x resale floor", weight,
                    f"${usd:.0f} against a typical {ref['brand']} {ref['model']} resale floor of ${floor}."))

    age = seller.account_age_days
    weight = 5 if age is None else 20 if age < 30 else 10 if age < 180 else 0
    raw.append(("account_age", "unknown" if age is None else f"{age} days", weight,
                "Newer accounts carry more risk; established accounts have more to lose."))

    count = seller.review_count
    weight = 10 if not count else 5 if count < 10 else 0
    raw.append(("review_volume", str(count if count is not None else "unknown"), weight,
                "Few reviews means little independent history."))

    if seller.rating is not None:
        weight = 15 if seller.rating < 3.5 else 5 if seller.rating < 4.2 else 0
        raw.append(("rating", f"{seller.rating:.1f}/5", weight, "Average buyer rating."))

    reviews = [_normalise(r) for r in seller.recent_reviews if r.strip()]
    if len(reviews) >= 3:
        dup_share = 1 - len(set(reviews)) / len(reviews)
        raw.append(("duplicate_reviews", f"{dup_share:.0%} duplicated", 15 if dup_share >= 0.4 else 0,
                    "Identical review text across buyers suggests manufactured reviews."))

    complaints = find_terms(" ".join(seller.recent_reviews), COMPLAINT_TERMS)
    if complaints:
        raw.append(("review_complaints", ", ".join(complaints), 20,
                    "Buyers mention authenticity or delivery problems."))

    listing_text = f"{listing.title} {listing.description}"
    for name, terms, weight, detail in (
        ("counterfeit_language", COUNTERFEIT_TERMS, 30, "Listing uses counterfeit trade terms."),
        ("off_platform_pressure", OFF_PLATFORM_TERMS, 20, "Listing pushes payment or contact off-platform."),
        ("urgency_language", URGENCY_TERMS, 5, "Pressure tactics to rush the purchase."),
    ):
        hits = find_terms(listing_text, terms)
        if hits:
            raw.append((name, ", ".join(hits), weight, detail))

    if seller.payment_methods:
        irreversible = only_irreversible_payments(seller.payment_methods)
        raw.append(("payment_methods", ", ".join(seller.payment_methods), 15 if irreversible else 0,
                    "Only irreversible payment methods means no buyer protection."
                    if irreversible else "Offers at least one protected payment method."))

    if seller.active_listings and seller.active_listings > 50 and (age or 0) < 90:
        raw.append(("stock_dumping", f"{seller.active_listings} listings", 10,
                    "A new account listing high volumes of branded stock."))

    return [RiskSignal(id=f"S{i}", name=n, value=v, weight=w, detail=d)
            for i, (n, v, w, d) in enumerate(raw, start=1)]


def risk_level(score: int) -> str:
    return "low" if score < 30 else "medium" if score < 60 else "high"


def _fallback(signals: list[RiskSignal]) -> dict:
    triggered = sorted((s for s in signals if s.weight), key=lambda s: -s.weight)
    if not triggered:
        text = "No rule-based risk signals triggered."
    else:
        text = "Main risk drivers: " + "; ".join(f"{s.name.replace('_', ' ')} ({s.value})" for s in triggered[:3]) + "."
    return {"adjustment": 0, "explanation": text, "cited_signals": [s.id for s in triggered[:3]]}


async def assess_seller_risk(
    listing: Listing, seller: Seller, engine: Engine, store: Store, settings: Settings
) -> SellerRisk:
    signals = compute_signals(listing, seller, settings)
    base = min(100, sum(s.weight for s in signals))
    fallback = _fallback(signals)
    payload = {
        "seller": seller.model_dump(),
        "listing": listing.model_dump(exclude={"image_urls"}),
        "signals": [s.model_dump() for s in signals],
        "base_score": base,
    }
    raw, fallback_used = await engine.reason_json(
        "seller", DEVELOPER_PROMPT, [{"type": "text", "text": json.dumps(payload, indent=1)}], fallback=lambda: fallback,
        schema=OUTPUT_SCHEMA,
    )

    valid_ids = {s.id for s in signals}
    try:
        adjustment = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, int(raw.get("adjustment", 0))))
    except (TypeError, ValueError):
        adjustment = 0
    cited = [c for c in raw.get("cited_signals") or [] if c in valid_ids]
    explanation = raw.get("explanation") if isinstance(raw.get("explanation"), str) else fallback["explanation"]
    score = max(0, min(100, base + adjustment))

    risk = SellerRisk(
        assessment_id=new_id("sr"),
        seller_id=seller.seller_id,
        risk_score=score,
        risk_level=risk_level(score),
        base_score=base,
        adjustment=adjustment,
        explanation=explanation,
        cited_signals=cited or fallback["cited_signals"],
        signals=signals,
        engine=engine.name,
        models={"reasoning": engine.models["seller"]},
        fallback_used=fallback_used,
    )
    store.save_assessment(risk.assessment_id, "seller", risk.model_dump())
    return risk
