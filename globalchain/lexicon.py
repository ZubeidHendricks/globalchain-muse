"""Listing-language cues that often accompany counterfeits or off-platform scams."""

import re

# Sellers of fakes frequently describe them openly in trade slang.
COUNTERFEIT_TERMS = [
    "replica", "rep ", "1:1", "mirror quality", "mirror grade", "aaa", "first copy", "master copy",
    # "inspired" alone is ordinary copywriting ("inspired by rainy commutes"); these phrasings aren't.
    "designer inspired", "designer-inspired", "luxury inspired", "inspired version",
    "dupe", "same as original", "factory direct", "unbranded original",
]

# Pushes to move the deal off-platform or to irreversible payment.
OFF_PLATFORM_TERMS = [
    "whatsapp", "telegram", "dm me", "inbox me", "eft only", "eft first", "cash send", "crypto only",
    "usdt", "gift card", "pay outside", "no returns", "deposit first",
]

URGENCY_TERMS = ["today only", "last one", "urgent sale", "must go", "quick sale", "first to pay"]

_SAFE_PAYMENTS = {"card", "credit card", "paypal", "platform escrow", "escrow", "buyer protection", "payfast", "ozow",
                  "shopify_checkout", "shop pay"}  # Shopify checkout takes cards, with chargebacks


def find_terms(text: str, terms: list[str]) -> list[str]:
    lowered = f" {text.lower()} "
    return [t.strip() for t in terms if re.search(rf"(?<![a-z]){re.escape(t.strip())}(?![a-z])", lowered)]


def only_irreversible_payments(methods: list[str]) -> bool:
    lowered = {m.lower().strip() for m in methods}
    return bool(lowered) and not (lowered & _SAFE_PAYMENTS)
