"""Vendor agents and the message bus the shopper's agent talks to them over.

Each vendor has its own Muse Spark agent with its own persona and catalogue. The shopper's agent
broadcasts a request; every vendor agent decides what to pitch and writes its own pitch, and can be
asked follow-up questions. Messages are envelopes signed with the sender's Hedera account key, so
the shopper's agent can check a pitch against the key Hedera holds for that vendor's account.

"Verified" means: GlobalChain's directory binds the vendor to a Hedera account, the pitch signature
matches that account's on-chain key, and each item's NFT is held by that account. An unverified
vendor can still pitch, but its envelopes are unsigned, and an item citing another vendor's
registration is caught because the NFT is in someone else's wallet.
"""

import asyncio
import json
import logging
import re
from pathlib import Path

from .demo import inline_static_urls
from .ledger import Wallet, nft_ref, verify_signature
from .provenance import NotFound, canonical_json
from .references import price_in_usd
from .schemas import Listing, MakerProduct, Seller
from .store import new_id, now_iso

log = logging.getLogger(__name__)

_DATA = Path(__file__).parent / "data" / "vendors.json"
BUYER_WALLET = "buyer"


# --- directory ------------------------------------------------------------------------------------

def vendors(store) -> list[dict]:
    return store.all("vendor")


def vendor(store, vendor_id: str) -> dict:
    v = store.get("vendor", vendor_id)
    if not v:
        raise NotFound(f"Unknown vendor {vendor_id!r}")
    return v


def items(store, vendor_id: str | None = None) -> list[dict]:
    return [i for i in store.all("item") if vendor_id is None or i["vendor_id"] == vendor_id]


def item(store, item_id: str) -> dict:
    i = store.get("item", item_id)
    if not i:
        raise NotFound(f"Unknown offer {item_id!r}")
    return i


def buyer_wallet(store) -> dict | None:
    return store.get("wallet", BUYER_WALLET)


def item_listing(i: dict, inline: bool = False) -> Listing:
    photos = inline_static_urls(i["photos"]) if inline else i["photos"]
    return Listing(title=i["title"], description=i["description"], price=i["price"], currency=i["currency"],
                   image_urls=photos, marketplace="GlobalChain agent network", brand=i.get("brand"),
                   model=i.get("model"), registration_id=i.get("registration_id"))


def item_seller(v: dict) -> Seller:
    return Seller(**v["seller"])


async def add_vendor(service, vendor_id: str, name: str, country: str | None, persona: str, seller: dict,
                     verified: bool) -> dict:
    wallet = None
    if verified:
        wallet = (await service.ledger.create_wallet(f"vendor {name}")).model_dump()
    doc = {"vendor_id": vendor_id, "name": name, "country": country, "verified": verified, "persona": persona,
           "seller": seller, "wallet": wallet, "joined_at": now_iso()}
    service.store.put("vendor", vendor_id, doc)
    return doc


async def add_item(service, v: dict, spec: dict, registration=None) -> dict:
    """Put an item in a vendor's catalogue. A verified vendor's item gets its NFT in their wallet."""
    item_id = new_id("itm")
    nft, issue_tx = None, None
    if registration is None and v["verified"]:
        photos = inline_static_urls(spec["photos"])
        registration = await service.register_product(MakerProduct(
            maker_name=v["name"], maker_country=v.get("country"), title=spec["title"],
            category=spec.get("category", "handbag"), brand=spec.get("brand"), model=spec.get("model"),
            description=spec.get("description", ""), image_urls=photos))
    if registration is not None and v["verified"]:
        token, serial, tx = await service.ledger.mint_item(
            registration.registration_hash.encode()[:32], v["wallet"]["account_id"], registration.certificate.model_dump())
        issue_tx = tx.model_dump()
        if tx.status == "success":
            nft = {"token_id": token, "serial": serial}
    doc = {"item_id": item_id, "vendor_id": v["vendor_id"], "key": spec.get("key"), "title": spec["title"],
           "brand": spec.get("brand"), "model": spec.get("model"), "category": spec.get("category", "handbag"),
           "description": spec.get("description", ""), "price": spec["price"], "currency": spec["currency"],
           "photos": spec["photos"], "registration_id": registration.registration_id if registration else None,
           "nft": nft, "nft_issue_tx": issue_tx, "status": "available", "listed_at": now_iso()}
    service.store.put("item", item_id, doc)
    return doc


_seed_lock = asyncio.Lock()


async def ensure_seeded(service) -> None:
    """Create the demo vendors, their wallets, registrations and NFTs, and the shopper's wallet, once."""
    async with _seed_lock:
        if service.store.get("wallet", BUYER_WALLET):
            return
        data = json.loads(_DATA.read_text())
        by_key: dict[str, dict] = {}
        for spec in data["vendors"]:
            v = await add_vendor(service, spec["vendor_id"], spec["name"], spec.get("country"), spec["persona"],
                                 spec["seller"], spec["verified"])
            for item_spec in spec["items"]:
                copied = item_spec.get("copies_registration_of")
                if copied:  # a counterfeiter reusing a genuine item's QR code
                    original = by_key[copied]
                    doc = await add_item(service, v, item_spec)
                    doc["registration_id"] = original["registration_id"]
                    service.store.put("item", doc["item_id"], doc)
                else:
                    doc = await add_item(service, v, item_spec)
                by_key[f"{spec['vendor_id']}:{item_spec['key']}"] = doc
        buyer = await service.ledger.create_wallet(data["buyer"]["label"], data["buyer"]["fund_usd"])
        service.store.put("wallet", BUYER_WALLET, buyer.model_dump())
        log.info("Seeded %d vendors and the shopper wallet %s", len(data["vendors"]), buyer.account_id)


# --- signed messages ------------------------------------------------------------------------------

def _signed_bytes(env: dict) -> bytes:
    return canonical_json({k: v for k, v in env.items() if k not in ("signature", "signer")}).encode()


def envelope(service, conversation_id: str, sender: dict | str, to: str, kind: str, body: dict) -> dict:
    """Build, sign (for vendors with a wallet) and store one agent-to-agent message."""
    sender_id = sender if isinstance(sender, str) else sender["vendor_id"]
    env = {"id": new_id("msg"), "conversation_id": conversation_id, "from": sender_id,
           "from_name": "Your agent" if isinstance(sender, str) else sender["name"], "to": to, "type": kind,
           "body": body, "sent_at": now_iso()}
    wallet = None if isinstance(sender, str) else sender.get("wallet")
    if wallet:
        env["signer"] = wallet["account_id"]
        env["signature"] = service.ledger.sign(wallet["account_id"], _signed_bytes(env))
    service.store.put("message", env["id"], env)
    return env


async def check_envelope(service, env: dict) -> dict:
    """Who sent this, according to Hedera rather than to the message itself."""
    v = service.store.get("vendor", env["from"])
    if not v or not v.get("verified") or not v.get("wallet"):
        return {"status": "unverified", "detail": "Not in GlobalChain's verified vendor directory"}
    if not env.get("signature") or env.get("signer") != v["wallet"]["account_id"]:
        return {"status": "unverified", "detail": "Message is not signed by the vendor's registered account"}
    key = await service.ledger.public_key(env["signer"])
    if not key:
        return {"status": "unverified", "detail": f"Could not read the key for {env['signer']} from Hedera"}
    if not verify_signature(key, env["signature"], _signed_bytes(env)):
        return {"status": "invalid_signature", "detail": "Signature does not match the account's key on Hedera"}
    return {"status": "verified", "account": env["signer"],
            "detail": f"Signed by {env['signer']}, matching the key Hedera holds for it"}


async def check_ownership(service, i: dict) -> dict:
    """Does the pitching vendor actually hold the NFT for the registration this item cites?"""
    if not i.get("registration_id"):
        return {"status": "no_registration", "detail": "No origin registration, so no NFT"}
    holder = next((x for x in items(service.store) if x["registration_id"] == i["registration_id"] and x.get("nft")),
                  None)
    if holder is None:
        return {"status": "no_nft", "detail": "Registration has no NFT in circulation"}
    owner = await service.ledger.nft_owner(holder["nft"]["token_id"], holder["nft"]["serial"])
    v = service.store.get("vendor", i["vendor_id"]) or {}
    mine = (v.get("wallet") or {}).get("account_id")
    ref = nft_ref(holder["nft"]["token_id"], holder["nft"]["serial"])
    if mine and owner == mine:
        return {"status": "owned", "nft": ref, "owner": owner, "detail": f"Vendor holds NFT {ref}"}
    if holder["item_id"] != i["item_id"]:
        return {"status": "cloned", "nft": ref, "owner": owner,
                "detail": f"This registration's NFT {ref} belongs to another vendor ({owner}). The QR code was copied."}
    return {"status": "not_held", "nft": ref, "owner": owner, "detail": f"NFT {ref} is held by {owner}, not the vendor"}


# --- vendor agents --------------------------------------------------------------------------------

PITCH_PROMPT = """You are the sales agent for {name}. {persona}
A shopper's agent has sent a request. Decide which of your catalogue items, if any, fit the need and
budget, and write a short pitch for each (under 50 words, honest about condition, no invented facts).
Offer nothing if nothing fits. Prices are fixed: quote them as listed. Reply as JSON."""

PITCH_SCHEMA = {"name": "vendor_pitch", "schema": {
    "type": "object", "additionalProperties": False, "required": ["offers", "message"],
    "properties": {
        "message": {"type": "string", "description": "One line to the shopper's agent"},
        "offers": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                              "required": ["item_id", "pitch"],
                                              "properties": {"item_id": {"type": "string"}, "pitch": {"type": "string"}}}},
    }}}

ANSWER_PROMPT = """You are the sales agent for {name}. {persona}
Answer the shopper's agent's question about your item in under 60 words, using only the catalogue
facts given. If you don't know, say so. Reply as JSON."""

ANSWER_SCHEMA = {"name": "vendor_answer", "schema": {
    "type": "object", "additionalProperties": False, "required": ["answer"],
    "properties": {"answer": {"type": "string"}}}}

_STOP = {"a", "an", "the", "me", "find", "for", "my", "i", "need", "want", "under", "below", "with", "and", "of",
         "to", "get", "every", "day", "everyday", "something", "good", "nice"}


def _catalogue(store, v: dict) -> list[dict]:
    return [{"item_id": i["item_id"], "title": i["title"], "category": i["category"], "price": i["price"],
             "currency": i["currency"], "description": i["description"]}
            for i in items(store, v["vendor_id"]) if i["status"] == "available"]


def _fits(service, i: dict, need: str, budget_usd: float | None) -> bool:
    usd = price_in_usd(item_listing(i), service.settings.usd_zar_rate)
    if budget_usd is not None and usd > budget_usd:
        return False
    words = {w for w in re.findall(r"[a-z0-9]+", need.lower()) if w not in _STOP and not w.isdigit()}
    text = f"{i['title']} {i['category']} {i['description']}".lower()
    bagish = {"bag", "handbag", "tote", "purse", "shoulder", "crossbody"}
    return not words or any(w in text for w in words) or bool(words & bagish and i["category"] in ("handbag", "tote"))


async def pitch(service, v: dict, conversation_id: str, request: dict) -> dict:
    """One vendor agent's answer to a broadcast request, as a signed envelope."""
    budget = request.get("budget_usd")
    catalogue = _catalogue(service.store, v)

    def fallback() -> dict:
        fits = [c for c in catalogue if _fits(service, service.store.get("item", c["item_id"]), request["need"], budget)]
        return {"message": f"{v['name']} has {len(fits)} item(s) for you." if fits else "Nothing suitable right now.",
                "offers": [{"item_id": c["item_id"], "pitch": c["description"][:200]} for c in fits]}

    content = [{"type": "text", "text": json.dumps({"request": request, "catalogue": catalogue}, indent=1)}]
    raw, fallback_used = await service.engine.reason_json(
        "vendor", PITCH_PROMPT.format(name=v["name"], persona=v["persona"]), content, fallback, schema=PITCH_SCHEMA)
    valid = {c["item_id"] for c in catalogue}
    offers = [o for o in raw.get("offers", []) if isinstance(o, dict) and o.get("item_id") in valid]
    # A vendor agent cannot pitch past the shopper's budget, whatever its model says.
    offers = [o for o in offers if _fits(service, service.store.get("item", o["item_id"]), "", budget)]
    body = {"message": str(raw.get("message", ""))[:300], "offers": offers, "fallback_used": fallback_used,
            "model": service.engine.models["vendor"]}
    return envelope(service, conversation_id, v, "shopper", "pitch", body)


async def answer(service, v: dict, conversation_id: str, question: str, item_id: str | None) -> dict:
    catalogue = _catalogue(service.store, v)
    facts = [c for c in catalogue if item_id in (None, c["item_id"])] or catalogue

    def fallback() -> dict:
        f = facts[0] if facts else None
        return {"answer": f"{f['title']}: {f['description']}" if f else "We have nothing listed right now."}

    content = [{"type": "text", "text": json.dumps({"question": question, "catalogue": facts}, indent=1)}]
    raw, fallback_used = await service.engine.reason_json(
        "vendor", ANSWER_PROMPT.format(name=v["name"], persona=v["persona"]), content, fallback, schema=ANSWER_SCHEMA)
    body = {"answer": str(raw.get("answer", ""))[:500], "item_id": item_id, "fallback_used": fallback_used}
    return envelope(service, conversation_id, v, "shopper", "answer", body)


def conversation(store, conversation_id: str) -> list[dict]:
    return [m for m in store.all("message") if m["conversation_id"] == conversation_id]


def wallet_view(w: dict | Wallet | None) -> dict | None:
    if w is None:
        return None
    w = w.model_dump() if isinstance(w, Wallet) else w
    return {k: w[k] for k in ("account_id", "label", "network", "simulated", "explorer_url")}
