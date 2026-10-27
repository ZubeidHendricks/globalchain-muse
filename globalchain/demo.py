"""A scripted shopping agent for the web demo.

It stands in for a real agent: parse the request, find listings within budget, call
VeriChainX on each, then rank and recommend. Real agents do the same over MCP.
"""

import asyncio
import base64
import json
import re
from pathlib import Path

from .references import price_in_usd
from .schemas import Listing, ProductVerification, Seller, SellerRisk
from .service import VeriChainX

_DATA = Path(__file__).parent / "data" / "demo_listings.json"
_STATIC = Path(__file__).parent / "static"


def load_demo_listings() -> list[tuple[Listing, Seller]]:
    items = json.loads(_DATA.read_text())["listings"]
    return [(Listing(**i["listing"]), Seller(**i["seller"])) for i in items]


def parse_budget_usd(query: str, usd_zar_rate: float) -> float | None:
    m = re.search(r"(?:under|below|less than|max|<)\s*(\$|r|usd|zar)?\s*([\d,]+(?:\.\d+)?)\s*(usd|zar|rand)?",
                  query.lower())
    if not m:
        return None
    amount = float(m.group(2).replace(",", ""))
    unit = (m.group(1) or m.group(3) or "$").lower()
    return amount / usd_zar_rate if unit in ("r", "zar", "rand") else amount


def _inline_static_images(listing: Listing) -> Listing:
    """Demo images live under /static; hand them to the pipeline as data: URLs."""
    urls = []
    for url in listing.image_urls:
        if url.startswith("/static/"):
            data = (_STATIC / url.removeprefix("/static/")).read_bytes()
            url = "data:image/png;base64," + base64.b64encode(data).decode()
        urls.append(url)
    return listing.model_copy(update={"image_urls": urls})


def recommend(product: ProductVerification, risk: SellerRisk) -> tuple[str, str]:
    if product.verdict == "likely_counterfeit" or risk.risk_level == "high":
        return "avoid", "Strong signs of a counterfeit or a risky seller."
    if product.verdict == "likely_authentic" and risk.risk_level == "low":
        return "recommended", "Evidence and seller history both look sound."
    return "caution", "Not enough evidence to be confident. Ask the seller for more photos before paying."


_RANK = {"recommended": 0, "caution": 1, "avoid": 2}


async def shop(query: str, service: VeriChainX) -> dict:
    budget = parse_budget_usd(query, service.settings.usd_zar_rate)
    candidates, over_budget = [], []
    for listing, seller in load_demo_listings():
        usd = price_in_usd(listing, service.settings.usd_zar_rate)
        (candidates if budget is None or usd <= budget else over_budget).append((listing, seller, usd))

    async def check(listing: Listing, seller: Seller, usd: float) -> dict:
        product, risk = await asyncio.gather(
            service.verify_product(_inline_static_images(listing)),
            service.assess_seller_risk(listing, seller),
        )
        action, reason = recommend(product, risk)
        return {"listing": listing.model_dump(), "seller": seller.model_dump(), "price_usd": round(usd, 2),
                "product": product.model_dump(), "seller_risk": risk.model_dump(),
                "recommendation": action, "reason": reason}

    results = await asyncio.gather(*(check(*c) for c in candidates))
    results.sort(key=lambda r: (_RANK[r["recommendation"]], -r["product"]["authenticity_score"],
                                r["seller_risk"]["risk_score"]))
    return {
        "query": query,
        "budget_usd": round(budget, 2) if budget is not None else None,
        "engine": service.engine.name,
        "results": results,
        "skipped_over_budget": [{"title": l.title, "price_usd": round(u, 2)} for l, _, u in over_budget],
    }
