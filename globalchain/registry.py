"""register_product: a maker registers an item at origin, before it is ever sold.

Muse Spark looks at the maker's photos and chooses the marks that identify this one item
(a hallmark, a signature, a weave or bead pattern, stitching, a serial tag). SAM 3.1 crops each
one, and the crops become the item's origin fingerprint. The registration hash is anchored on
Hedera HCS and, when configured, minted as an HTS NFT certificate. A QR code links to the public
origin page, and any listing that carries the registration id is compared against the fingerprint.

Follows AfriChain's product registration flow (backend/auth-service/src/services/productService.ts
and qrCodeService.ts), re-implemented here around SAM 3.1 and Muse Spark.
"""

import io
import logging

import qrcode

from .certificates import Minter, certificate_metadata
from .config import Settings
from .engine import Engine
from .provenance import Anchorer, NotFound, canonical_json, record_hash
from .references import match_reference
from .sam import load_images
from .schemas import Listing, MakerProduct, Registration
from .store import Store, new_id, now_iso
from .verify import segment_evidence

log = logging.getLogger(__name__)

REGISTRATION_SCHEMA = "globalchain.registration/v1"
MESSAGE_TYPE = "gcm.reg.v1"
MAX_FEATURES = 6

DEVELOPER_PROMPT = f"""You help a maker register an item so it can be recognised later, when it is resold.
Look at the photos and choose up to {MAX_FEATURES} visible features that identify this particular item and
would be hard to copy exactly: hallmarks or stamps, serial or date codes, signatures, logo hardware,
stitching, weave or bead patterns, tags, distinctive wear.

Rules:
- Only choose features you can see in the photos.
- sam_prompt is a short noun phrase naming one kind of object for a segmentation model, for example
  "leather tag" or "metal clasp". Never use commas, "and" or "or" in it.
- why is one sentence on what makes this feature identifying.

Reply with JSON only: {{"features": [{{"name": "...", "sam_prompt": "...", "why": "..."}}]}}"""

OUTPUT_SCHEMA = {
    "name": "origin_fingerprint_features",
    "schema": {
        "type": "object",
        "properties": {
            "features": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "sam_prompt": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["name", "sam_prompt", "why"],
                },
            },
        },
        "required": ["features"],
    },
}


def _valid_prompt(prompt: str) -> bool:
    # SAM 3.1 segments one concept per prompt; lists split it.
    words = prompt.split()
    return 0 < len(words) <= 5 and "," not in prompt and "and" not in words and "or" not in words


def _validate_features(raw: dict) -> list[dict]:
    features, seen = [], set()
    for f in raw.get("features") or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", "")).strip()
        prompt = str(f.get("sam_prompt", "")).strip().lower()
        if not name or name.lower() in seen or not _valid_prompt(prompt):
            continue
        seen.add(name.lower())
        features.append({"name": name, "sam_prompt": prompt, "why": str(f.get("why", "")).strip()})
    return features[:MAX_FEATURES]


def _reference_features(product: MakerProduct) -> list[dict]:
    ref = match_reference(Listing(title=product.title, brand=product.brand, model=product.model, price=0))
    return [{**f, "why": "Standard check for this kind of product."} for f in ref["features"]]


async def _choose_features(product: MakerProduct, images: list[str], engine: Engine) -> tuple[list[dict], bool]:
    fallback = {"features": _reference_features(product)}
    content: list[dict] = [{"type": "text", "text": canonical_json(product.model_dump(exclude={"image_urls"}))}]
    for i, url in enumerate(images):
        content.append({"type": "text", "text": f"Maker photo {i}:"})
        content.append({"type": "image_url", "image_url": {"url": url}})
    raw, fallback_used = await engine.reason_json(
        "register", DEVELOPER_PROMPT, content, fallback=lambda: fallback, schema=OUTPUT_SCHEMA
    )
    features = _validate_features(raw)
    if not features:
        return fallback["features"], True
    return features, fallback_used


def _urls(settings: Settings, registration_id: str) -> tuple[str, str]:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/origin/{registration_id}", f"{base}/v1/registrations/{registration_id}/qr.png"


async def register_product(product: MakerProduct, engine: Engine, store: Store, anchorer: Anchorer,
                           minter: Minter, settings: Settings) -> Registration:
    images = await load_images(product.image_urls[: settings.max_images], settings.http_timeout)
    if not images:
        raise ValueError("None of the product photos could be loaded.")

    features, fallback_used = await _choose_features(product, images, engine)
    regions = await segment_evidence(engine, images, features, id_prefix="R")
    rationale = {f["name"]: f["why"] for f in features}

    registration_id = new_id("reg")
    issued_at = now_iso()
    details = product.model_dump(exclude={"image_urls"})
    models = {"features": engine.models["register"], "segmentation": engine.models["segmentation"]}
    record = {
        "schema": REGISTRATION_SCHEMA,
        "registration_id": registration_id,
        "issued_at": issued_at,
        "product": details,
        "fingerprint": [{"id": r.id, "feature": r.feature, "crop_sha256": r.crop_sha256} for r, _ in regions],
        "engine": engine.name,
        "models": models,
    }
    digest = record_hash(record)
    anchor = await anchorer.anchor(canonical_json({"t": MESSAGE_TYPE, "id": registration_id, "h": digest}))
    certificate = await minter.mint(certificate_metadata(registration_id, digest))

    origin_url, qr_url = _urls(settings, registration_id)
    registration = Registration(
        registration_id=registration_id, product=details, fingerprint=[r for r, _ in regions],
        rationale=rationale, registration_hash=digest, anchor=anchor, certificate=certificate,
        origin_url=origin_url, qr_url=qr_url, issued_at=issued_at, engine=engine.name, models=models,
        fallback_used=fallback_used,
    )
    store.save_registration(registration_id, {**registration.model_dump(), "record": record},
                            {r.id: crop for r, crop in regions})
    return registration


def get_registration(registration_id: str, store: Store) -> Registration:
    found = store.get_registration(registration_id)
    if not found:
        raise NotFound(f"Unknown registration {registration_id!r}")
    payload, _ = found
    return Registration(**{k: v for k, v in payload.items() if k != "record"})


def registration_crop(registration_id: str, region_id: str, store: Store) -> str:
    """The fingerprint crop for one region, as a data URL."""
    found = store.get_registration(registration_id)
    if not found or region_id not in found[1]:
        raise NotFound(f"Unknown region {region_id!r} of registration {registration_id!r}")
    return found[1][region_id]


def qr_png(url: str) -> bytes:
    buf = io.BytesIO()
    qrcode.make(url, border=2).save(buf)
    return buf.getvalue()
