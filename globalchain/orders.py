"""Orders: the shopper's agent prepares one, the shopper pays, the NFT is theirs, the vendor ships.

Ownership comes first. With crypto, payment and the NFT move in one atomic Hedera transaction; with
a simulated off-chain rail the NFT moves as soon as the payment is marked settled. Only then does
the vendor's agent prepare and ship, and each step is anchored on Hedera against the item's NFT, so
the order's history can be checked the same way verification records are.
"""

import hashlib
import secrets

from .ledger import nft_ref
from .provenance import NotFound, canonical_json
from .references import price_in_usd
from .store import new_id, now_iso
from .shopify import UCPClient, UCPError
from .vendors import BUYER_WALLET, envelope, item, item_listing, vendor

MESSAGE_TYPE = "gcm.order.v1"
PAYMENT_METHODS = {
    "crypto": "tUSDC on Hedera (atomic with the NFT transfer)",
    "card": "Card (simulated in this demo)",
    "eft": "EFT (simulated in this demo)",
    "mobile_money": "Mobile money (simulated in this demo)",
    "shopify_checkout": "The store's own Shopify checkout (Shop Pay, cards and the store's other methods)",
}
# What the vendor's agent does next, and what it tells the shopper's agent.
FULFILMENT = [
    ("preparing", "Payment received and the NFT is yours. We're checking and packing the item now."),
    ("shipped", "Shipped with {carrier}, tracking {tracking}. The parcel is sealed with the NFT reference."),
    ("out_for_delivery", "Out for delivery today."),
    ("delivered", "Delivered. Enjoy it, and keep the NFT: it is the item's ownership record for any resale."),
]


def _event(order: dict, status: str, detail: str, tx: dict | None = None) -> dict:
    return {"status": status, "detail": detail, "at": now_iso(), "tx": tx}


async def _anchor(service, order: dict, event: dict) -> None:
    digest = hashlib.sha256(canonical_json({"order_id": order["order_id"], **event}).encode()).hexdigest()
    nft = order["nft"]
    anchor = await service.anchorer.anchor(canonical_json({
        "t": MESSAGE_TYPE, "o": order["order_id"], "e": event["status"],
        "nft": nft_ref(nft["token_id"], nft["serial"]) if nft else None, "h": digest}))
    event["hash"] = digest
    event["anchor"] = anchor.model_dump()


def get_order(service, order_id: str) -> dict:
    o = service.store.get("order", order_id)
    if not o:
        raise NotFound(f"Unknown order {order_id!r}")
    return o


async def _shopify_order(service, i: dict, v: dict, conversation_id: str | None) -> dict:
    """Build the checkout on the store; the shopper pays there, never through GlobalChain."""
    try:
        checkout = await UCPClient(v["store_domain"], service.settings).create_checkout(i["shopify"]["variant_id"])
    except UCPError as exc:
        raise ValueError(f"Couldn't start a checkout at {v['store_domain']}: {exc}") from exc
    if not checkout.get("continue_url"):
        raise ValueError(f"{v['store_domain']} didn't return a checkout link")
    total = next((t for t in checkout.get("totals") or [] if t.get("type") == "total"), None)
    order = {"order_id": new_id("ord"), "conversation_id": conversation_id, "item_id": i["item_id"],
             "vendor_id": v["vendor_id"], "vendor_name": v["name"], "title": i["title"], "photo": (i["photos"] or [""])[0],
             "price": i["price"], "currency": i["currency"],
             "amount_usd": round(price_in_usd(item_listing(i), service.settings.usd_zar_rate), 2),
             "payment_method": "shopify_checkout", "payment_label": PAYMENT_METHODS["shopify_checkout"],
             "registration_id": i.get("registration_id"), "nft": None, "status": "awaiting_payment",
             "created_at": now_iso(), "events": [], "tracking": None,
             "checkout": {"id": checkout.get("id"), "status": checkout.get("status"),
                          "continue_url": checkout["continue_url"], "total": total},
             "ownership": ("An ownership NFT is minted to you when the store reports the order paid."
                           if v.get("partner") else
                           "This store isn't a GlobalChain partner, so no ownership NFT is issued.")}
    service.store.put("order", order["order_id"], order)
    return order


async def prepare_order(service, item_id: str, payment_method: str, conversation_id: str | None = None) -> dict:
    """A quote awaiting the shopper's confirmation. Nothing is paid or transferred yet."""
    if payment_method not in PAYMENT_METHODS:
        raise ValueError(f"payment_method must be one of {', '.join(PAYMENT_METHODS)}")
    i = item(service.store, item_id)
    v = vendor(service.store, i["vendor_id"])
    if i["status"] != "available":
        raise ValueError(f"{i['title']} is no longer available")
    if v.get("kind") == "shopify":
        return await _shopify_order(service, i, v, conversation_id)
    if not v["verified"] or not i.get("nft"):
        raise ValueError(f"{v['name']} is not a verified vendor holding this item's NFT; GlobalChain won't settle it")
    method = payment_method if payment_method in v["seller"].get("payment_methods", []) else None
    if method is None:
        raise ValueError(f"{v['name']} accepts {', '.join(v['seller']['payment_methods'])}")
    usd = round(price_in_usd(item_listing(i), service.settings.usd_zar_rate), 2)
    order = {"order_id": new_id("ord"), "conversation_id": conversation_id, "item_id": item_id,
             "vendor_id": v["vendor_id"], "vendor_name": v["name"], "title": i["title"], "photo": i["photos"][0],
             "price": i["price"], "currency": i["currency"], "amount_usd": usd, "payment_method": payment_method,
             "payment_label": PAYMENT_METHODS[payment_method], "registration_id": i["registration_id"],
             "nft": i["nft"], "status": "awaiting_payment", "created_at": now_iso(), "events": [],
             "tracking": None}
    service.store.put("order", order["order_id"], order)
    return order


async def pay(service, order_id: str, payer: str | None = None, recipient: str | None = None) -> dict:
    """Run only on the shopper's explicit confirmation. Settles payment and hands over the NFT.

    payer is the paying wallet's account (the shopper's demo wallet by default; an agent wallet for
    buy_with_agent_wallet) and recipient the account the NFT goes to (the payer by default).
    """
    order = get_order(service, order_id)
    if order["status"] != "awaiting_payment":
        return order  # idempotent: a second click does not pay twice
    if order["payment_method"] == "shopify_checkout":
        raise ValueError(f"Pay on the store's checkout: {order['checkout']['continue_url']}")
    i = item(service.store, order["item_id"])
    if i["status"] != "available":
        raise ValueError("This item was sold to someone else")
    v = vendor(service.store, order["vendor_id"])
    seller_acct = v["wallet"]["account_id"]
    payer_acct = payer or service.store.get("wallet", BUYER_WALLET)["account_id"]
    buyer_acct = recipient or payer_acct
    token, serial = order["nft"]["token_id"], order["nft"]["serial"]

    if order["payment_method"] == "crypto":
        tx = await service.ledger.swap(token, serial, seller_acct, payer_acct, order["amount_usd"], receiver=buyer_acct)
        paid_detail = (f"Paid {order['amount_usd']:.2f} tUSDC and received NFT {nft_ref(token, serial)} in one transaction"
                       if buyer_acct == payer_acct else
                       f"{payer_acct} paid {order['amount_usd']:.2f} tUSDC and NFT {nft_ref(token, serial)} went to "
                       f"{buyer_acct}, in one transaction")
    else:
        # Simulated off-chain rail: once "settled", the vendor's NFT moves to the shopper.
        tx = await service.ledger.transfer_nft(token, serial, seller_acct, buyer_acct)
        paid_detail = (f"{PAYMENT_METHODS[order['payment_method']]}: ${order['amount_usd']:.2f} settled; "
                       f"NFT {nft_ref(token, serial)} transferred to you")
    if tx.status != "success":
        order["payment_error"] = tx.detail
        service.store.put("order", order_id, order)
        raise ValueError(f"Payment failed: {tx.detail}")

    i["status"] = "sold"
    i["owner"] = buyer_acct
    service.store.put("item", i["item_id"], i)
    order.pop("payment_error", None)
    order["status"] = "owned"
    order["settlement"] = tx.model_dump()
    order["owner"] = buyer_acct
    order["payer"] = payer_acct
    event = _event(order, "owned", paid_detail, tx.model_dump())
    await _anchor(service, order, event)
    order["events"].append(event)
    service.store.put("order", order_id, order)
    envelope(service, order.get("conversation_id") or order_id, "shopper", v["vendor_id"], "order",
             {"order_id": order_id, "item_id": i["item_id"], "settlement": tx.transaction_id})
    return order


async def advance(service, order_id: str) -> dict:
    """The vendor's agent takes the next fulfilment step and tells the shopper's agent."""
    order = get_order(service, order_id)
    done = {e["status"] for e in order["events"]}
    if "owned" not in done:
        raise ValueError("The order isn't paid yet")
    nxt = next(((s, msg) for s, msg in FULFILMENT if s not in done), None)
    if nxt is None:
        return order
    status, msg = nxt
    if status == "shipped":
        order["tracking"] = {"carrier": "GlobalChain Courier (demo)", "number": "GC" + secrets.token_hex(4).upper()}
        msg = msg.format(carrier=order["tracking"]["carrier"], tracking=order["tracking"]["number"])
    v = vendor(service.store, order["vendor_id"])
    env = envelope(service, order.get("conversation_id") or order_id, v, "shopper", "status",
                   {"order_id": order_id, "status": status, "message": msg})
    event = _event(order, status, msg)
    event["message_id"] = env["id"]
    await _anchor(service, order, event)
    order["events"].append(event)
    order["status"] = status
    service.store.put("order", order_id, order)
    return order


async def track(service, order_id: str) -> dict:
    """The order with Hedera's answer to who owns the item right now."""
    order = get_order(service, order_id)
    nft = order["nft"]
    owner = await service.ledger.nft_owner(nft["token_id"], nft["serial"]) if nft else None
    buyer = service.store.get("wallet", BUYER_WALLET) or {}
    return {**order, "on_chain_owner": owner, "owned_by_you": bool(owner and owner == buyer.get("account_id")),
            "owned_by_recipient": bool(owner and owner == order.get("owner")),
            "simulated_ledger": service.ledger.simulated,
            "nft_url": (f"{service.settings.hashscan_base}/token/{nft['token_id']}/{nft['serial']}"
                        if nft and not service.ledger.simulated else None)}
