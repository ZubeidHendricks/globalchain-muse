"""Muse Spark agents on GlobalChain: the shopper's agent and the maker's agent.

The shopper's agent broadcasts what the shopper needs to the vendor agents on the network, collects
their signed pitches, checks each vendor's signature and each item's NFT on Hedera, verifies the
items and sellers, and ranks them. When the shopper asks for one, it prepares the order; the
shopper confirms the payment themselves, and the item's NFT moves to them before it ships.

The maker's agent registers an item from its photos and lists it, which makes the maker a verified
vendor whose own agent then pitches it.

Muse Spark drives both through chat-completions tool calling. Without a Model API key, or if a
Muse Spark turn fails, a scripted policy picks the next step from the same run state so the run
still completes. Every trace step says which policy chose it. Conversations carry across turns.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from openai import APIError

from .demo import inline_static_urls, parse_budget_usd, recommend
from .orders import PAYMENT_METHODS, prepare_order
from .provenance import NotFound
from .references import price_in_usd
from .schemas import MakerProduct, ProductVerification, SellerRisk
from .service import GlobalChain
from .store import new_id
from .storefront import store_insights
from .vendors import (
    add_item, add_vendor, answer, check_envelope, check_ownership, conversation, ensure_seeded,
    ensure_shopify_vendors, envelope, item, item_listing, item_seller, pitch, vendor, vendors,
)

log = logging.getLogger(__name__)

MAX_TURNS = 10
_RANK = {"recommended": 0, "caution": 1, "avoid": 2}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


_OFFER = {"offer_id": {"type": "string"}}

SHOP_TOOLS = [
    _fn("broadcast_request", "Send the shopper's need to every vendor agent on the GlobalChain network and collect "
        "their pitches. Each pitch says whether Hedera confirms the vendor's signature and whether the vendor "
        "holds the item's NFT.",
        {"need": {"type": "string", "description": "What the shopper needs, in their words"},
         "max_price": {"type": "number", "description": "Budget, in the currency given"},
         "currency": {"type": "string", "enum": ["USD", "ZAR"]}},
        ["need"]),
    _fn("ask_vendor", "Ask one vendor's agent a follow-up question about an offer.",
        {"vendor_id": {"type": "string"}, "question": {"type": "string"}, "offer_id": {"type": "string"}},
        ["vendor_id", "question"]),
    _fn("verify_product", "SAM 3.1 + Muse Spark check of an offer's photos, compared with the item's origin "
        "fingerprint when it has a registration. Returns verdict, score and cited evidence.", _OFFER, ["offer_id"]),
    _fn("assess_seller_risk", "Score the risk of buying an offer from its vendor (0 low, 100 high).",
        _OFFER, ["offer_id"]),
    _fn("get_registration", "Look up an origin registration: who made the item, its fingerprint and Hedera proof.",
        {"registration_id": {"type": "string"}}, ["registration_id"]),
    _fn("present_recommendations", "Show the shopper your ranked shortlist. Call once, after checking every "
        "offer, listing each checked offer best first.",
        {"items": {"type": "array", "items": {"type": "object", "properties": {
            "offer_id": {"type": "string"},
            "recommendation": {"type": "string", "enum": ["recommended", "caution", "avoid"]},
            "reason": {"type": "string", "description": "One sentence citing evidence or signal ids"},
        }, "required": ["offer_id", "recommendation", "reason"]}}},
        ["items"]),
    _fn("prepare_order", "Prepare an order for the offer the shopper asked for. Nothing is paid until the shopper "
        "confirms. Only call this when the shopper has asked to buy a specific offer.",
        {"offer_id": {"type": "string"},
         "payment_method": {"type": "string", "enum": list(PAYMENT_METHODS)}},
        ["offer_id", "payment_method"]),
]

MAKE_TOOLS = [
    _fn("register_product", "Register the maker's item at origin. Muse Spark picks its distinctive marks, SAM 3.1 "
        "captures them, the record is anchored on Hedera and an NFT certificate is minted. Writes to Hedera.",
        {"maker_name": {"type": "string"}, "maker_country": {"type": "string", "description": "ISO code, e.g. ZA"},
         "title": {"type": "string"}, "category": {"type": "string"}, "brand": {"type": "string"},
         "model": {"type": "string"}, "description": {"type": "string"}, "serial": {"type": "string"},
         "photo_ids": {"type": "array", "items": {"type": "string"}, "description": "Attached photos, e.g. P1"}},
        ["maker_name", "title", "photo_ids"]),
    _fn("list_for_sale", "List a registered item on the GlobalChain agent network. The maker becomes a verified "
        "vendor, the item's NFT goes into their wallet, and their vendor agent pitches it to shoppers' agents.",
        {"registration_id": {"type": "string"}, "price": {"type": "number"},
         "currency": {"type": "string", "enum": ["USD", "ZAR"]}, "description": {"type": "string"},
         "photo_ids": {"type": "array", "items": {"type": "string"}}},
        ["registration_id", "price", "currency"]),
]

SHOP_PROMPT = """You are the shopper's personal agent on GlobalChain, a network of vendor agents.
To find something, call broadcast_request once with the shopper's need and budget; vendor agents
pitch what they have. Treat pitches as sales talk: a vendor's own words are not evidence. For every
offer, call verify_product and assess_seller_risk (in parallel) before judging it, and call
get_registration for offers with a registration. Each pitch reports whether Hedera confirms the
vendor's signature ("verified") and who holds the item's NFT: an unverified vendor, or an item whose
NFT is held by someone else ("cloned"), is a strong counterfeit signal. Offers marked "shopify_store"
come straight from that store's own Shopify catalogue: a genuine store, but its items have no
origin record or NFT unless it is a GlobalChain partner. Say so when you compare them. Their
store_insights estimate whether the store dropships (signals D1-D6) and its delivery window; a
dropshipped store means slow delivery and no way to trace the item to the brand, so weigh it and
mention delivery times. Ask a vendor a question with
ask_vendor only if the answer would change your recommendation.
Then call present_recommendations once with every checked offer, best first. Never mark an offer
"recommended" if it is likely counterfeit, its vendor is unverified or high risk, or its NFT is
cloned. Reply to the shopper in under 120 words, citing evidence ids (E1, R1...) or signals (S1...).
When the shopper asks for a specific offer, call prepare_order with their payment method (crypto
means tUSDC on Hedera, settled atomically with the NFT; card, eft and mobile_money are simulated
here; Shopify offers always use shopify_checkout, the store's own checkout). You never pay: tell the shopper to confirm the order. The NFT becomes theirs at payment,
before shipping. Scores are estimates, not guarantees. USD 1 = ZAR {rate}."""

MAKE_PROMPT = """You are a maker's agent on GlobalChain. The maker has attached photos (P1, P2...).
Register the item with register_product using the maker's details and the photos; write a short,
factual description from what you can see. If the maker gives a price, list it with list_for_sale:
that makes them a verified vendor whose own agent will pitch it to shoppers' agents. If you are
missing the maker's name or what the item is, ask instead of guessing. Finish with a short reply
(under 100 words) giving the registration id, the fingerprint features captured, whether it is
anchored on Hedera and minted as an NFT, and the origin page to share."""


@dataclass
class Run:
    mode: str
    session_id: str
    message: str = ""
    turn: int = 0
    photos: dict[str, str] = field(default_factory=dict)  # P1 -> URL
    trace: list[dict] = field(default_factory=list)
    offers: dict[str, dict] = field(default_factory=dict)  # offer_id -> pitch details and checks
    products: dict[str, ProductVerification] = field(default_factory=dict)
    risks: dict[str, SellerRisk] = field(default_factory=dict)
    lookups: set[str] = field(default_factory=set)
    recommendations: list[dict] | None = None
    orders: list[dict] = field(default_factory=list)
    registration: dict | None = None
    listing: dict | None = None
    broadcast: bool = False
    ordered_turn: int = -1
    batch_errors: list[str] = field(default_factory=list)  # errors from the latest round of tool calls
    fallback_used: bool = False
    calls: int = 0


# --- shopper tools --------------------------------------------------------------------------------

async def _broadcast(run: Run, service: GlobalChain, need: str, max_price: float | None = None,
                     currency: str = "USD") -> dict:
    await ensure_seeded(service)
    ensure_shopify_vendors(service)
    rate = service.settings.usd_zar_rate
    budget = None if max_price is None else round(max_price / rate if currency == "ZAR" else max_price, 2)
    request = {"need": need, "budget_usd": budget}
    envelope(service, run.session_id, "shopper", "all", "request", request)
    directory = vendors(service.store)
    shops = [v for v in directory if v.get("kind") == "shopify"]
    pitches, insights = await asyncio.gather(
        asyncio.gather(*(pitch(service, v, run.session_id, request) for v in directory)),
        asyncio.gather(*(store_insights(service, v["store_domain"]) for v in shops)))
    insight_by_vendor = {v["vendor_id"]: i for v, i in zip(shops, insights)}
    run.broadcast = True
    out = []
    for env in pitches:
        v = vendor(service.store, env["from"])
        sender = await check_envelope(service, env)
        offers = []
        for o in env["body"]["offers"]:
            i = item(service.store, o["item_id"])
            owner = await check_ownership(service, i)
            usd = round(price_in_usd(item_listing(i), rate), 2)
            offer = {"offer_id": i["item_id"], "vendor_id": v["vendor_id"], "vendor": v["name"], "title": i["title"],
                     "price": i["price"], "currency": i["currency"], "price_usd": usd, "pitch": o["pitch"],
                     "registration_id": i.get("registration_id"), "vendor_check": sender["status"],
                     "nft": owner["status"], "nft_detail": owner["detail"]}
            if v["vendor_id"] in insight_by_vendor:
                ins = insight_by_vendor[v["vendor_id"]]
                offer["store_insights"] = {"dropship_risk": ins["dropship_risk"], "score": ins["score"],
                                           "delivery_days": ins["delivery_days"],
                                           "signals": [f"{s['id']}: {s['detail']}" for s in ins["signals"]]}
            run.offers[i["item_id"]] = {**offer, "message_id": env["id"], "vendor_detail": sender["detail"]}
            offers.append(offer)
        out.append({"vendor_id": v["vendor_id"], "vendor": v["name"], "vendor_check": sender["status"],
                    "vendor_detail": sender["detail"], "message": env["body"]["message"], "offers": offers})
    return {"budget_usd": budget, "vendors_asked": len(pitches),
            "pitches": [p for p in out if p["offers"]],
            "declined": [p["vendor"] for p in out if not p["offers"]]}


def _offer(run: Run, offer_id: str) -> dict:
    if offer_id not in run.offers:
        raise ValueError(f"No offer {offer_id!r}; use an offer_id from broadcast_request")
    return run.offers[offer_id]


async def _ask(run: Run, service: GlobalChain, vendor_id: str, question: str, offer_id: str | None = None) -> dict:
    v = vendor(service.store, vendor_id)
    envelope(service, run.session_id, "shopper", vendor_id, "question", {"question": question, "item_id": offer_id})
    env = await answer(service, v, run.session_id, question, offer_id)
    check = await check_envelope(service, env)
    return {"vendor": v["name"], "answer": env["body"]["answer"], "vendor_check": check["status"]}


async def _verify(run: Run, service: GlobalChain, offer_id: str) -> dict:
    _offer(run, offer_id)
    p = await service.verify_product(item_listing(item(service.store, offer_id), inline=True))
    run.products[offer_id] = p
    return {"offer_id": offer_id, "assessment_id": p.assessment_id, "verdict": p.verdict,
            "authenticity_score": p.authenticity_score, "confidence": p.confidence, "summary": p.summary,
            "findings": [f.model_dump() for f in p.findings][:8], "missing_evidence": p.missing_evidence,
            "registration_status": p.registration_status, "rule_based": p.fallback_used}


async def _risk(run: Run, service: GlobalChain, offer_id: str) -> dict:
    o = _offer(run, offer_id)
    i = item(service.store, offer_id)
    r = await service.assess_seller_risk(item_listing(i), item_seller(vendor(service.store, o["vendor_id"])))
    run.risks[offer_id] = r
    return {"offer_id": offer_id, "assessment_id": r.assessment_id, "risk_score": r.risk_score,
            "risk_level": r.risk_level, "explanation": r.explanation,
            "signals": [{"id": s.id, "detail": s.detail, "weight": s.weight} for s in r.signals if s.weight],
            "rule_based": r.fallback_used}


def _registration_summary(reg) -> dict:
    return {"registration_id": reg.registration_id, "product": reg.product,
            "fingerprint": [{"id": f.id, "feature": f.feature, "why": reg.rationale.get(f.feature, "")}
                            for f in reg.fingerprint],
            "anchor": reg.anchor.status, "certificate": reg.certificate.status,
            "certificate_url": reg.certificate.explorer_url, "origin_url": reg.origin_url}


async def _lookup(run: Run, service: GlobalChain, registration_id: str) -> dict:
    run.lookups.add(registration_id)
    try:
        return _registration_summary(service.get_registration(registration_id))
    except NotFound:
        return {"registration_id": registration_id, "found": False,
                "note": "No such registration. A QR code or label citing it may be fake."}


def _flagged(run: Run, offer_id: str) -> str | None:
    """Why an offer must not be recommended or bought, if it must not."""
    o, p, r = run.offers.get(offer_id, {}), run.products.get(offer_id), run.risks.get(offer_id)
    if o.get("vendor_check") not in ("verified", "shopify_store"):
        return "the vendor is not verified on Hedera"
    if o.get("nft") == "cloned":
        return "its NFT belongs to another vendor (copied QR code)"
    if p and p.verdict == "likely_counterfeit":
        return "the photos look counterfeit"
    if r and r.risk_level == "high":
        return "the seller is high risk"
    return None


def _guard(item_: dict, run: Run) -> dict:
    """Muse Spark ranks, but it cannot recommend what the checks rule out."""
    why = _flagged(run, item_["offer_id"])
    insights = run.offers.get(item_["offer_id"], {}).get("store_insights") or {}
    if not why and insights.get("dropship_risk") == "high":
        why = "the store looks dropshipped, so delivery is slow and the item can't be traced to the brand"
    if item_["recommendation"] == "recommended" and why:
        return {**item_, "recommendation": "caution", "overridden": True,
                "reason": item_["reason"] + f" (Downgraded: {why}.)"}
    return item_


async def _present(run: Run, service: GlobalChain, items: list[dict]) -> dict:
    unchecked = [i["offer_id"] for i in items if i["offer_id"] not in run.products]
    if unchecked:
        raise ValueError(f"Run verify_product and assess_seller_risk first: {', '.join(unchecked)}")
    run.recommendations = [_guard(i, run) for i in items if i.get("recommendation") in _RANK]
    return {"shown": len(run.recommendations),
            "overridden": [i["offer_id"] for i in run.recommendations if i.get("overridden")]}


async def _order(run: Run, service: GlobalChain, offer_id: str, payment_method: str) -> dict:
    _offer(run, offer_id)
    if offer_id not in run.products:
        raise ValueError("Verify the offer before ordering it")
    why = _flagged(run, offer_id)
    if why:
        raise ValueError(f"Won't order this: {why}")
    if vendor(service.store, run.offers[offer_id]["vendor_id"]).get("kind") == "shopify":
        payment_method = "shopify_checkout"  # a Shopify store is only ever paid on its own checkout
    order = await prepare_order(service, offer_id, payment_method, run.session_id)
    run.orders.append(order)
    run.ordered_turn = run.turn
    if order["payment_method"] == "shopify_checkout":
        nxt = f"The shopper pays on the store's checkout. {order['ownership']}"
    else:
        nxt = "The shopper must confirm the payment. The NFT transfers to them when they do."
    return {"order_id": order["order_id"], "title": order["title"], "vendor": order["vendor_name"],
            "amount_usd": order["amount_usd"], "payment": order["payment_label"], "status": order["status"],
            "next": nxt}


# --- maker tools ----------------------------------------------------------------------------------

def _photos(run: Run, ids: list[str] | None) -> list[str]:
    ids = ids or list(run.photos)
    missing = [i for i in ids if i not in run.photos]
    if missing:
        raise ValueError(f"Unknown photo ids {missing}; attached: {list(run.photos)}")
    return [run.photos[i] for i in ids]


async def _register(run: Run, service: GlobalChain, photo_ids: list[str] | None = None, **fields) -> dict:
    fields = {k: v for k, v in fields.items() if v not in (None, "") and k in MakerProduct.model_fields}
    product = MakerProduct(**fields, image_urls=inline_static_urls(_photos(run, photo_ids)))
    reg = await service.register_product(product)
    run.registration = reg.model_dump()
    out = _registration_summary(reg)
    out["qr_url"] = reg.qr_url
    return out


async def _list(run: Run, service: GlobalChain, registration_id: str, price: float, currency: str = "USD",
                description: str | None = None, photo_ids: list[str] | None = None) -> dict:
    reg = service.get_registration(registration_id)
    p = reg.product
    slug = re.sub(r"[^a-z0-9]+", "_", p["maker_name"].lower()).strip("_")
    v = service.store.get("vendor", f"v_maker_{slug}") or await add_vendor(
        service, f"v_maker_{slug}", p["maker_name"], p.get("maker_country"),
        persona=f"{p['maker_name']} makes and sells their own work. Proud of the craft, honest about details.",
        seller={"seller_id": f"maker-{slug}", "name": p["maker_name"], "account_age_days": 0, "review_count": 0,
                "ships_from": p.get("maker_country"), "payment_methods": list(PAYMENT_METHODS)},
        verified=True)
    i = await add_item(service, v, {"title": p["title"], "brand": p.get("brand"), "model": p.get("model"),
                                    "category": p.get("category", "handbag"),
                                    "description": description or p.get("description") or "", "price": price,
                                    "currency": currency, "photos": _photos(run, photo_ids)}, registration=reg)
    run.listing = {"listing_id": i["item_id"], "vendor_id": v["vendor_id"], "wallet": v["wallet"]["account_id"],
                   "nft": i["nft"], "price": price, "currency": currency, "title": i["title"]}
    return {"offer_id": i["item_id"], "vendor_id": v["vendor_id"], "wallet": v["wallet"]["account_id"],
            "nft": i["nft"], "title": i["title"], "price": price, "currency": currency}


TOOLS = {"broadcast_request": _broadcast, "ask_vendor": _ask, "verify_product": _verify,
         "assess_seller_risk": _risk, "get_registration": _lookup, "present_recommendations": _present,
         "prepare_order": _order, "register_product": _register, "list_for_sale": _list}

# What the trace shows for each tool, from its result.
_BRIEF = {
    "broadcast_request": lambda r: (f"{r['vendors_asked']} vendor agents asked · "
                                    f"{sum(len(p['offers']) for p in r['pitches'])} offer(s) pitched"
                                    + (f" · declined: {', '.join(r['declined'])}" if r["declined"] else "")),
    "ask_vendor": lambda r: f"{r['vendor']}: {r['answer'][:120]}",
    "verify_product": lambda r: f"{r['verdict'].replace('_', ' ')} · {r['authenticity_score']}/100"
                                + (f" · origin {r['registration_status']}" if r["registration_status"] != "none" else ""),
    "assess_seller_risk": lambda r: f"{r['risk_level']} risk · {r['risk_score']}/100",
    "get_registration": lambda r: (f"made by {r['product']['maker_name']}, {len(r['fingerprint'])} fingerprint features"
                                   if "product" in r else "registration not found"),
    "present_recommendations": lambda r: f"{r['shown']} shown" + (f", {len(r['overridden'])} downgraded by guardrail"
                                                                  if r["overridden"] else ""),
    "prepare_order": lambda r: f"{r['order_id']} · {r['title']} · ${r['amount_usd']:,.2f} by {r['payment']} · "
                               "awaiting your confirmation",
    "register_product": lambda r: f"{r['registration_id']} · {len(r['fingerprint'])} features · "
                                  f"anchor {r['anchor']} · certificate {r['certificate']}",
    "list_for_sale": lambda r: f"{r['offer_id']} at {r['currency']} {r['price']:,.0f} · NFT in wallet {r['wallet']}",
}


def _tools(mode: str) -> list[dict]:
    return SHOP_TOOLS if mode == "shop" else MAKE_TOOLS


async def _execute(run: Run, service: GlobalChain, call: dict, policy: str) -> str:
    name = call["function"]["name"]
    step = {"kind": "call", "tool": name, "policy": policy, "args": {}}
    run.trace.append(step)  # before awaiting, so the trace keeps the order the agent asked in
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
        step["args"] = args
        if name not in TOOLS or name not in {t["function"]["name"] for t in _tools(run.mode)}:
            raise ValueError(f"Unknown tool {name!r}")
        result = await TOOLS[name](run, service, **args)
        step["summary"] = _BRIEF[name](result)
    except (ValueError, TypeError, LookupError) as exc:
        result = {"error": str(exc)}
        step["error"] = str(exc)
    run.calls += 1
    return json.dumps(result, default=str)


# --- policies -------------------------------------------------------------------------------------

class MusePolicy:
    def __init__(self, service: GlobalChain):
        self.engine = service.engine
        self.name = service.engine.models["agent"]

    async def next(self, run: Run, messages: list[dict]) -> dict:
        return await self.engine.chat("agent", messages, _tools(run.mode))


_BUY = re.compile(r"\b(get|buy|order|purchase|take|grab)\b", re.I)
_PAY = [("crypto", r"crypto|usdc|hbar|stablecoin|wallet"), ("mobile_money", r"mobile money|m-?pesa|momo"),
        ("eft", r"\beft\b|bank transfer"), ("card", r"\bcard\b|visa|mastercard")]


class ScriptedPolicy:
    """Deterministic stand-in for Muse Spark: the same tools, in a fixed order, from the run state."""

    name = "scripted"

    def __init__(self, service: GlobalChain):
        self.service = service
        self._n = 0

    def _call(self, name: str, **args) -> dict:
        self._n += 1
        return {"id": f"call_s{self._n}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}

    def _say(self, text: str) -> dict:
        return {"role": "assistant", "content": text}

    def _calls(self, calls: list[dict]) -> dict:
        return {"role": "assistant", "content": None, "tool_calls": calls}

    async def next(self, run: Run, messages: list[dict]) -> dict:
        if run.batch_errors:  # retrying the same fixed step would only fail again
            return self._say(f"I couldn't finish: {run.batch_errors[-1]}")
        return self._shop(run) if run.mode == "shop" else self._make(run)

    def _pick(self, run: Run) -> str | None:
        words = set(re.findall(r"[a-z0-9]+", run.message.lower())) - {"get", "me", "the", "a", "buy", "one", "with"}
        ranked = [i["offer_id"] for i in run.recommendations or []]
        scored = sorted(((len(words & set(re.findall(r"[a-z0-9]+", f"{o['title']} {o['vendor']}".lower()))), oid)
                         for oid, o in run.offers.items()), reverse=True)
        if scored and scored[0][0] > 0:
            return scored[0][1]
        return next((oid for oid in ranked if not _flagged(run, oid)), None)

    def _shop(self, run: Run) -> dict:
        wants_to_buy = bool(_BUY.search(run.message)) and run.recommendations is not None
        if wants_to_buy and run.ordered_turn != run.turn:
            offer_id = self._pick(run)
            if offer_id:
                method = next((m for m, pat in _PAY if re.search(pat, run.message, re.I)), "crypto")
                return self._calls([self._call("prepare_order", offer_id=offer_id, payment_method=method)])
        if run.ordered_turn == run.turn and run.orders and run.orders[-1]["payment_method"] == "shopify_checkout":
            o = run.orders[-1]
            return self._say(f"I started a checkout for {o['title']} at {o['vendor_name']}. Finish paying on the store's "
                             f"own checkout: {o['checkout']['continue_url']} {o['ownership']}")
        if run.ordered_turn == run.turn and run.orders:
            o = run.orders[-1]
            return self._say(f"Order {o['order_id']} is ready: {o['title']} from {o['vendor_name']}, "
                             f"${o['amount_usd']:,.2f} by {o['payment_label']}. Confirm the payment below; the "
                             "item's NFT moves to your wallet the moment you do, before it ships.")
        if not run.broadcast:
            budget = parse_budget_usd(run.message, self.service.settings.usd_zar_rate)
            args = {"need": run.message} | ({"max_price": round(budget, 2), "currency": "USD"} if budget else {})
            return self._calls([self._call("broadcast_request", **args)])
        calls = []
        for oid, o in run.offers.items():
            if oid not in run.products:
                calls.append(self._call("verify_product", offer_id=oid))
            if oid not in run.risks:
                calls.append(self._call("assess_seller_risk", offer_id=oid))
            if o["registration_id"] and o["registration_id"] not in run.lookups:
                run.lookups.add(o["registration_id"])  # one lookup per registration, even if two offers cite it
                calls.append(self._call("get_registration", registration_id=o["registration_id"]))
        if calls:
            return self._calls(calls)
        if run.recommendations is None:
            items = []
            for oid in run.offers:
                action, reason = recommend(run.products[oid], run.risks[oid])
                why = _flagged(run, oid)
                if why:
                    action, reason = "avoid", f"Avoid: {why}."
                elif (run.offers[oid].get("store_insights") or {}).get("dropship_risk") == "high":
                    action, reason = "caution", "The store looks dropshipped: expect slow delivery and no brand traceability."
                items.append({"offer_id": oid, "recommendation": action, "reason": reason})
            items.sort(key=lambda i: (_RANK[i["recommendation"]], -run.products[i["offer_id"]].authenticity_score))
            return self._calls([self._call("present_recommendations", items=items)])
        if not run.recommendations:
            return self._say("No vendor agent had anything that fits your request and budget.")
        top = run.recommendations[0]
        o = run.offers[top["offer_id"]]
        return self._say(f"Best option: {o['title']} from {o['vendor']} ({top['recommendation']}). {top['reason']} "
                         "Say \"get me\" one of them and how you'd like to pay; scores are estimates, not guarantees.")

    def _make(self, run: Run) -> dict:
        if run.registration is None:
            if not run.photos:
                return self._say("Please attach at least one clear photo of the item so I can register it.")
            name = re.search(r"\b(?:I am|I'm|my name is)\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*)", run.message)
            title = re.search(r"register (?:my |this |our )?(.+?)(?:\s+and\b|\s+for\b|[.,]|$)", run.message, re.I)
            if not name or not title:
                return self._say("Tell me your name and what the item is (for example, \"I'm Lerato Dlamini. "
                                 "Register my beaded necklace\") and I'll register it.")
            country = "ZA" if re.search(r"south africa|johannesburg|cape town|durban|soweto", run.message, re.I) else None
            return self._calls([self._call("register_product", maker_name=name.group(1), maker_country=country,
                                           title=title.group(1).strip(), photo_ids=list(run.photos))])
        price = re.search(r"(?:for|at)\s+(R|\$|ZAR\s?|USD\s?)\s?([\d,]+(?:\.\d+)?)", run.message, re.I)
        if price and run.listing is None and "registration_id" in run.registration:
            currency = "ZAR" if price.group(1).strip().upper() in ("R", "ZAR") else "USD"
            return self._calls([self._call("list_for_sale", registration_id=run.registration["registration_id"],
                                           price=float(price.group(2).replace(",", "")), currency=currency)])
        reg = run.registration
        listed = (f" Listed as {run.listing['listing_id']}; its NFT is in your wallet {run.listing['wallet']} and your "
                  "vendor agent will pitch it to shoppers' agents." if run.listing else "")
        return self._say(f"Registered {reg['registration_id']} with {len(reg['fingerprint'])} fingerprint features. "
                         f"Hedera: {reg['anchor']['status']}; certificate: {reg['certificate']['status']}.{listed} "
                         f"Share the origin page: {reg['origin_url']}")


# --- sessions and the loop ------------------------------------------------------------------------

_SESSIONS: dict[str, tuple[Run, list[dict]]] = {}  # in-memory: a demo server's conversations


def _user_message(run: Run, text: str, photos: dict[str, str]) -> dict:
    if not photos:
        return {"role": "user", "content": text}
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for pid, url in photos.items():
        content.append({"type": "text", "text": f"Photo {pid}:"})
        content.append({"type": "image_url", "image_url": {"url": inline_static_urls([url])[0]}})
    return {"role": "user", "content": content}


def _results(run: Run, service: GlobalChain) -> list[dict]:
    """Cards for the web demo, in the agent's order; checked offers it left out go last."""
    order = [i["offer_id"] for i in run.recommendations or []]
    order += [oid for oid in run.products if oid not in order]
    by_id = {i["offer_id"]: i for i in run.recommendations or []}
    out = []
    for oid in order:
        if oid not in run.products or oid not in run.risks:
            continue
        o = run.offers[oid]
        i = item(service.store, oid)
        v = vendor(service.store, o["vendor_id"])
        p, r = run.products[oid], run.risks[oid]
        action, reason = recommend(p, r)
        rec = by_id.get(oid, {"recommendation": action, "reason": reason})
        out.append({"listing_id": oid, "offer": o, "listing": item_listing(i).model_dump(),
                    "seller": item_seller(v).model_dump(), "price_usd": o["price_usd"],
                    "product": p.model_dump(), "seller_risk": r.model_dump(),
                    "recommendation": rec["recommendation"], "reason": rec["reason"],
                    "overridden": rec.get("overridden", False), "status": i["status"]})
    return out


async def run_agent(mode: str, message: str, service: GlobalChain, photos: list[str] | None = None,
                    session_id: str | None = None) -> dict:
    if mode not in ("shop", "make"):
        raise ValueError("mode must be 'shop' or 'make'")
    if session_id and session_id in _SESSIONS and _SESSIONS[session_id][0].mode == mode:
        run, messages = _SESSIONS[session_id]
        run.turn += 1
    else:
        run = Run(mode=mode, session_id=new_id("conv"))
        prompt = SHOP_PROMPT.format(rate=service.settings.usd_zar_rate) if mode == "shop" else MAKE_PROMPT
        messages = [{"role": "developer", "content": prompt}]
        _SESSIONS[run.session_id] = (run, messages)
    new_photos = {f"P{len(run.photos) + i + 1}": u for i, u in enumerate(photos or [])}
    run.photos.update(new_photos)
    run.message, run.batch_errors = message, []
    messages.append(_user_message(run, message, new_photos))
    trace_start, orders_start = len(run.trace), len(run.orders)

    scripted = ScriptedPolicy(service)
    policy = scripted if service.engine.name == "mock" else MusePolicy(service)
    answer_text = None
    for _ in range(MAX_TURNS):
        try:
            msg = await policy.next(run, messages)
        except APIError as exc:  # keep going on the scripted policy rather than leave the shopper stranded
            log.warning("Muse Spark agent turn failed (%s); continuing on the scripted policy", exc)
            run.trace.append({"kind": "note", "text": f"Muse Spark unavailable ({type(exc).__name__}); "
                                                      "continuing with the scripted policy"})
            run.fallback_used, policy = True, scripted
            msg = await policy.next(run, messages)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if msg.get("content") and calls:
            run.trace.append({"kind": "think", "text": msg["content"], "policy": policy.name})
        if not calls:
            answer_text = msg.get("content") or ""
            break
        start = len(run.trace)
        results = await asyncio.gather(*(_execute(run, service, c, policy.name) for c in calls))
        run.batch_errors = [t["error"] for t in run.trace[start:] if t.get("error")]
        messages += [{"role": "tool", "tool_call_id": c["id"], "content": res} for c, res in zip(calls, results)]
    else:
        answer_text = "I stopped after the step limit. Here is what I checked so far."
    return {"session_id": run.session_id, "mode": mode, "engine": service.engine.name,
            "model": service.engine.models["agent"], "policy": policy.name, "fallback_used": run.fallback_used,
            "tool_calls": run.calls, "trace": run.trace[trace_start:], "answer": answer_text,
            "results": _results(run, service), "orders": run.orders[orders_start:],
            "registration": run.registration, "listing": run.listing,
            "conversation": conversation(service.store, run.session_id) if mode == "shop" else []}
