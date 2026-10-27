import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from globalchain import orders, shopify
from globalchain.agent import run_agent
from globalchain.lexicon import COUNTERFEIT_TERMS, find_terms
from globalchain.storefront import score_signals

GOOD = "www.goodbags.com"
DROP = "www.dropbags.shop"


def _product(n: int, domain: str, price: int, title: str) -> dict:
    return {"id": f"gid://shopify/Product/{n}", "title": title, "url": f"https://{domain}/products/{n}",
            "description": {"html": f"<p>{title}: a roomy everyday tote.</p>"},
            "price_range": {"min": {"amount": price, "currency": "USD"}},
            "media": [{"type": "image", "url": f"https://cdn.example/{n}.png"}],
            "variants": [{"id": f"gid://shopify/ProductVariant/{n}0", "price": {"amount": price, "currency": "USD"},
                          "availability": {"available": True}}]}


class FakeShops:
    """Two Shopify stores: an established brand and a new dropshipping store."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/api/ucp/mcp":
            body = json.loads(request.content)
            tool = body["params"]["name"]
            self.calls.append((host, tool))
            assert body["params"]["arguments"]["meta"]["ucp-agent"]["profile"].startswith("https://")
            if tool == "search_catalog":
                products = ([_product(1, host, 12900, "Everyday Tote"), _product(2, host, 99900, "Luxe Tote")]
                            if host == GOOD else [_product(3, host, 2499, "Trendy Tote Bag")])
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {
                    "ucp": {"version": "2026-08-25"}, "products": products}}})
            if tool == "create_checkout":
                line = body["params"]["arguments"]["checkout"]["line_items"][0]
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {
                    "id": "chk_1", "status": "incomplete", "continue_url": f"https://{host}/checkouts/chk_1",
                    "line_items": [line], "totals": [{"type": "total", "amount": 12900}]}}})
        if host == "rdap.org":
            age = 3000 if GOOD in str(request.url) or "goodbags" in str(request.url) else 40
            registered = (datetime.now(UTC) - timedelta(days=age)).isoformat().replace("+00:00", "Z")
            return httpx.Response(200, json={"events": [{"eventAction": "registration", "eventDate": registered}]})
        if path == "/policies/shipping-policy":
            text = "Orders ship in 2-5 business days." if host == GOOD else \
                "Processing 3-5 days. Delivery takes 10-25 business days as items ship from our overseas warehouse."
            return httpx.Response(200, text=f"<html><body><p>{text}</p></body></html>")
        if path == "/products.json":
            vendor = "Good Bags" if host == GOOD else "AliExpress Supplier"
            return httpx.Response(200, json={"products": [{"vendor": vendor}]})
        if path == "/":
            html = "<script src='https://cdn.dsers.com/app.js'></script>" if host == DROP else "<html></html>"
            return httpx.Response(200, text=html)
        return httpx.Response(404)


@pytest.fixture
def shops(service, monkeypatch):
    fake = FakeShops()
    monkeypatch.setattr(shopify, "DEFAULT_TRANSPORT", httpx.MockTransport(fake))
    service.settings = service.settings.model_copy(update={"shopify_stores": f"{GOOD}=Good Bags,{DROP}"})
    return fake


async def test_shopify_stores_pitch_from_their_own_catalogue(service, shops):
    out = await run_agent("shop", "I need an everyday tote under $400", service)
    by_vendor: dict[str, list[dict]] = {}
    for r in out["results"]:
        by_vendor.setdefault(r["offer"]["vendor"], []).append(r)
    good = by_vendor["Good Bags"]
    assert [r["offer"]["title"] for r in good] == ["Everyday Tote"]  # the $999 tote is over budget
    o = good[0]["offer"]
    assert o["vendor_check"] == "shopify_store" and o["nft"] == "no_registration"
    assert o["store_insights"]["dropship_risk"] == "low" and o["store_insights"]["delivery_days"] == [2, 5]
    drop = by_vendor["Dropbags"][0]
    ins = drop["offer"]["store_insights"]
    assert ins["dropship_risk"] == "high" and ins["delivery_days"] == [3, 25]
    assert {s.split(":")[0] for s in ins["signals"]} >= {"D1", "D2", "D3", "D4", "D5"}
    assert drop["recommendation"] != "recommended"
    assert ("www.goodbags.com", "search_catalog") in shops.calls
    # The store's catalogue results arrive as an unsigned message labelled with their source.
    pitch = next(m for m in out["conversation"] if m["from"] == "shop_goodbags_com")
    assert pitch["body"]["source"] == "shopify_ucp" and "signature" not in pitch


async def test_ordering_a_shopify_offer_hands_off_to_the_store_checkout(service, shops):
    out = await run_agent("shop", "I need an everyday tote under $400", service)
    nxt = await run_agent("shop", "get me the Everyday Tote from Good Bags, pay with crypto", service,
                          session_id=out["session_id"])
    [order] = nxt["orders"]
    assert order["payment_method"] == "shopify_checkout"  # a Shopify store is only paid on its own checkout
    assert order["checkout"]["continue_url"] == f"https://{GOOD}/checkouts/chk_1"
    assert order["nft"] is None and "isn't a GlobalChain partner" in order["ownership"]
    assert order["checkout"]["continue_url"] in nxt["answer"]
    with pytest.raises(ValueError, match="store's checkout"):
        await orders.pay(service, order["order_id"])


async def test_unreachable_store_is_skipped(service, shops, monkeypatch):
    def down(request):
        if request.url.path == "/api/ucp/mcp":
            return httpx.Response(503)
        return shops(request)

    monkeypatch.setattr(shopify, "DEFAULT_TRANSPORT", httpx.MockTransport(down))
    out = await run_agent("shop", "I need an everyday tote under $400", service)
    assert not any(r["offer"]["vendor"] in ("Good Bags", "Dropbags") for r in out["results"])
    assert out["results"]  # the rest of the network still answered


def test_dropship_scoring():
    now = datetime(2026, 10, 27, tzinfo=UTC)
    clean = score_signals("Ships in 1-10 business days worldwide.", "<html/>", ["Cuyana"], now - timedelta(days=5000), now)
    assert clean["dropship_risk"] == "low" and clean["signals"] == []
    none = score_signals(None, "", [], None, now)
    assert [s["id"] for s in none["signals"]] == ["D6"]
    bad = score_signals("Delivery 15-30 days, customs fees may apply.", "zendrop", ["CJ Dropshipping"],
                        now - timedelta(days=60), now)
    assert bad["dropship_risk"] == "high" and bad["score"] == 100


def test_ordinary_copy_is_not_counterfeit_language():
    assert find_terms("Inspired by rainy commutes in Vancouver", COUNTERFEIT_TERMS) == []
    assert find_terms("Designer inspired tote, 1:1 quality", COUNTERFEIT_TERMS) == ["1:1", "designer inspired"]


def test_store_domain_validation():
    assert shopify.store_domain("https://WWW.Example.com/collections/x") == "www.example.com"
    with pytest.raises(ValueError):
        shopify.store_domain("javascript:alert(1)")
