"""The GlobalChain Shopify app: how a store owner joins the network as a verified partner.

1. Install. The owner enters their store at /partners and approves the app on Shopify (OAuth
   authorization code grant). Completing OAuth proves they control the store, so GlobalChain
   lists them as a verified partner and creates their Hedera wallet.
2. Register products. From the partner page the owner picks products; GlobalChain fetches each
   product's photos with the Admin API and registers it at origin (Muse Spark + SAM 3.1), with the
   origin certificate NFT held in the store's wallet.
3. Sell. Shoppers' agents find the store's products through its UCP endpoint and buy on its own
   checkout. When Shopify reports an order paid (orders/paid webhook), GlobalChain mints an
   ownership NFT for each registered item: to the buyer's wallet when a GlobalChain agent placed
   the order, otherwise held in the store's wallet as escrow (a buyer claim flow is still to come).

Every request from Shopify is checked with the app secret: the OAuth callback's hmac query
parameter and the webhook's X-Shopify-Hmac-Sha256 header.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from . import shopify
from .provenance import NotFound
from .schemas import MakerProduct
from .shopify import item_id_for
from .store import new_id, now_iso
from .vendors import BUYER_WALLET, add_vendor

log = logging.getLogger(__name__)

SCOPES = "read_products,read_orders"
API_VERSION = "2026-10"
STATE_TTL = timedelta(minutes=10)
# The path Shopify CLI whitelists when it points the app at its dev tunnel.
CALLBACK_PATH = "/auth/callback"
_SHOP = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")


class ShopifyAuthError(PermissionError):
    pass


def valid_shop(shop: str) -> str:
    shop = (shop or "").strip().lower()
    if not _SHOP.fullmatch(shop):
        raise ValueError("Enter your store's myshopify.com address, e.g. your-store.myshopify.com")
    return shop


def _secret(settings) -> bytes:
    if not (settings.shopify_api_key and settings.shopify_api_secret):
        raise ShopifyAuthError("The GlobalChain Shopify app isn't configured (SHOPIFY_API_KEY / SHOPIFY_API_SECRET)")
    return settings.shopify_api_secret.encode()


def verify_query_hmac(settings, params: dict[str, str]) -> None:
    """Shopify signs OAuth redirects: hex HMAC-SHA256 of the other params, sorted, as k=v&k=v."""
    given = params.get("hmac", "")
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in ("hmac", "signature"))
    expected = hmac.new(_secret(settings), message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(given, expected):
        raise ShopifyAuthError("Shopify signature check failed")


def verify_webhook_hmac(settings, body: bytes, header: str | None) -> None:
    expected = base64.b64encode(hmac.new(_secret(settings), body, hashlib.sha256).digest()).decode()
    if not header or not hmac.compare_digest(header, expected):
        raise ShopifyAuthError("Webhook signature check failed")


def install_url(service, shop: str) -> str:
    shop = valid_shop(shop)
    _secret(service.settings)
    state = secrets.token_urlsafe(24)
    service.store.put("shopify_oauth_state", state, {"shop": shop, "created_at": now_iso()})
    query = urlencode({"client_id": service.settings.shopify_api_key, "scope": SCOPES, "state": state,
                       "redirect_uri": f"{service.settings.public_base_url.rstrip('/')}{CALLBACK_PATH}"})
    return f"https://{shop}/admin/oauth/authorize?{query}"


def _http(service) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=service.settings.http_timeout, transport=shopify.DEFAULT_TRANSPORT)


async def admin_graphql(service, shop: str, query: str, variables: dict | None = None) -> dict:
    token = service.store.get("shopify_token", shop)
    if not token:
        raise NotFound(f"{shop} hasn't installed the GlobalChain app")
    async with _http(service) as http:
        resp = await http.post(f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
                               headers={"X-Shopify-Access-Token": token["access_token"]},
                               json={"query": query, "variables": variables or {}})
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise ValueError(f"Shopify Admin API: {data['errors']}")
    return data["data"]


def partner_id(shop: str) -> str:
    return "shop_" + re.sub(r"[^a-z0-9]+", "_", shop).strip("_")


async def complete_install(service, params: dict[str, str]) -> dict:
    """OAuth callback: check Shopify's signature and our state, get a token, onboard the partner."""
    verify_query_hmac(service.settings, params)
    shop = valid_shop(params.get("shop", ""))
    state = service.store.get("shopify_oauth_state", params.get("state", ""))
    fresh = state and datetime.fromisoformat(state["created_at"]) > datetime.now(UTC) - STATE_TTL
    if not fresh or state.get("used") or state["shop"] != shop:
        raise ShopifyAuthError("Install session expired or doesn't match this store; start again from /partners")
    service.store.put("shopify_oauth_state", params["state"], {**state, "used": True})
    async with _http(service) as http:
        resp = await http.post(f"https://{shop}/admin/oauth/access_token", json={
            "client_id": service.settings.shopify_api_key, "client_secret": service.settings.shopify_api_secret,
            "code": params.get("code", "")})
    resp.raise_for_status()
    token = resp.json()
    service.store.put("shopify_token", shop, {"access_token": token["access_token"], "scope": token.get("scope"),
                                              "installed_at": now_iso()})

    info = (await admin_graphql(service, shop, "{ shop { name billingAddress { countryCodeV2 } } }"))["shop"]
    vid = partner_id(shop)
    v = service.store.get("vendor", vid)
    if not v or not v.get("wallet"):
        v = await add_vendor(service, vid, info["name"], (info.get("billingAddress") or {}).get("countryCodeV2"),
                             persona=f"The sales agent for {info['name']}, a GlobalChain partner store on Shopify.",
                             seller={"seller_id": f"shopify-{shop}", "name": info["name"], "ships_from": None,
                                     "payment_methods": ["shopify_checkout"]},
                             verified=True)
    v.update({"kind": "shopify", "partner": True, "store_domain": shop})
    service.store.put("vendor", vid, v)
    await admin_graphql(service, shop, """
        mutation($url: URL!) { webhookSubscriptionCreate(topic: ORDERS_PAID,
          webhookSubscription: {callbackUrl: $url, format: JSON}) { userErrors { message } } }""",
                        {"url": f"{service.settings.public_base_url.rstrip('/')}/shopify/webhooks/orders-paid"})
    return v


PRODUCTS_QUERY = """{ products(first: 25, sortKey: UPDATED_AT, reverse: true) { nodes {
  id title vendor productType description
  featuredMedia { preview { image { url } } }
  media(first: 4) { nodes { ... on MediaImage { image { url } } } }
  variants(first: 1) { nodes { id price } } } } }"""


async def partner_products(service, vid: str) -> list[dict]:
    v = service.store.get("vendor", vid)
    if not v or not v.get("partner"):
        raise NotFound(f"No partner store {vid!r}")
    nodes = (await admin_graphql(service, v["store_domain"], PRODUCTS_QUERY))["products"]["nodes"]
    out = []
    for p in nodes:
        item = service.store.get("item", item_id_for(v["store_domain"], p["id"])) or {}
        photos = [m["image"]["url"] for m in p["media"]["nodes"] if m.get("image")]
        out.append({"product_id": p["id"], "title": p["title"], "vendor": p.get("vendor"), "photos": photos,
                    "price": (p["variants"]["nodes"] or [{}])[0].get("price"),
                    "registration_id": item.get("registration_id"), "description": p.get("description", "")})
    return out


async def register_partner_product(service, vid: str, product_id: str) -> dict:
    """Register one of the partner's products at origin; its certificate NFT goes to their wallet."""
    v = service.store.get("vendor", vid)
    if not v or not v.get("partner"):
        raise NotFound(f"No partner store {vid!r}")
    p = next((x for x in await partner_products(service, vid) if x["product_id"] == product_id), None)
    if p is None:
        raise NotFound(f"No product {product_id!r} in {v['store_domain']}")
    if p["registration_id"]:
        return service.get_registration(p["registration_id"]).model_dump()
    if not p["photos"]:
        raise ValueError("This product has no photos to register")
    reg = await service.register_product(MakerProduct(
        maker_name=v["name"], maker_country=v.get("country"), title=p["title"], brand=p.get("vendor"),
        description=p["description"][:500], image_urls=p["photos"][:4]))
    token, serial, tx = await service.ledger.mint_item(b"", v["wallet"]["account_id"], reg.certificate.model_dump())
    iid = item_id_for(v["store_domain"], product_id)
    item = service.store.get("item", iid) or {
        "item_id": iid, "vendor_id": vid, "key": product_id, "title": p["title"], "brand": p.get("vendor"),
        "model": None, "category": "bag", "description": p["description"][:600],
        "price": float(p["price"] or 0), "currency": "USD", "photos": p["photos"][:2], "status": "available",
        "listed_at": now_iso(), "shopify": {"domain": v["store_domain"], "product_id": product_id}}
    item.update({"registration_id": reg.registration_id,
                 "nft": {"token_id": token, "serial": serial} if tx.status == "success" else None,
                 "nft_issue_tx": tx.model_dump()})
    service.store.put("item", iid, item)
    return reg.model_dump()


def _gid(kind: str, numeric) -> str:
    return f"gid://shopify/{kind}/{numeric}"


async def order_paid(service, shop: str, webhook_id: str | None, payload: dict) -> dict:
    """Mint an ownership NFT for each registered item in a paid order. Safe to receive twice."""
    shop = valid_shop(shop)
    v = service.store.get("vendor", partner_id(shop))
    if not v or not v.get("partner"):
        raise NotFound(f"{shop} isn't a GlobalChain partner")
    key = webhook_id or f"{shop}:{payload.get('id')}"
    done = service.store.get("shopify_webhook", key)
    if done:
        return done
    # A GlobalChain agent's order is matched by its checkout; anyone else's NFT waits in escrow.
    tokens = {str(payload.get(k)) for k in ("checkout_token", "checkout_id", "cart_token", "token") if payload.get(k)}
    ours = next((o for o in service.store.all("order")
                 if (o.get("checkout") or {}).get("id") and str(o["checkout"]["id"]).split("/")[-1] in tokens), None)
    buyer = service.store.get("wallet", BUYER_WALLET) if ours else None
    to_account = buyer["account_id"] if buyer else v["wallet"]["account_id"]
    minted = []
    for line in payload.get("line_items") or []:
        item = service.store.get("item", item_id_for(shop, _gid("Product", line.get("product_id"))))
        if not item or not item.get("registration_id"):
            continue
        for _ in range(max(1, int(line.get("quantity") or 1))):
            meta = f"gcm:own:{item['registration_id']}:{payload.get('id')}"[:100].encode()
            cert = await service.minter.mint(meta)
            token, serial, tx = await service.ledger.mint_item(meta, to_account, cert.model_dump())
            minted.append({"item_id": item["item_id"], "title": item["title"], "token_id": token, "serial": serial,
                           "status": tx.status, "owner": to_account if tx.status == "success" else None,
                           "escrow": not buyer, "tx": tx.model_dump()})
    result = {"webhook": key, "shop": shop, "shopify_order": payload.get("id"), "minted": minted,
              "matched_order": ours["order_id"] if ours else None, "received_at": now_iso()}
    if ours:
        ours.update({"status": "owned", "owner": to_account, "nft": minted[0] if minted else None})
        ours["events"].append({"status": "owned", "detail": f"Paid on {shop}; ownership NFT minted to you",
                               "at": now_iso(), "tx": minted[0]["tx"] if minted else None})
        service.store.put("order", ours["order_id"], ours)
    for m in minted:
        if m["escrow"]:  # held in the store's wallet; a buyer claim flow isn't built yet
            service.store.put("nft_claim", new_id("claim"), {**m, "shop": shop, "shopify_order": payload.get("id")})
    service.store.put("shopify_webhook", key, result)
    return result


# --- partner session: a signed cookie set after a completed install ----------------------------

SESSION_COOKIE = "gc_partner"


def session_cookie(settings, vid: str) -> str:
    sig = hmac.new(_secret(settings), f"partner:{vid}".encode(), hashlib.sha256).hexdigest()
    return f"{vid}.{sig}"


def session_vendor(settings, cookie: str | None) -> str | None:
    """The partner a cookie was issued to, or None if it's missing or forged."""
    if not cookie or "." not in cookie:
        return None
    vid, _, sig = cookie.rpartition(".")
    try:
        ok = hmac.compare_digest(sig, session_cookie(settings, vid).rpartition(".")[2])
    except ShopifyAuthError:
        return None
    return vid if ok else None
