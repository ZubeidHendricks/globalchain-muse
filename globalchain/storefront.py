"""Store insights: is a Shopify store likely dropshipping?

A dropshipped item ships from a third-party supplier, which usually means long delivery and no way
to trace the item to the brand. These are public signals only, and heuristics: plenty of honest
stores dropship, so a high score lowers how strongly the shopper's agent recommends a store rather
than marking its items counterfeit.

  D1  long delivery window in the store's shipping policy (/policies/shipping-policy)
  D2  dropshipping or supplier apps referenced in the storefront's HTML
  D3  supplier marketplaces named as product vendors in /products.json
  D4  a recently registered domain (RDAP)
  D5  the shipping policy mentions overseas warehouses or customs
  D6  no shipping policy published
"""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta

import httpx

from . import shopify
from .config import Settings
from .store import now_iso

log = logging.getLogger(__name__)

CACHE_HOURS = 24
_UA = {"user-agent": "GlobalChainMuse/0.3 (agent store insights)"}
DROPSHIP_APPS = ("dsers", "zendrop", "cjdropshipping", "spocket", "autods", "syncee", "oberlo", "modalyst",
                 "dropified", "aliexpress")
SUPPLIER_VENDORS = re.compile(r"aliexpress|alibaba|1688|dhgate|temu|shein|cj ?dropshipping|wholesale|supplier", re.I)
OVERSEAS = re.compile(r"ships? from (china|overseas|our international|international warehouse)|customs (fees|duties|delays)"
                      r"|overseas warehouse", re.I)
_RANGE = re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*(?:business |working )?days", re.I)


def _text(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def score_signals(policy: str | None, homepage: str, vendors: list[str], registered: datetime | None,
                  now: datetime | None = None) -> dict:
    """Score the gathered evidence. Pure, so it can be tested without the network."""
    now = now or datetime.now(UTC)
    signals: list[dict] = []
    ranges = [(int(a), int(b)) for a, b in _RANGE.findall(policy or "") if int(a) <= int(b) <= 90]
    delivery = (min(a for a, _ in ranges), max(b for _, b in ranges)) if ranges else None
    # Brands commonly quote up to ~10 business days for international orders; supplier-shipped
    # stores typically quote 10-20 days or more.
    if delivery and delivery[1] >= 14:
        signals.append({"id": "D1", "name": "long_delivery", "weight": 40 if delivery[1] >= 20 else 30,
                        "detail": f"Shipping policy quotes up to {delivery[1]} days"})
    apps = [a for a in DROPSHIP_APPS if a in homepage.lower()]
    if apps:
        signals.append({"id": "D2", "name": "dropship_apps", "weight": 35,
                        "detail": f"Storefront references {', '.join(apps)}"})
    suppliers = [v for v in vendors if SUPPLIER_VENDORS.search(v)]
    if suppliers:
        signals.append({"id": "D3", "name": "supplier_vendors", "weight": 30,
                        "detail": f"Products list supplier vendors: {', '.join(suppliers[:3])}"})
    if registered:
        age = (now - registered).days
        if age < 365:
            signals.append({"id": "D4", "name": "new_domain", "weight": 25 if age < 180 else 15,
                            "detail": f"Domain registered {age} days ago"})
    if policy and OVERSEAS.search(policy):
        signals.append({"id": "D5", "name": "ships_overseas", "weight": 20,
                        "detail": "Shipping policy mentions overseas warehouses or customs"})
    if policy is None:
        signals.append({"id": "D6", "name": "no_shipping_policy", "weight": 10,
                        "detail": "No shipping policy published"})
    score = min(100, sum(s["weight"] for s in signals))
    return {"dropship_risk": "high" if score >= 50 else "medium" if score >= 25 else "low", "score": score,
            "delivery_days": list(delivery) if delivery else None, "signals": signals,
            "domain_registered": registered.date().isoformat() if registered else None}


async def _get(http: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        return await http.get(url)
    except httpx.HTTPError as exc:
        log.info("store insight fetch failed for %s: %s", url, exc)
        return None


async def gather(domain: str, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    async with httpx.AsyncClient(timeout=settings.http_timeout, headers=_UA, follow_redirects=True,
                                 transport=transport or shopify.DEFAULT_TRANSPORT) as http:
        policy_r, home_r, products_r, rdap_r = await asyncio.gather(
            _get(http, f"https://{domain}/policies/shipping-policy"), _get(http, f"https://{domain}/"),
            _get(http, f"https://{domain}/products.json?limit=50"),
            _get(http, f"https://rdap.org/domain/{domain.removeprefix('www.')}"))
    policy = _text(policy_r.text) if policy_r is not None and policy_r.status_code == 200 else None
    homepage = home_r.text if home_r is not None and home_r.status_code == 200 else ""
    vendors: list[str] = []
    if products_r is not None and products_r.status_code == 200:
        try:
            vendors = sorted({p.get("vendor") or "" for p in products_r.json().get("products", [])} - {""})
        except ValueError:
            pass
    registered = None
    if rdap_r is not None and rdap_r.status_code == 200:
        try:
            event = next((e for e in rdap_r.json().get("events", []) if e.get("eventAction") == "registration"), None)
            registered = datetime.fromisoformat(event["eventDate"].replace("Z", "+00:00")) if event else None
        except (ValueError, KeyError):
            pass
    return {"domain": domain, **score_signals(policy, homepage, vendors, registered), "checked_at": now_iso()}


async def store_insights(service, domain: str, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Cached for a day per store."""
    cached = service.store.get("store_insight", domain)
    if cached and datetime.fromisoformat(cached["checked_at"]) > datetime.now(UTC) - timedelta(hours=CACHE_HOURS):
        return cached
    out = await gather(domain, service.settings, transport)
    service.store.put("store_insight", domain, out)
    return out
