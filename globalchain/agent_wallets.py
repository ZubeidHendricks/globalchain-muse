"""Shopping for other developers' agents (MCP): find checked products and buy with an agent wallet.

Adapted from VeriChainX Sol's agent-wallet buying. A bot gets its own demo wallet funded with tUSDC
up to a spending cap its owner sets, and buys from a shopping session it ran: the purchase goes
through the same checks and guardrail as the shopper's own agent, payment and NFT move in one
Hedera transaction, and the NFT can go straight to the owner's account. Wallets are custodial demo
accounts (testnet or simulated), like the shopper's; the bot holds a secret key for its wallet, and
GlobalChain stores only its hash.
"""

import asyncio
import hashlib
import re
import secrets

from .agent import order_from_session, run_agent, session_offer
from .orders import pay, track
from .store import new_id, now_iso
from .vendors import wallet_view

MAX_CAP_USD = 500.0
_ACCOUNT = re.compile(r"^\d+\.\d+\.\d+$")
_lock = asyncio.Lock()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def create_agent_wallet(service, label: str, spending_cap_usd: float) -> dict:
    if not 0 < spending_cap_usd <= MAX_CAP_USD:
        raise ValueError(f"spending_cap_usd must be more than 0 and at most {MAX_CAP_USD:.0f}")
    if len(service.store.all("agent_wallet")) >= service.settings.agent_wallets_max:
        raise ValueError("No more agent wallets can be created on this demo server")
    label = label.strip()[:60] or "agent"
    w = await service.ledger.create_wallet(f"agent {label}", spending_cap_usd)
    key = "gcw_" + secrets.token_urlsafe(24)
    doc = {"wallet_id": new_id("aw"), "label": label, "key_hash": _hash(key), "wallet": w.model_dump(),
           "cap_usd": spending_cap_usd, "spent_usd": 0.0, "created_at": now_iso()}
    service.store.put("agent_wallet", doc["wallet_id"], doc)
    return {"wallet_id": doc["wallet_id"], "wallet_key": key, "wallet": wallet_view(w),
            "balance_tusdc": await service.ledger.balance_usd(w.account_id), "spending_cap_usd": spending_cap_usd,
            "note": "Keep wallet_key secret: it authorises purchases from this wallet. It is shown once."}


def _wallet(service, wallet_key: str) -> dict:
    h = _hash(wallet_key)
    w = next((w for w in service.store.all("agent_wallet") if secrets.compare_digest(w["key_hash"], h)), None)
    if not w:
        raise ValueError("Unknown wallet_key; call create_agent_wallet first")
    return w


def _view(result: dict) -> dict:
    o = result["offer"]
    return {"offer_id": result["listing_id"], "title": o["title"], "vendor": o["vendor"],
            "price": f"{o['price']:,} {o['currency']}", "price_usd": result["price_usd"],
            "recommendation": result["recommendation"], "reason": result["reason"],
            "scorecard": {"overall": result["scorecard"]["overall"], "grade": result["scorecard"]["grade"],
                          **{r["key"]: {"score": r["score"], "note": r["note"]} for r in result["scorecard"]["rows"]}},
            "vendor_check": o["vendor_check"], "nft": o["nft"], "registration_id": o.get("registration_id"),
            "verdict": result["product"]["verdict"], "seller_risk": result["seller_risk"]["risk_level"],
            "sold": result["status"] == "sold",
            "buy": ("blocked" if result["recommendation"] == "avoid" else
                    "store checkout only" if o["vendor_check"] == "shopify_store" else
                    "buy_with_agent_wallet with session_id and offer_id")}


async def find_products(service, query: str, max_price_usd: float | None = None) -> dict:
    text = f"{query} under ${max_price_usd:g}" if max_price_usd else query
    r = await run_agent("shop", text, service)
    return {"session_id": r["session_id"], "answer": r["answer"], "engine": r["engine"],
            "fallback_used": r["fallback_used"], "results": [_view(x) for x in r["results"]]}


async def buy_with_agent_wallet(service, session_id: str, offer_id: str, wallet_key: str, max_price_usd: float,
                                recipient_account: str | None = None) -> dict:
    w = _wallet(service, wallet_key)
    payer = w["wallet"]["account_id"]
    if recipient_account and not service.ledger.simulated and not _ACCOUNT.match(recipient_account):
        raise ValueError("recipient_account must be a Hedera account id like 0.0.12345")
    offer = session_offer(session_id, offer_id)
    if offer["vendor_check"] == "shopify_store":
        # Checked before ordering: ordering would open a checkout on the real store.
        raise ValueError(f"{offer['vendor']} is a Shopify store: it sells only on its own checkout, "
                         "so an agent wallet can't pay for it")
    async with _lock:  # one purchase at a time, so two calls can't both fit under the cap
        amount = offer["price_usd"]
        if amount > max_price_usd:
            raise ValueError(f"{offer['title']} costs ${amount:,.2f}, over your max_price_usd of ${max_price_usd:,.2f}")
        if w["spent_usd"] + amount > w["cap_usd"]:
            raise ValueError(f"Over the wallet's spending cap: ${w['spent_usd']:,.2f} of ${w['cap_usd']:,.2f} "
                             f"spent, and this costs ${amount:,.2f}")
        order = await order_from_session(service, session_id, offer_id, "crypto")
        amount = order["amount_usd"]
        await pay(service, order["order_id"], payer=payer, recipient=recipient_account or None)
        w["spent_usd"] = round(w["spent_usd"] + amount, 2)
        service.store.put("agent_wallet", w["wallet_id"], w)
    t = await track(service, order["order_id"])
    return {"order_id": t["order_id"], "status": t["status"], "title": t["title"], "vendor": t["vendor_name"],
            "paid_usd": amount, "payer": payer, "nft_owner": t["on_chain_owner"],
            "nft_url": t["nft_url"], "settlement": (t.get("settlement") or {}).get("explorer_url"),
            "simulated": t["simulated_ledger"], "spent_usd": w["spent_usd"], "spending_cap_usd": w["cap_usd"],
            "track_url": f"{service.settings.public_base_url}/track/{t['order_id']}"}


async def order_status(service, order_id: str) -> dict:
    t = await track(service, order_id)
    return {k: t.get(k) for k in ("order_id", "status", "title", "vendor_name", "amount_usd", "payer", "owner",
                                  "on_chain_owner", "owned_by_recipient", "nft_url", "tracking", "simulated_ledger")} | {
        "events": [{"status": e["status"], "detail": e["detail"], "at": e["at"],
                    "hedera": (e.get("anchor") or {}).get("explorer_url")} for e in t["events"]],
        "track_url": f"{service.settings.public_base_url}/track/{order_id}"}
