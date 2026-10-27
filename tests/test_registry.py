import base64
from pathlib import Path

import pytest

from globalchain.certificates import MAX_METADATA_BYTES, certificate_metadata
from globalchain.engine import MockEngine
from globalchain.provenance import NotFound
from globalchain.registry import _validate_features, qr_png
from globalchain.schemas import Listing, MakerProduct

DEMO = Path(__file__).parent.parent / "globalchain" / "static" / "demo"


def _photo(name: str) -> str:
    return "data:image/png;base64," + base64.b64encode((DEMO / name).read_bytes()).decode()


@pytest.fixture
def product() -> MakerProduct:
    return MakerProduct(maker_name="Thandi Mokoena", maker_country="ZA", title="Coach Tabby 26 Shoulder Bag",
                        brand="Coach", model="Tabby 26", image_urls=[_photo("tabby-a-1.png"), _photo("tabby-a-2.png")])


class FeaturePicker(MockEngine):
    """Muse Spark stand-in that proposes fingerprint features, including ones that must be rejected."""

    async def reason_json(self, task, developer, content, fallback, schema=None):
        if task != "register":
            return await super().reason_json(task, developer, content, fallback, schema)
        return {"features": [
            {"name": "Clasp engraving", "sam_prompt": "metal clasp", "why": "Engraved C with a nick on the left."},
            {"name": "clasp engraving", "sam_prompt": "metal clasp", "why": "duplicate"},
            {"name": "Tags", "sam_prompt": "tag, label", "why": "two concepts"},
            {"name": "Seams", "sam_prompt": "seam or stitch", "why": "two concepts"},
            {"name": "Creed patch", "sam_prompt": "leather patch", "why": "Serial stamped by hand."},
        ]}, False


def test_feature_validation_keeps_single_concept_prompts():
    out = _validate_features({"features": [
        {"name": "A", "sam_prompt": "Metal Clasp", "why": "x"},
        {"name": "B", "sam_prompt": "tag and label", "why": "x"},
        {"name": "C", "sam_prompt": "a very long prompt with far too many words", "why": "x"},
        {"name": "a", "sam_prompt": "zipper", "why": "duplicate name"},
        "not a dict",
    ]})
    assert out == [{"name": "A", "sam_prompt": "metal clasp", "why": "x"}]


def test_certificate_metadata_fits_hts_limit():
    meta = certificate_metadata("reg_" + "f" * 16, "a" * 64)
    assert len(meta) <= MAX_METADATA_BYTES
    assert meta.startswith(b"gcm:reg_")


def test_qr_code_is_png():
    assert qr_png("http://localhost:8000/origin/reg_x").startswith(b"\x89PNG")


async def test_register_falls_back_to_reference_features(service, product):
    reg = await service.register_product(product)
    assert reg.fallback_used is True
    assert reg.fingerprint and all(r.id.startswith("R") for r in reg.fingerprint)
    assert reg.anchor.status == "unanchored"
    assert reg.certificate.status == "not_minted"
    assert reg.origin_url.endswith(f"/origin/{reg.registration_id}")
    assert service.get_registration(reg.registration_id).registration_hash == reg.registration_hash
    assert service.registration_crop(reg.registration_id, "R1").startswith("data:image/jpeg")


async def test_register_uses_muse_spark_features(service, product):
    service.engine = FeaturePicker()
    reg = await service.register_product(product)
    assert reg.fallback_used is False
    assert [r.feature for r in reg.fingerprint] == ["Clasp engraving", "Creed patch"]
    assert reg.rationale["Creed patch"] == "Serial stamped by hand."


async def test_register_without_loadable_photos_is_rejected(service, product):
    with pytest.raises(ValueError):
        await service.register_product(product.model_copy(update={"image_urls": ["data:image/png;base64,AAAA"]}))


async def test_unknown_registration_is_not_found(service):
    with pytest.raises(NotFound):
        service.get_registration("reg_missing")


async def test_verify_compares_against_origin_fingerprint(service, product):
    service.engine = FeaturePicker()
    reg = await service.register_product(product)
    listing = Listing(title="Coach Tabby 26, pre-loved", price=300, brand="Coach", model="Tabby 26",
                      image_urls=[_photo("tabby-b-1.png")], registration_id=reg.registration_id)
    result = await service.verify_product(listing)
    assert result.registration_status == "found"
    assert [r.id for r in result.origin_evidence] == ["R1", "R2"]
    # The listing is segmented for the features the maker registered, not the brand's generic list.
    assert {e.feature for e in result.evidence} <= {"Clasp engraving", "Creed patch"}

    record = await service.issue_verification_record(result.assessment_id, decision="approved")
    assert record.record["product"]["registration_id"] == reg.registration_id
    # Every resale of a registered item lands in the same passport.
    again = await service.verify_product(listing.model_copy(update={"title": "Coach Tabby, relisted"}))
    record2 = await service.issue_verification_record(again.assessment_id)
    assert record2.product_key == record.product_key


async def test_unknown_registration_counts_against_listing(service):
    base = Listing(title="Coach Tabby 26", price=300, brand="Coach", model="Tabby 26")
    plain = await service.verify_product(base)
    faked = await service.verify_product(base.model_copy(update={"registration_id": "reg_doesnotexist"}))
    assert faked.registration_status == "unknown"
    assert faked.authenticity_score < plain.authenticity_score
    assert any("does not exist" in f.observation for f in faked.findings)


def test_registration_endpoints(service, product, monkeypatch):
    from fastapi.testclient import TestClient

    import globalchain.api as api_module

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    with TestClient(api_module.create_app(), base_url="http://localhost:8000") as client:
        reg = client.post("/v1/registrations", json={"product": product.model_dump()}).json()
        rid = reg["registration_id"]
        assert client.get(f"/v1/registrations/{rid}").json()["registration_hash"] == reg["registration_hash"]
        qr = client.get(f"/v1/registrations/{rid}/qr.png")
        assert qr.headers["content-type"] == "image/png"
        crop = client.get(f"/v1/registrations/{rid}/crops/R1.jpg")
        assert crop.content.startswith(b"\xff\xd8")
        assert client.get(f"/origin/{rid}").status_code == 200
        assert client.get("/v1/registrations/reg_missing").status_code == 404
        assert client.get(f"/v1/registrations/{rid}/crops/R99.jpg").status_code == 404
        bad = product.model_dump() | {"image_urls": ["data:image/png;base64,AAAA"]}
        assert client.post("/v1/registrations", json={"product": bad}).status_code == 400
