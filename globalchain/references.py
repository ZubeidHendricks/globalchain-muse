"""Brand reference data: which features to segment and what they should look like."""

import json
from functools import lru_cache
from pathlib import Path

from .schemas import Listing

_DATA = Path(__file__).parent / "data" / "brands.json"


@lru_cache
def load_references() -> list[dict]:
    return json.loads(_DATA.read_text())["references"]


def match_reference(listing: Listing) -> dict:
    """Pick the reference for the claimed brand and model, falling back to the generic set."""
    refs = load_references()
    haystack = " ".join(filter(None, [listing.brand, listing.model, listing.title])).lower()
    best, best_score = None, 0
    for ref in refs:
        if not ref["brand"]:
            continue
        score = 0
        if ref["brand"].lower() in haystack:
            score += 2
        if any(alias in haystack for alias in ref["aliases"]):
            score += 1
        if score > best_score:
            best, best_score = ref, score
    if best and best_score >= 2:
        return best
    return next(r for r in refs if r["id"] == "generic-handbag")


def price_in_usd(listing: Listing, usd_zar_rate: float) -> float:
    if listing.currency == "ZAR":
        return listing.price / usd_zar_rate
    return listing.price
