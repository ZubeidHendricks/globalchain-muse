"""The scorecard: beyond the recommendation (can it be trusted at all?), how good the buy is.

Five parts, each 0-100 with a one-line note saying where the number came from, and an overall score
that ranks results within a recommendation. Computed in code from the checks, never by the model, and
a score can't read better than the recommendation: caution is capped at 69 (Fair), avoid at 20. Adapted from VeriChainX Sol's
scorecard, with an origin part in place of reviews: here the chain checks are the strongest evidence.
"""

from statistics import median

WEIGHTS = {"authentic": 0.30, "origin": 0.25, "seller": 0.25, "value": 0.10, "delivery": 0.10}
LABELS = {"authentic": "Authentic", "origin": "Origin", "seller": "Seller", "value": "Value", "delivery": "Delivery"}


def _row(key: str, score: float | None, note: str) -> dict:
    return {"key": key, "label": LABELS[key], "score": None if score is None else round(max(0, min(100, score))),
            "note": note}


def combine(rows: list[dict], recommendation: str) -> dict:
    known = [r for r in rows if r["score"] is not None]
    weight = sum(WEIGHTS[r["key"]] for r in known)
    overall = sum(r["score"] * WEIGHTS[r["key"]] for r in known) / weight if weight else 0
    # The recommendation still rules: the grade can't read better than it.
    overall = min(overall, {"avoid": 20, "caution": 69}.get(recommendation, 100))
    overall = round(max(0, min(100, overall)))
    grade = "Excellent" if overall >= 85 else "Good" if overall >= 70 else "Fair" if overall >= 50 else "Poor"
    return {"overall": overall, "grade": grade, "rows": rows}


def _authentic(offer: dict, product: dict) -> dict:
    score = product["authenticity_score"]
    verdict = product["verdict"].replace("_", " ")
    if offer.get("nft") == "cloned":
        return _row("authentic", 0, "Its NFT belongs to another vendor: the QR code was copied")
    if product.get("registration_status") == "found":
        return _row("authentic", score, f"Photos compared with the item's origin fingerprint: {verdict}")
    if product.get("registration_status") == "unknown":
        return _row("authentic", min(score, 20), "Cites a registration that doesn't exist")
    if offer.get("vendor_check") == "shopify_store":
        return _row("authentic", min(score, 70), f"Checked against brand reference data: {verdict}; no origin record")
    return _row("authentic", min(score, 75), f"Checked against brand reference data: {verdict}")


def _origin(offer: dict) -> dict:
    nft, check = offer.get("nft"), offer.get("vendor_check")
    if nft == "cloned":
        return _row("origin", 0, "Registration copied from another vendor's item")
    if check == "shopify_store":
        return _row("origin", 25, "Shopify store, not a GlobalChain partner: no origin record or NFT")
    if check != "verified":
        return _row("origin", 0, "Vendor's signature not confirmed on Hedera")
    if nft == "owned":
        return _row("origin", 100, "Origin record on Hedera, vendor's signature checked, vendor holds the NFT")
    if offer.get("registration_id"):
        return _row("origin", 50, "Origin record on Hedera, but the vendor doesn't hold its NFT")
    return _row("origin", 35, "Signed by a verified vendor; the item has no origin record")


def _seller(offer: dict, risk: dict, seller: dict) -> dict:
    notes = [f"seller risk {risk['risk_score']}/100 ({risk['risk_level']})"]
    if seller.get("review_count"):
        rating = f"{seller['rating']:.1f}/5 from " if seller.get("rating") is not None else ""
        notes.append(f"{rating}{seller['review_count']:,} reviews")
    score = 100 - risk["risk_score"]
    if offer.get("vendor_check") not in ("verified", "shopify_store"):
        score = min(score, 20)
        notes.append("not in the verified vendor directory")
    return _row("seller", score, "; ".join(notes))


def value_row(price_usd: float, peers_usd: list[float], dropship: bool = False) -> dict:
    """Price against the typical (median) price of the other results."""
    if len(peers_usd) < 3 or not price_usd:
        return _row("value", None, "Not enough similar offers to compare prices")
    typical = median(peers_usd)
    diff = (typical - price_usd) / typical
    pct = round(abs(diff) * 100)
    note = ("About the typical price of similar offers" if pct < 5
            else f"{pct}% {'below' if diff > 0 else 'above'} the typical price of similar offers")
    score = max(10, 55 + diff * 50)
    if dropship:
        score -= 15
        note += "; supplier items like this usually sell for less elsewhere"
    return _row("value", score, note)


def _delivery(offer: dict, seller: dict) -> dict:
    ins = offer.get("store_insights") or {}
    days = ins.get("delivery_days")
    if days:
        lo, hi = days[0], days[-1]
        score = 90 if hi <= 5 else 78 if hi <= 8 else 60 if hi <= 12 else 40 if hi <= 20 else 20
        return _row("delivery", score, f"About {lo}-{hi} days by the store's shipping policy")
    if ins.get("dropship_risk") == "high":
        return _row("delivery", 25, "Looks dropshipped: expect slow delivery")
    where = f" from {seller['ships_from']}" if seller.get("ships_from") else ""
    return _row("delivery", None, f"Ships{where}; delivery time set by the vendor")


def scorecard(result: dict, peers_usd: list[float]) -> dict:
    """result is one of the agent's result cards (offer, product, seller_risk, seller, recommendation)."""
    offer, product, risk, seller = result["offer"], result["product"], result["seller_risk"], result["seller"]
    dropship = (offer.get("store_insights") or {}).get("dropship_risk") == "high"
    return combine([_authentic(offer, product), _origin(offer), _seller(offer, risk, seller),
                    value_row(result["price_usd"], peers_usd, dropship), _delivery(offer, seller)],
                   result["recommendation"])
