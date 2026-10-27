"""Muse Spark agents that work through GlobalChain's tools: a shopper and a maker.

The shopper searches the demo marketplace, runs verify_product and assess_seller_risk on each
candidate (against the item's origin fingerprint when a listing claims a registration) and
recommends. It never approves a purchase; the shopper does that. The maker registers an item from
its photos and lists it for sale.

Muse Spark drives both through chat-completions tool calling. Without a Model API key, or if a
Muse Spark turn fails, a scripted policy picks the next step from the same run state so the run
still completes. Every trace step says which policy chose it.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from openai import APIError

from .demo import inline_static_urls, load_demo_listings, parse_budget_usd, recommend
from .references import price_in_usd
from .schemas import Listing, MakerProduct, ProductVerification, Seller, SellerRisk
from .service import GlobalChain
from .store import new_id

log = logging.getLogger(__name__)

MAX_TURNS = 10
_RANK = {"recommended": 0, "caution": 1, "avoid": 2}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


SHOP_TOOLS = [
    _fn("search_listings", "Search the marketplace. Returns listings within budget with ids to check.",
        {"query": {"type": "string"},
         "max_price": {"type": "number", "description": "Budget, in the currency given"},
         "currency": {"type": "string", "enum": ["USD", "ZAR"]}},
        ["query"]),
    _fn("verify_product", "SAM 3.1 + Muse Spark check of a listing's photos. Compares them with the item's "
        "origin fingerprint when the listing claims a registration_id. Returns verdict, score and cited evidence.",
        {"listing_id": {"type": "string"}}, ["listing_id"]),
    _fn("assess_seller_risk", "Score the risk of buying this listing from its seller (0 low, 100 high).",
        {"listing_id": {"type": "string"}}, ["listing_id"]),
    _fn("get_registration", "Look up an origin registration: who made the item, its fingerprint and Hedera proof.",
        {"registration_id": {"type": "string"}}, ["registration_id"]),
    _fn("present_recommendations", "Show the shopper your ranked recommendations. Call once, after checking "
        "every candidate, listing each checked listing best first.",
        {"items": {"type": "array", "items": {"type": "object", "properties": {
            "listing_id": {"type": "string"},
            "recommendation": {"type": "string", "enum": ["recommended", "caution", "avoid"]},
            "reason": {"type": "string", "description": "One sentence citing evidence or signal ids"},
        }, "required": ["listing_id", "recommendation", "reason"]}}},
        ["items"]),
]

MAKE_TOOLS = [
    _fn("register_product", "Register the maker's item at origin. Muse Spark picks its distinctive marks, SAM 3.1 "
        "captures them, the record is anchored on Hedera and an NFT certificate is minted. Writes to Hedera.",
        {"maker_name": {"type": "string"}, "maker_country": {"type": "string", "description": "ISO code, e.g. ZA"},
         "title": {"type": "string"}, "category": {"type": "string"}, "brand": {"type": "string"},
         "model": {"type": "string"}, "description": {"type": "string"}, "serial": {"type": "string"},
         "photo_ids": {"type": "array", "items": {"type": "string"}, "description": "Attached photos, e.g. P1"}},
        ["maker_name", "title", "photo_ids"]),
    _fn("list_for_sale", "List a registered item on the marketplace. The listing carries the registration id, "
        "so buyers' agents can compare it with the origin fingerprint.",
        {"registration_id": {"type": "string"}, "price": {"type": "number"},
         "currency": {"type": "string", "enum": ["USD", "ZAR"]}, "description": {"type": "string"},
         "photo_ids": {"type": "array", "items": {"type": "string"}}},
        ["registration_id", "price", "currency"]),
]

SHOP_PROMPT = """You are a shopping agent with GlobalChain's trust tools. Find listings that match the
shopper's request, then call verify_product and assess_seller_risk on every candidate (call them in
parallel) before you judge any of them. If a listing claims a registration_id, verify_product compares
it with that item's origin fingerprint; call get_registration to learn who made it. A registration id
that does not exist is a strong counterfeit signal.
Then call present_recommendations once with every checked listing, best first. Never mark a listing
"recommended" if it is likely counterfeit or its seller is high risk. You do not buy anything: the
shopper approves or skips. Finish with a short reply to the shopper (under 120 words): what you
recommend and why, citing evidence ids (E1, R1...) or signal ids (S1...). Scores are estimates, and a
Hedera record proves what was checked and when, not that an item is genuine. USD 1 = ZAR {rate}."""

MAKE_PROMPT = """You are a maker's agent with GlobalChain's origin tools. The maker has attached photos
(P1, P2...). Register the item with register_product using the maker's details and the photos; write a
short, factual description from what you can see. If the maker gives a price, list it with
list_for_sale. If you are missing the maker's name or what the item is, ask instead of guessing.
Finish with a short reply (under 100 words) giving the registration id, what fingerprint features were
captured, whether it is anchored on Hedera and minted as a certificate, and the origin page to share."""


@dataclass
class Run:
    mode: str
    message: str
    photos: dict[str, str] = field(default_factory=dict)  # P1 -> URL
    trace: list[dict] = field(default_factory=list)
    candidates: dict[str, tuple[Listing, Seller]] = field(default_factory=dict)
    products: dict[str, ProductVerification] = field(default_factory=dict)
    risks: dict[str, SellerRisk] = field(default_factory=dict)
    lookups: set[str] = field(default_factory=set)
    recommendations: list[dict] | None = None
    registration: dict | None = None
    listing: dict | None = None
    batch_errors: list[str] = field(default_factory=list)  # errors from the latest round of tool calls
    searched: bool = False
    fallback_used: bool = False
    calls: int = 0


class Marketplace:
    """The demo marketplace: seeded listings plus anything the maker agent lists."""

    def __init__(self, service: GlobalChain):
        self.service = service

    def all(self) -> dict[str, tuple[Listing, Seller]]:
        items = {f"lst_{listing.url.rstrip('/').rsplit('/', 1)[-1]}": (listing, seller)
                 for listing, seller in load_demo_listings()}
        for lid, listing, seller in self.service.store.market_listings():
            items[lid] = (Listing(**listing), Seller(**seller))
        return items

    def add(self, listing: Listing, seller: Seller) -> str:
        lid = new_id("lst")
        self.service.store.save_market_listing(lid, listing.model_dump(), seller.model_dump())
        return lid


# --- tool implementations -----------------------------------------------------------------------

def _summary_listing(lid: str, listing: Listing, seller: Seller, usd: float) -> dict:
    return {"listing_id": lid, "title": listing.title, "price": listing.price, "currency": listing.currency,
            "price_usd": round(usd, 2), "seller": seller.name or seller.seller_id, "ships_from": seller.ships_from,
            "registration_id": listing.registration_id, "photos": len(listing.image_urls)}


_STOP = {"a", "an", "the", "me", "find", "for", "my", "i", "want", "under", "below", "with", "and", "of", "to"}


async def _search(run: Run, service: GlobalChain, query: str, max_price: float | None = None,
                  currency: str = "USD") -> dict:
    rate = service.settings.usd_zar_rate
    budget = None if max_price is None else (max_price / rate if currency == "ZAR" else max_price)
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _STOP and not w.isdigit()}
    within, over = [], 0
    for lid, (listing, seller) in Marketplace(service).all().items():
        usd = price_in_usd(listing, rate)
        if budget is not None and usd > budget:
            over += 1
            continue
        text = f"{listing.title} {listing.brand or ''} {listing.model or ''} {listing.description}".lower()
        within.append((len([w for w in words if w in text]), lid, listing, seller, usd))
    # Keep listings that match the query; if nothing matches, a broad request ("a handbag") gets them all.
    best = max((m for m, *_ in within), default=0)
    hits = [w for w in within if w[0] > 0 or best == 0]
    run.searched = True
    for _, lid, listing, seller, _usd in hits:
        run.candidates[lid] = (listing, seller)
    return {"listings": [_summary_listing(lid, li, se, usd) for _, lid, li, se, usd in hits],
            "over_budget": over, "budget_usd": round(budget, 2) if budget is not None else None}


def _candidate(run: Run, service: GlobalChain, listing_id: str) -> tuple[Listing, Seller]:
    if listing_id not in run.candidates:
        found = Marketplace(service).all().get(listing_id)
        if not found:
            raise ValueError(f"No listing {listing_id!r}; call search_listings for valid ids")
        run.candidates[listing_id] = found
    return run.candidates[listing_id]


async def _verify(run: Run, service: GlobalChain, listing_id: str) -> dict:
    listing, _ = _candidate(run, service, listing_id)
    listing = listing.model_copy(update={"image_urls": inline_static_urls(listing.image_urls)})
    p = await service.verify_product(listing)
    run.products[listing_id] = p
    return {"listing_id": listing_id, "assessment_id": p.assessment_id, "verdict": p.verdict,
            "authenticity_score": p.authenticity_score, "confidence": p.confidence, "summary": p.summary,
            "findings": [f.model_dump() for f in p.findings][:8], "missing_evidence": p.missing_evidence,
            "registration_status": p.registration_status, "rule_based": p.fallback_used}


async def _risk(run: Run, service: GlobalChain, listing_id: str) -> dict:
    listing, seller = _candidate(run, service, listing_id)
    r = await service.assess_seller_risk(listing, seller)
    run.risks[listing_id] = r
    return {"listing_id": listing_id, "assessment_id": r.assessment_id, "risk_score": r.risk_score,
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
    from .provenance import NotFound

    try:
        return _registration_summary(service.get_registration(registration_id))
    except NotFound:
        return {"registration_id": registration_id, "found": False,
                "note": "No such registration. A QR code or label citing it may be fake."}


def _guard(item: dict, run: Run) -> dict:
    """Muse Spark ranks, but it cannot recommend what the checks rule out."""
    p, r = run.products.get(item["listing_id"]), run.risks.get(item["listing_id"])
    if item["recommendation"] == "recommended" and p and r and (
            p.verdict == "likely_counterfeit" or r.risk_level == "high"):
        return {**item, "recommendation": "caution", "overridden": True,
                "reason": item["reason"] + " (Downgraded: the checks flagged a counterfeit or high-risk seller.)"}
    return item


async def _present(run: Run, service: GlobalChain, items: list[dict]) -> dict:
    unchecked = [i["listing_id"] for i in items if i["listing_id"] not in run.products]
    if unchecked:
        raise ValueError(f"Run verify_product and assess_seller_risk first: {', '.join(unchecked)}")
    run.recommendations = [_guard(i, run) for i in items if i.get("recommendation") in _RANK]
    return {"shown": len(run.recommendations),
            "overridden": [i["listing_id"] for i in run.recommendations if i.get("overridden")]}


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
    listing = Listing(title=p["title"], description=description or p.get("description") or "", price=price,
                      currency=currency, image_urls=_photos(run, photo_ids), marketplace="GlobalChain demo market",
                      brand=p.get("brand"), model=p.get("model"), registration_id=registration_id)
    slug = re.sub(r"[^a-z0-9]+", "-", p["maker_name"].lower()).strip("-")
    seller = Seller(seller_id=f"maker-{slug}", name=p["maker_name"], ships_from=p.get("maker_country"),
                    payment_methods=["card"])
    lid = Marketplace(service).add(listing, seller)
    run.listing = {"listing_id": lid, **listing.model_dump()}
    return {"listing_id": lid, "title": listing.title, "price": price, "currency": currency,
            "registration_id": registration_id}


TOOLS = {"search_listings": _search, "verify_product": _verify, "assess_seller_risk": _risk,
         "get_registration": _lookup, "present_recommendations": _present,
         "register_product": _register, "list_for_sale": _list}

# What the trace shows for each tool, from its result.
_BRIEF = {
    "search_listings": lambda r: f"{len(r['listings'])} listing(s) found, {r['over_budget']} over budget",
    "verify_product": lambda r: f"{r['verdict'].replace('_', ' ')} · {r['authenticity_score']}/100"
                                + (f" · origin {r['registration_status']}" if r["registration_status"] != "none" else ""),
    "assess_seller_risk": lambda r: f"{r['risk_level']} risk · {r['risk_score']}/100",
    "get_registration": lambda r: (f"made by {r['product']['maker_name']}, {len(r['fingerprint'])} fingerprint features"
                                   if "product" in r else "registration not found"),
    "present_recommendations": lambda r: f"{r['shown']} shown" + (f", {len(r['overridden'])} downgraded by guardrail"
                                                                  if r["overridden"] else ""),
    "register_product": lambda r: f"{r['registration_id']} · {len(r['fingerprint'])} features · "
                                  f"anchor {r['anchor']} · certificate {r['certificate']}",
    "list_for_sale": lambda r: f"{r['listing_id']} at {r['currency']} {r['price']:,.0f}",
}


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


def _tools(mode: str) -> list[dict]:
    return SHOP_TOOLS if mode == "shop" else MAKE_TOOLS


# --- policies -------------------------------------------------------------------------------------

class MusePolicy:
    def __init__(self, service: GlobalChain):
        self.engine = service.engine
        self.name = service.engine.models["agent"]

    async def next(self, run: Run, messages: list[dict]) -> dict:
        return await self.engine.chat("agent", messages, _tools(run.mode))


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

    def _shop(self, run: Run) -> dict:
        if not run.searched:
            budget = parse_budget_usd(run.message, self.service.settings.usd_zar_rate)
            args = {"query": run.message} | ({"max_price": round(budget, 2), "currency": "USD"} if budget else {})
            return self._calls([self._call("search_listings", **args)])
        calls = []
        for lid, (listing, _) in run.candidates.items():
            if lid not in run.products:
                calls.append(self._call("verify_product", listing_id=lid))
            if lid not in run.risks:
                calls.append(self._call("assess_seller_risk", listing_id=lid))
            if listing.registration_id and listing.registration_id not in run.lookups:
                calls.append(self._call("get_registration", registration_id=listing.registration_id))
        if calls:
            return self._calls(calls)
        if run.recommendations is None:
            items = []
            for lid in run.candidates:
                action, reason = recommend(run.products[lid], run.risks[lid])
                items.append({"listing_id": lid, "recommendation": action, "reason": reason})
            items.sort(key=lambda i: (_RANK[i["recommendation"]], -run.products[i["listing_id"]].authenticity_score))
            return self._calls([self._call("present_recommendations", items=items)])
        if not run.recommendations:
            return self._say("I couldn't find any listings that match within your budget.")
        top = run.recommendations[0]
        title = run.candidates[top["listing_id"]][0].title
        return self._say(f"Best option: {title} ({top['recommendation']}). {top['reason']} "
                         "Approve or skip it below; scores are estimates, not guarantees.")

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
        listed = f" Listed as {run.listing['listing_id']}." if run.listing else ""
        return self._say(f"Registered {reg['registration_id']} with {len(reg['fingerprint'])} fingerprint features. "
                         f"Hedera: {reg['anchor']['status']}; certificate: {reg['certificate']['status']}.{listed} "
                         f"Share the origin page: {reg['origin_url']}")


# --- loop -----------------------------------------------------------------------------------------

def _first_message(run: Run, service: GlobalChain) -> dict:
    if run.mode == "shop" or not run.photos:
        return {"role": "user", "content": run.message}
    content: list[dict[str, Any]] = [{"type": "text", "text": run.message}]
    for pid, url in run.photos.items():
        content.append({"type": "text", "text": f"Photo {pid}:"})
        content.append({"type": "image_url", "image_url": {"url": inline_static_urls([url])[0]}})
    return {"role": "user", "content": content}


def _results(run: Run, service: GlobalChain) -> list[dict]:
    """Cards for the web demo, in the agent's order; checked listings it left out go last."""
    order = [i["listing_id"] for i in run.recommendations or []]
    order += [lid for lid in run.products if lid not in order]
    by_id = {i["listing_id"]: i for i in run.recommendations or []}
    out = []
    for lid in order:
        if lid not in run.products or lid not in run.risks:
            continue
        listing, seller = run.candidates[lid]
        p, r = run.products[lid], run.risks[lid]
        action, reason = recommend(p, r)
        rec = by_id.get(lid, {"recommendation": action, "reason": reason})
        out.append({"listing_id": lid, "listing": listing.model_dump(), "seller": seller.model_dump(),
                    "price_usd": round(price_in_usd(listing, service.settings.usd_zar_rate), 2),
                    "product": p.model_dump(), "seller_risk": r.model_dump(),
                    "recommendation": rec["recommendation"], "reason": rec["reason"],
                    "overridden": rec.get("overridden", False)})
    return out


async def run_agent(mode: str, message: str, service: GlobalChain, photos: list[str] | None = None) -> dict:
    if mode not in ("shop", "make"):
        raise ValueError("mode must be 'shop' or 'make'")
    run = Run(mode=mode, message=message, photos={f"P{i + 1}": u for i, u in enumerate(photos or [])})
    scripted = ScriptedPolicy(service)
    policy = scripted if service.engine.name == "mock" else MusePolicy(service)
    prompt = SHOP_PROMPT.format(rate=service.settings.usd_zar_rate) if mode == "shop" else MAKE_PROMPT
    messages: list[dict] = [{"role": "developer", "content": prompt}, _first_message(run, service)]
    answer = None
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
            answer = msg.get("content") or ""
            break
        start = len(run.trace)
        results = await asyncio.gather(*(_execute(run, service, c, policy.name) for c in calls))
        run.batch_errors = [t["error"] for t in run.trace[start:] if t.get("error")]
        messages += [{"role": "tool", "tool_call_id": c["id"], "content": res} for c, res in zip(calls, results)]
    else:
        answer = "I stopped after the step limit. Here is what I checked so far."
    return {"mode": mode, "engine": service.engine.name, "model": service.engine.models["agent"],
            "policy": policy.name, "fallback_used": run.fallback_used, "tool_calls": run.calls,
            "trace": run.trace, "answer": answer, "results": _results(run, service),
            "registration": run.registration, "listing": run.listing}
