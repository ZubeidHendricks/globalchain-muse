"""verify_product: SAM 3.1 segments the evidence, Muse Spark reasons over it and cites it.

When the listing claims an origin registration (from the item's QR code), SAM 3.1 segments the
features the maker registered, and Muse Spark compares each listing region (E1, E2, ...) with
the item's own origin crop (R1, R2, ...) instead of relying on generic brand data alone.
"""

import asyncio
import base64
import io
import json
import logging

from PIL import Image

from .config import Settings
from .engine import Engine
from .lexicon import COUNTERFEIT_TERMS, find_terms
from .references import match_reference, price_in_usd
from .sam import SamDetection, crop_region, load_images, parse_sam_output
from .schemas import Box, EvidenceRegion, Finding, Listing, ProductVerification
from .store import Store, new_id

log = logging.getLogger(__name__)

# Evidence ids for claims grounded in the listing itself rather than a photo region.
LISTING_EVIDENCE = {"listing_text", "price"}

DEVELOPER_PROMPT = """You are VeriChainX, a product authentication analyst working for a shopping agent.
You compare a marketplace listing against brand reference data and decide how likely the item is genuine.

Rules:
- Ground every finding in evidence. Cite photo regions by their id (E1, E2, ...), or use
  "listing_text" / "price" for claims about the listing copy or price.
- Judge only what is visible or stated. Photos can be stolen from genuine listings, so strong
  photos alone never justify a high score when the price or text contradicts them.
- If key reference features are not visible, list them in missing_evidence and lower confidence.
- Prefer "inconclusive" over a confident wrong answer.

Reply with JSON only:
{"verdict": "likely_authentic" | "inconclusive" | "likely_counterfeit",
 "authenticity_score": 0-100,
 "confidence": "low" | "medium" | "high",
 "summary": "two or three sentences a shopper can read",
 "findings": [{"evidence_id": "E1", "observation": "...", "supports": "authentic" | "counterfeit" | "neutral"}],
 "missing_evidence": ["feature name", ...]}"""

ORIGIN_RULES = """

This listing claims an origin registration made by the maker. R1, R2, ... are crops of the item's
distinctive features captured at registration; E1, E2, ... are the same features segmented from
the listing photos.
- Compare each E region with the R region for the same feature and name both in the observation.
  Cite either id as evidence_id.
- Matching specifics (engraving, placement, pattern, stitch count, wear) support authentic. Clear
  differences support counterfeit, even when the item looks well made.
- The origin page is public, so its photos can be copied. A match does not outweigh a contradicting
  price, listing text or seller story."""


# Structured-output schema mirroring the reply format above; _validate still re-checks every field.
OUTPUT_SCHEMA = {
    "name": "product_verification",
    "schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["likely_authentic", "inconclusive", "likely_counterfeit"]},
            "authenticity_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "observation": {"type": "string"},
                        "supports": {"type": "string", "enum": ["authentic", "counterfeit", "neutral"]},
                    },
                    "required": ["evidence_id", "observation", "supports"],
                },
            },
            "missing_evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["verdict", "authenticity_score", "confidence", "summary", "findings", "missing_evidence"],
    },
}

def _best_detection(detections: list[SamDetection]) -> SamDetection | None:
    return max(detections, key=lambda d: d.area, default=None)


def _image_size(data_url: str) -> tuple[int, int]:
    img = Image.open(io.BytesIO(base64.b64decode(data_url.partition(",")[2])))
    return img.width, img.height


async def segment_evidence(
    engine: Engine, images: list[str], features: list[dict], id_prefix: str = "E"
) -> list[tuple[EvidenceRegion, str]]:
    """Segment each feature with SAM 3.1 and crop the best region. Returns (region, crop data URL) pairs."""
    sizes = [_image_size(u) for u in images]
    jobs = [(i, f) for i in range(len(images)) for f in features]

    async def run(i: int, feature: dict):
        try:
            text = await engine.segment(images[i], feature["sam_prompt"], sizes[i])
        except Exception as exc:  # one failed region should not sink the assessment
            log.warning("SAM failed for image %d / %r: %s", i, feature["sam_prompt"], exc)
            return None
        return _best_detection(parse_sam_output(text))

    results = await asyncio.gather(*(run(i, f) for i, f in jobs))

    # Keep the largest detection per feature across all images.
    best: dict[str, tuple[int, dict, SamDetection]] = {}
    for (i, feature), det in zip(jobs, results):
        if det and (feature["name"] not in best or det.area > best[feature["name"]][2].area):
            best[feature["name"]] = (i, feature, det)

    regions = []
    for n, (i, feature, det) in enumerate(best.values(), start=1):
        crop_url, crop_hash = crop_region(images[i], det)
        regions.append((
            EvidenceRegion(
                id=f"{id_prefix}{n}",
                image_index=i,
                feature=feature["name"],
                prompt=feature["sam_prompt"],
                box=Box(x1=det.x1, y1=det.y1, x2=det.x2, y2=det.y2, frame_w=det.frame_w, frame_h=det.frame_h),
                crop_sha256=crop_hash,
            ),
            crop_url,
        ))
    return regions


def _rule_based(listing: Listing, ref: dict, features: list[dict], regions: list[EvidenceRegion],
                registration: dict | None, settings: Settings) -> dict:
    """Deterministic verdict from price, listing language and registration status. Used offline and as a fallback."""
    findings, score = [], 60
    text_hits = find_terms(f"{listing.title} {listing.description}", COUNTERFEIT_TERMS)
    if text_hits:
        score -= 45
        findings.append({"evidence_id": "listing_text", "supports": "counterfeit",
                         "observation": f"Listing uses counterfeit trade terms: {', '.join(text_hits)}."})
    if listing.registration_id and not registration:
        score -= 25
        findings.append({"evidence_id": "listing_text", "supports": "counterfeit",
                         "observation": f"Listing cites origin registration {listing.registration_id}, "
                                        "which does not exist. The QR code or label may be fake."})
    elif registration:
        product = registration["product"]
        findings.append({"evidence_id": "listing_text", "supports": "neutral",
                         "observation": f"Listing claims origin registration {registration['registration_id']} "
                                        f"({product['title']}, made by {product['maker_name']}, registered "
                                        f"{registration['issued_at'][:10]})."})
    usd = price_in_usd(listing, settings.usd_zar_rate)
    if ref.get("resale_usd"):
        floor = ref["resale_usd"][0]
        if usd < floor * 0.5:
            score -= 30
            findings.append({"evidence_id": "price", "supports": "counterfeit",
                             "observation": f"Price ${usd:.0f} is under half the typical resale floor of ${floor}."})
        elif usd >= floor:
            score += 10
            findings.append({"evidence_id": "price", "supports": "authentic",
                             "observation": f"Price ${usd:.0f} is within the normal resale range."})
    against = "the origin fingerprint" if registration else "brand references"
    for r in regions:
        findings.append({"evidence_id": r.id, "supports": "neutral",
                         "observation": f"Segmented {r.feature}; visual comparison with {against} needs the live model."})
    found = {r.feature for r in regions}
    missing = [f["name"] for f in features if f["name"] not in found]
    score = max(0, min(100, score))
    verdict = "likely_counterfeit" if score < 35 else "likely_authentic" if score >= 75 else "inconclusive"
    return {
        "verdict": verdict,
        "authenticity_score": score,
        "confidence": "low",
        "summary": "Rule-based check of price and listing language only; photo regions were segmented "
                   "but not visually compared.",
        "findings": findings,
        "missing_evidence": missing,
    }


def _build_content(listing: Listing, ref: dict, images: list[str], regions: list[tuple[EvidenceRegion, str]],
                   origin: list[tuple[EvidenceRegion, str]], registration: dict | None,
                   settings: Settings) -> list[dict]:
    reference = {k: ref[k] for k in ("brand", "model", "retail_usd", "resale_usd", "features")}
    header = {
        "listing": listing.model_dump(exclude={"image_urls"}),
        "price_usd": round(price_in_usd(listing, settings.usd_zar_rate), 2),
        "reference": reference,
        "evidence_regions": [{"id": r.id, "feature": r.feature, "image": r.image_index} for r, _ in regions],
    }
    if registration:
        header["origin_registration"] = {
            "registration_id": registration["registration_id"],
            "registered_at": registration["issued_at"],
            "product": registration["product"],
            "fingerprint": [{"id": r.id, "feature": r.feature, "why": registration["rationale"].get(r.feature, "")}
                            for r, _ in origin],
        }
    content: list[dict] = [{"type": "text", "text": json.dumps(header, indent=1)}]
    for i, url in enumerate(images):
        content.append({"type": "text", "text": f"Full listing photo {i}:"})
        content.append({"type": "image_url", "image_url": {"url": url}})
    for region, crop_url in regions:
        content.append({"type": "text", "text": f"{region.id}: {region.feature} (from photo {region.image_index})"})
        content.append({"type": "image_url", "image_url": {"url": crop_url}})
    for region, crop_url in origin:
        content.append({"type": "text", "text": f"{region.id}: {region.feature} (captured at origin registration)"})
        content.append({"type": "image_url", "image_url": {"url": crop_url}})
    return content


def _validate(raw: dict, valid_ids: set[str], fallback: dict) -> dict:
    """Keep only well-formed output and findings that cite evidence that exists."""
    out = dict(fallback)
    if raw.get("verdict") in ("likely_authentic", "inconclusive", "likely_counterfeit"):
        out["verdict"] = raw["verdict"]
    if isinstance(raw.get("authenticity_score"), (int, float)):
        out["authenticity_score"] = int(max(0, min(100, raw["authenticity_score"])))
    if raw.get("confidence") in ("low", "medium", "high"):
        out["confidence"] = raw["confidence"]
    if isinstance(raw.get("summary"), str) and raw["summary"].strip():
        out["summary"] = raw["summary"].strip()
    findings = []
    for f in raw.get("findings") or []:
        try:
            finding = Finding.model_validate(f)
        except Exception:
            continue
        if finding.evidence_id in valid_ids:
            findings.append(finding.model_dump())
    if findings:
        out["findings"] = findings
    if isinstance(raw.get("missing_evidence"), list):
        out["missing_evidence"] = [str(m) for m in raw["missing_evidence"]]
    return out


def _origin_regions(registration: dict, crops: dict[str, str]) -> list[tuple[EvidenceRegion, str]]:
    return [(EvidenceRegion(**r), crops[r["id"]]) for r in registration["fingerprint"] if r["id"] in crops]


async def verify_product(listing: Listing, engine: Engine, store: Store, settings: Settings) -> ProductVerification:
    ref = match_reference(listing)
    registration, origin = None, []
    if listing.registration_id:
        found = store.get_registration(listing.registration_id)
        if found:
            registration, crops = found
            origin = _origin_regions(registration, crops)
    # A registered item is checked for the features its maker registered; otherwise, the brand's.
    features = ([{"name": r.feature, "sam_prompt": r.prompt} for r, _ in origin] if origin
                else ref["features"])

    images = await load_images(listing.image_urls[: settings.max_images], settings.http_timeout)
    regions = await segment_evidence(engine, images, features) if images else []
    region_models = [r for r, _ in regions]
    fallback = _rule_based(listing, ref, features, region_models, registration, settings)

    raw, fallback_used = await engine.reason_json(
        "verify",
        DEVELOPER_PROMPT + (ORIGIN_RULES if origin else ""),
        _build_content(listing, ref, images, regions, origin, registration, settings),
        fallback=lambda: fallback,
        schema=OUTPUT_SCHEMA,
    )
    valid_ids = {r.id for r in region_models} | {r.id for r, _ in origin} | LISTING_EVIDENCE
    result = _validate(raw, valid_ids, fallback)
    if not region_models and result["confidence"] == "high":
        result["confidence"] = "medium"  # never high confidence without photo evidence

    status = "none" if not listing.registration_id else "found" if registration else "unknown"
    verification = ProductVerification(
        assessment_id=new_id("pv"),
        brand=listing.brand or ref["brand"],
        model=listing.model or ref["model"],
        reference_id=ref["id"],
        evidence=region_models,
        registration_id=listing.registration_id,
        registration_status=status,
        origin_evidence=[r for r, _ in origin],
        engine=engine.name,
        models={"reasoning": engine.models["verify"], "segmentation": engine.models["segmentation"]},
        fallback_used=fallback_used,
        **result,
    )
    store.save_assessment(
        verification.assessment_id, "product",
        {**verification.model_dump(), "listing": listing.model_dump(exclude={"image_urls"}),
         "image_count": len(images)},
    )
    return verification
