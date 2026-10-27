import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from globalchain import shopify, shopify_app
from globalchain.demo import inline_static_urls
from globalchain.shopify import item_id_for
from globalchain.vendors import BUYER_WALLET, ensure_seeded

SHOP = "lerato-bags.myshopify.com"
SECRET = "test-app-secret"
PRODUCT = "gid://shopify/Product/101"
PHOTO = inline_static_urls(["/static/demo/tabby-a-1.png"])[0]


class FakeAdmin:
    """A Shopify store's OAuth and Admin GraphQL endpoints."""

    def __init__(self):
        self.queries: list[str] = []
        self.token_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == SHOP
        if request.url.path == "/admin/oauth/access_token":
            self.token_requests += 1
            assert json.loads(request.content)["client_secret"] == SECRET
            return httpx.Response(200, json={"access_token": "shpat_test", "scope": shopify_app.SCOPES})
        assert request.url.path == f"/admin/api/{shopify_app.API_VERSION}/graphql.json"
        assert request.headers["X-Shopify-Access-Token"] == "shpat_test"
        query = json.loads(request.content)["query"]
        self.queries.append(query)
        if "shop {" in query:
            return httpx.Response(200, json={"data": {"shop": {"name": "Lerato Bags",
                                                               "billingAddress": {"countryCodeV2": "ZA"}}}})
        if "webhookSubscriptionCreate" in query:
            return httpx.Response(200, json={"data": {"webhookSubscriptionCreate": {"userErrors": []}}})
        if "products(" in query:
            return httpx.Response(200, json={"data": {"products": {"nodes": [
                {"id": PRODUCT, "title": "Woven Market Tote", "vendor": "Lerato Bags", "productType": "Bag",
                 "description": "Hand-woven tote with a brass maker's tag.", "featuredMedia": None,
                 "media": {"nodes": [{"image": {"url": PHOTO}}]},
                 "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/1010", "price": "180.00"}]}},
                {"id": "gid://shopify/Product/102", "title": "Gift card", "vendor": "Lerato Bags",
                 "productType": "", "description": "", "featuredMedia": None, "media": {"nodes": []},
                 "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/1020", "price": "50.00"}]}}]}}})
        return httpx.Response(400, json={"errors": ["unexpected query"]})


def _signed(params: dict) -> dict:
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return {**params, "hmac": hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()}


def _webhook_sig(body: bytes) -> str:
    return base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


@pytest.fixture
def admin(service, monkeypatch):
    fake = FakeAdmin()
    monkeypatch.setattr(shopify, "DEFAULT_TRANSPORT", httpx.MockTransport(fake))
    service.settings = service.settings.model_copy(update={"shopify_api_key": "test-key",
                                                           "shopify_api_secret": SECRET})
    return fake


async def _install(service) -> dict:
    state = parse_qs(urlparse(shopify_app.install_url(service, SHOP)).query)["state"][0]
    return await shopify_app.complete_install(service, _signed({"shop": SHOP, "code": "abc", "state": state,
                                                                "timestamp": "1761600000"}))


def test_install_url_asks_shopify_for_read_only_scopes(service, admin):
    url = urlparse(shopify_app.install_url(service, "Lerato-Bags.myshopify.com"))
    q = parse_qs(url.query)
    assert url.netloc == SHOP and url.path == "/admin/oauth/authorize"
    assert q["scope"] == ["read_products,read_orders"] and q["client_id"] == ["test-key"]
    assert q["redirect_uri"] == ["http://localhost:8000/shopify/callback"]
    assert service.store.get("shopify_oauth_state", q["state"][0])["shop"] == SHOP
    for bad in ("evil.com", "shop.myshopify.com.evil.com", "https://x.myshopify.com/admin"):
        with pytest.raises(ValueError):
            shopify_app.install_url(service, bad)


def test_app_without_credentials_refuses_to_install(service):
    with pytest.raises(shopify_app.ShopifyAuthError, match="isn't configured"):
        shopify_app.install_url(service, SHOP)


async def test_install_makes_the_store_a_verified_partner_with_a_wallet(service, admin):
    v = await _install(service)
    assert v["vendor_id"] == "shop_lerato_bags_myshopify_com"
    assert v["verified"] and v["partner"] and v["kind"] == "shopify" and v["store_domain"] == SHOP
    assert v["wallet"]["account_id"]
    assert any("webhookSubscriptionCreate" in q for q in admin.queries)
    assert service.store.get("shopify_token", SHOP)["access_token"] == "shpat_test"


async def test_callback_rejects_forged_signature_and_reused_or_foreign_state(service, admin):
    state = parse_qs(urlparse(shopify_app.install_url(service, SHOP)).query)["state"][0]
    params = {"shop": SHOP, "code": "abc", "state": state, "timestamp": "1761600000"}
    with pytest.raises(shopify_app.ShopifyAuthError, match="signature"):
        await shopify_app.complete_install(service, {**params, "hmac": "00" * 32})
    with pytest.raises(shopify_app.ShopifyAuthError, match="expired"):
        await shopify_app.complete_install(service, _signed({**params, "state": "made-up"}))
    with pytest.raises(shopify_app.ShopifyAuthError, match="expired"):
        await shopify_app.complete_install(service, _signed({**params, "shop": "other.myshopify.com"}))
    assert admin.token_requests == 0
    await shopify_app.complete_install(service, _signed(params))
    with pytest.raises(shopify_app.ShopifyAuthError, match="expired"):  # each state works once
        await shopify_app.complete_install(service, _signed(params))
    assert admin.token_requests == 1


async def test_partner_registers_a_product_and_its_nft_lands_in_their_wallet(service, admin):
    v = await _install(service)
    products = await shopify_app.partner_products(service, v["vendor_id"])
    assert [p["title"] for p in products] == ["Woven Market Tote", "Gift card"]
    reg = await shopify_app.register_partner_product(service, v["vendor_id"], PRODUCT)
    item = service.store.get("item", item_id_for(SHOP, PRODUCT))
    assert item["registration_id"] == reg["registration_id"] and item["vendor_id"] == v["vendor_id"]
    assert await service.ledger.nft_owner(item["nft"]["token_id"], item["nft"]["serial"]) == v["wallet"]["account_id"]
    # Registering again returns the same record rather than minting a second certificate.
    again = await shopify_app.register_partner_product(service, v["vendor_id"], PRODUCT)
    assert again["registration_id"] == reg["registration_id"]
    with pytest.raises(ValueError, match="no photos"):
        await shopify_app.register_partner_product(service, v["vendor_id"], "gid://shopify/Product/102")


def _paid(order_id: int, **extra) -> dict:
    return {"id": order_id, "line_items": [{"product_id": 101, "quantity": 1}, {"product_id": 102, "quantity": 1}],
            **extra}


async def test_paid_order_from_someone_else_mints_into_escrow_once(service, admin):
    v = await _install(service)
    await shopify_app.register_partner_product(service, v["vendor_id"], PRODUCT)
    out = await shopify_app.order_paid(service, SHOP, "wh_1", _paid(5001))
    [m] = out["minted"]  # the unregistered gift card gets nothing
    assert m["escrow"] and m["owner"] == v["wallet"]["account_id"] and out["matched_order"] is None
    assert len(service.store.all("nft_claim")) == 1
    again = await shopify_app.order_paid(service, SHOP, "wh_1", _paid(5001))  # Shopify retries webhooks
    assert again == out and len(service.store.all("nft_claim")) == 1


async def test_paid_order_from_our_agent_mints_to_the_buyer(service, admin):
    await ensure_seeded(service)
    v = await _install(service)
    await shopify_app.register_partner_product(service, v["vendor_id"], PRODUCT)
    service.store.put("order", "ord_test", {"order_id": "ord_test", "status": "awaiting_store_checkout",
                                            "checkout": {"id": "gid://shopify/Checkout/chk_77"}, "events": []})
    out = await shopify_app.order_paid(service, SHOP, "wh_2", _paid(5002, checkout_token="chk_77"))
    buyer = service.store.get("wallet", BUYER_WALLET)["account_id"]
    assert out["matched_order"] == "ord_test" and out["minted"][0]["owner"] == buyer
    order = service.store.get("order", "ord_test")
    assert order["status"] == "owned" and order["owner"] == buyer and order["events"][-1]["status"] == "owned"


async def test_webhook_from_a_store_that_never_installed_is_refused(service, admin):
    from globalchain.provenance import NotFound

    with pytest.raises(NotFound):
        await shopify_app.order_paid(service, "stranger.myshopify.com", "wh_3", _paid(1))


def test_partner_session_cookie_cannot_be_forged(service, admin):
    cookie = shopify_app.session_cookie(service.settings, "shop_a")
    assert shopify_app.session_vendor(service.settings, cookie) == "shop_a"
    assert shopify_app.session_vendor(service.settings, cookie.replace("shop_a", "shop_b")) is None
    assert shopify_app.session_vendor(service.settings, "shop_a.deadbeef") is None
    assert shopify_app.session_vendor(service.settings, None) is None


@pytest.fixture
def client(service, admin, monkeypatch):
    from fastapi.testclient import TestClient

    import globalchain.api as api_module

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    with TestClient(api_module.create_app(), base_url="http://localhost:8000") as c:
        yield c


def test_install_and_dashboard_endpoints(client, service):
    r = client.get("/shopify/install", params={"shop": SHOP}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"https://{SHOP}/admin/oauth/authorize")
    assert client.get("/shopify/install", params={"shop": "evil.com"}, follow_redirects=False).status_code == 400
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    params = _signed({"shop": SHOP, "code": "abc", "state": state, "timestamp": "1761600000"})
    vid = shopify_app.partner_id(SHOP)

    client.cookies.clear()
    assert client.get(f"/v1/partners/{vid}").status_code == 403  # not signed in yet
    cb = client.get("/shopify/callback", params=params, follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == f"/partners/{vid}"
    dash = client.get(f"/v1/partners/{vid}").json()
    assert dash["vendor"]["partner"] and [p["title"] for p in dash["products"]][0] == "Woven Market Tote"
    assert client.post(f"/v1/partners/{vid}/register", json={"product_id": PRODUCT}).status_code == 200
    assert client.get("/partners").status_code == 200 and client.get(f"/partners/{vid}").status_code == 200
    # Another store's dashboard needs that store's own install.
    assert client.get("/v1/partners/shop_other_myshopify_com").status_code == 403


def test_webhook_endpoint_checks_shopify_signature(client, service):
    body = json.dumps(_paid(9)).encode()
    headers = {"content-type": "application/json", "X-Shopify-Shop-Domain": SHOP, "X-Shopify-Webhook-Id": "wh_9"}
    bad = client.post("/shopify/webhooks/orders-paid", content=body, headers={**headers, "X-Shopify-Hmac-Sha256": "x"})
    assert bad.status_code == 401
    unknown = client.post("/shopify/webhooks/orders-paid", content=body,
                          headers={**headers, "X-Shopify-Hmac-Sha256": _webhook_sig(body)})
    assert unknown.status_code == 404  # signed correctly, but the store never installed
