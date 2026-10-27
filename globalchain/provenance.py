"""issue_verification_record and check_provenance: hash assessments and anchor them on Hedera.

Only the record hash goes on-chain (via the Hedera Consensus Service), never listing
content or seller data. Anyone holding the record can recompute the hash and compare
it with the HCS message the mirror node returns.
"""

import asyncio
import base64
import hashlib
import json
import logging
from typing import Protocol

import httpx

from .config import Settings
from .schemas import Anchor, ProvenanceCheck, VerificationRecord
from .store import Store, new_id, now_iso

log = logging.getLogger(__name__)

RECORD_SCHEMA = "globalchain.record/v1"
MESSAGE_TYPE = "vcx.record.v1"


class NotFound(LookupError):
    pass


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(record: dict) -> str:
    return hashlib.sha256(canonical_json(record).encode()).hexdigest()


def product_key_for(product: dict) -> str:
    """Key that groups records about the same item into one product passport.

    A registered item is keyed by its origin registration, so every resale of it shares one
    passport. Otherwise listing URL or title is a stand-in that only links records of the
    same listing.
    """
    if product.get("registration_status") == "found":
        basis = "reg|" + product["registration_id"]
    else:
        listing = product.get("listing") or {}
        basis = "|".join(str(v or "").lower() for v in (
            product.get("brand"), product.get("model"), listing.get("url") or listing.get("title"),
        ))
    return "pk_" + hashlib.sha256(basis.encode()).hexdigest()[:20]


class Anchorer(Protocol):
    async def anchor(self, message: str) -> Anchor: ...


class NullAnchorer:
    async def anchor(self, message: str) -> Anchor:
        return Anchor(status="unanchored", detail="Hedera operator credentials are not configured.")


class HederaAnchorer:
    """Submits record hashes to an HCS topic with the hiero Python SDK."""

    def __init__(self, settings: Settings):
        from hiero_sdk_python import AccountId, Client, Network, PrivateKey

        self.settings = settings
        self.client = Client(Network(settings.hedera_network))
        self.operator_key = PrivateKey.from_string(settings.operator_key)
        self.client.set_operator(AccountId.from_string(settings.operator_id), self.operator_key)
        self.topic_id = settings.hedera_topic_id
        self._lock = asyncio.Lock()

    def _ensure_topic(self) -> str:
        from hiero_sdk_python import TopicCreateTransaction

        if self.topic_id:
            return self.topic_id
        receipt = (
            TopicCreateTransaction()
            .set_memo("VeriChainX verification records")
            # Only our operator may post, so the topic cannot be polluted with spoofed hashes.
            .set_submit_key(self.operator_key.public_key())
            .execute(self.client, validate_status=True)
        )
        self.topic_id = str(receipt.topic_id)
        log.warning("Created HCS topic %s; set HEDERA_TOPIC_ID to reuse it", self.topic_id)
        return self.topic_id

    def _submit(self, message: str) -> Anchor:
        from hiero_sdk_python import TopicId, TopicMessageSubmitTransaction

        topic = self._ensure_topic()
        receipt = (
            TopicMessageSubmitTransaction()
            .set_topic_id(TopicId.from_string(topic))
            .set_message(message)
            .execute(self.client, validate_status=True)
        )
        return Anchor(
            status="anchored",
            network=self.settings.hedera_network,
            topic_id=topic,
            sequence_number=receipt.topic_sequence_number,
            transaction_id=str(receipt.transaction_id) if receipt.transaction_id else None,
            explorer_url=f"{self.settings.hashscan_base}/topic/{topic}",
        )

    async def anchor(self, message: str) -> Anchor:
        async with self._lock:  # topic creation and nonce handling are not concurrency-safe
            try:
                return await asyncio.to_thread(self._submit, message)
            except Exception as exc:
                log.exception("HCS submit failed")
                return Anchor(status="failed", network=self.settings.hedera_network, detail=str(exc))


def build_anchorer(settings: Settings) -> Anchorer:
    if settings.operator_id and settings.operator_key:
        return HederaAnchorer(settings)
    return NullAnchorer()


def _passport_url(settings: Settings, product_key: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/passport/{product_key}"


async def issue_verification_record(
    product_assessment_id: str,
    seller_assessment_id: str | None,
    decision: str,
    store: Store,
    anchorer: Anchorer,
    settings: Settings,
) -> VerificationRecord:
    product = store.get_assessment(product_assessment_id, "product")
    if not product:
        raise NotFound(f"Unknown product assessment {product_assessment_id!r}")
    seller = None
    if seller_assessment_id:
        seller = store.get_assessment(seller_assessment_id, "seller")
        if not seller:
            raise NotFound(f"Unknown seller assessment {seller_assessment_id!r}")

    # Idempotent: the same assessments and decision always map to one record.
    source_key = f"{product_assessment_id}|{seller_assessment_id or ''}|{decision}"
    existing = store.get_record_by_source(source_key)
    if existing:
        return VerificationRecord(**{k: existing[k] for k in ("record_id", "product_key", "record_hash", "record")},
                                  anchor=Anchor(**existing["anchor"]),
                                  passport_url=_passport_url(settings, existing["product_key"]))

    product_key = product_key_for(product)
    record_id = new_id("vr")
    record = {
        "schema": RECORD_SCHEMA,
        "record_id": record_id,
        "product_key": product_key,
        "issued_at": now_iso(),
        "decision": decision,
        "listing": {k: product["listing"].get(k) for k in ("title", "price", "currency", "url", "marketplace")},
        "product": {
            "assessment_id": product["assessment_id"],
            "brand": product["brand"],
            "model": product["model"],
            "reference_id": product["reference_id"],
            "verdict": product["verdict"],
            "authenticity_score": product["authenticity_score"],
            "confidence": product["confidence"],
            "evidence": [{"id": e["id"], "feature": e["feature"], "crop_sha256": e["crop_sha256"]}
                         for e in product["evidence"]],
            "findings_sha256": hashlib.sha256(canonical_json(product["findings"]).encode()).hexdigest(),
            "registration_id": product.get("registration_id"),
            "registration_status": product.get("registration_status", "none"),
            "engine": product["engine"],
            "models": product["models"],
            "fallback_used": product.get("fallback_used", False),
        },
        "seller": None if not seller else {
            "assessment_id": seller["assessment_id"],
            "seller_id": seller["seller_id"],
            "risk_score": seller["risk_score"],
            "risk_level": seller["risk_level"],
            "signals": {s["name"]: s["weight"] for s in seller["signals"]},
            "engine": seller["engine"],
            "fallback_used": seller.get("fallback_used", False),
        },
    }
    digest = record_hash(record)
    message = canonical_json({"t": MESSAGE_TYPE, "id": record_id, "pk": product_key, "h": digest})
    anchor = await anchorer.anchor(message)

    store.save_record({"record_id": record_id, "product_key": product_key, "record_hash": digest,
                       "record": record, "anchor": anchor.model_dump(), "source_key": source_key})
    return VerificationRecord(record_id=record_id, product_key=product_key, record_hash=digest, record=record,
                              anchor=anchor, passport_url=_passport_url(settings, product_key))


async def _fetch_hcs_message(anchor: Anchor, settings: Settings) -> dict:
    url = f"{settings.mirror_base}/topics/{anchor.topic_id}/messages/{anchor.sequence_number}"
    async with httpx.AsyncClient(timeout=settings.http_timeout) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        return resp.json()


async def check_provenance(record_id: str, store: Store, settings: Settings) -> ProvenanceCheck:
    row = store.get_record(record_id)
    if not row:
        raise NotFound(f"Unknown record {record_id!r}")
    anchor = Anchor(**row["anchor"])
    recomputed = record_hash(row["record"])
    history = [
        {"record_id": r["record_id"], "issued_at": r["record"]["issued_at"], "decision": r["record"]["decision"],
         "verdict": r["record"]["product"]["verdict"], "anchor_status": r["anchor"]["status"]}
        for r in store.records_for_product(row["product_key"])
    ]

    on_chain_hash, status = None, "unanchored"
    if anchor.status == "anchored":
        try:
            msg = await _fetch_hcs_message(anchor, settings)
            on_chain = json.loads(base64.b64decode(msg["message"]))
            on_chain_hash = on_chain.get("h")
            if msg.get("consensus_timestamp") and not anchor.consensus_timestamp:
                anchor.consensus_timestamp = msg["consensus_timestamp"]
                store.update_anchor(record_id, anchor.model_dump())
            status = "verified" if on_chain_hash == recomputed == row["record_hash"] else "hash_mismatch"
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("mirror node lookup failed for %s: %s", record_id, exc)
            status = "chain_unavailable"
    elif recomputed != row["record_hash"]:
        status = "hash_mismatch"

    return ProvenanceCheck(record_id=record_id, product_key=row["product_key"], status=status,
                           record_hash=row["record_hash"], recomputed_hash=recomputed,
                           on_chain_hash=on_chain_hash, anchor=anchor, history=history)
