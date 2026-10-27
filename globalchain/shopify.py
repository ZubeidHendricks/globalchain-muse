"""Shopify stores on the agent network, through each store's own UCP endpoint.

Every Shopify store serves the Universal Commerce Protocol over MCP at https://{store}/api/ucp/mcp:
catalogue search, carts and checkout. The shopper's agent queries it directly, so a store's offers
come from the store itself. Without a Shopify-issued token an agent can build a checkout but not
complete it, so the buyer finishes payment on the store's own checkout (Shop Pay, cards, whatever
the store accepts) through the checkout's continue_url.

Stores that install the GlobalChain app are partners: they get a Hedera wallet, their products get
origin registrations, and each paid order mints an ownership NFT for the buyer (see shopify_app.py).
Other stores still appear, labelled as plain Shopify stores with no origin NFT.
"""

import hashlib
import logging
import re

import httpx

from .config import Settings
from .store import now_iso

log = logging.getLogger(__name__)

# Shopify's published sample agent profile, used until GlobalChain's own profile is hosted publicly
# (a store fetches the profile URL, so it must be reachable from the internet).
FIXTURE_PROFILE = "https://shopify.dev/ucp/agent-profiles/2026-08-25/valid-with-capabilities.json"

# Tests swap in an httpx.MockTransport here so no store is contacted.
DEFAULT_TRANSPORT: httpx.AsyncBaseTransport | None = None


class UCPError(RuntimeError):
    pass


def store_domain(value: str) -> str:
    """Normalise 'https://www.Example.com/' or 'example.myshopify.com' to a bare host."""
    host = re.sub(r"^https?://", "", value.strip().lower()).split("/")[0]
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
        raise ValueError(f"Not a store domain: {value!r}")
    return host


class UCPClient:
    def __init__(self, domain: str, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.domain = store_domain(domain)
        self.settings = settings
        self.transport = transport or DEFAULT_TRANSPORT
        self.profile = settings.ucp_agent_profile_url or FIXTURE_PROFILE

    async def call(self, tool: str, arguments: dict) -> dict:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": tool, "arguments": {"meta": {"ucp-agent": {"profile": self.profile}}, **arguments}}}
        async with httpx.AsyncClient(timeout=self.settings.http_timeout, transport=self.transport,
                                     follow_redirects=True) as http:
            resp = await http.post(f"https://{self.domain}/api/ucp/mcp", json=body)
        try:
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UCPError(f"{self.domain} did not answer as a UCP store ({exc})") from exc
        if "error" in data:
            raise UCPError(f"{self.domain}: {data['error'].get('message', data['error'])}")
        result = data.get("result") or {}
        if result.get("isError"):
            raise UCPError(f"{self.domain}: {result.get('content') or 'tool error'}")
        return result.get("structuredContent") or {}

    async def search(self, query: str, limit: int = 3) -> list[dict]:
        out = await self.call("search_catalog", {"catalog": {"query": query, "pagination": {"limit": limit}}})
        return out.get("products") or []

    async def create_checkout(self, variant_id: str, quantity: int = 1) -> dict:
        return await self.call("create_checkout", {"checkout": {
            "line_items": [{"item": {"id": variant_id}, "quantity": quantity}]}})


def item_id_for(domain: str, product_id: str) -> str:
    return "shp_" + hashlib.sha256(f"{domain}|{product_id}".encode()).hexdigest()[:16]


def to_item(vendor_id: str, domain: str, brand: str, p: dict) -> dict | None:
    """A UCP product as a GlobalChain catalogue item, or None if it can't be bought."""
    variants = [v for v in p.get("variants") or [] if (v.get("availability") or {}).get("available", True)]
    if not variants:
        return None
    v = min(variants, key=lambda x: x["price"]["amount"])
    if v["price"]["currency"] not in ("USD", "ZAR"):  # the price checks only know these two
        return None
    d = p.get("description")
    desc = re.sub(r"<[^>]+>", " ", (d.get("html") or d.get("plain") or "") if isinstance(d, dict) else str(d or ""))
    photos = [m["url"] for m in p.get("media") or [] if m.get("type") == "image" and m.get("url")][:2]
    return {"item_id": item_id_for(domain, p["id"]), "vendor_id": vendor_id, "key": p["id"], "title": p["title"],
            "brand": brand, "model": None, "category": "bag", "description": re.sub(r"\s+", " ", desc).strip()[:600],
            "price": v["price"]["amount"] / 100, "currency": v["price"]["currency"], "photos": photos,
            "registration_id": None, "nft": None, "status": "available", "listed_at": now_iso(),
            "shopify": {"domain": domain, "product_id": p["id"], "variant_id": v["id"], "url": p.get("url")}}
