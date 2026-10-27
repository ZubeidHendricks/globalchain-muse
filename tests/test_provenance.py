import base64
import json

import httpx
import pytest
import respx

from globalchain.provenance import NotFound, canonical_json, record_hash
from globalchain.schemas import Anchor


async def _assessed(service, demo_items, i=0):
    listing, seller = demo_items[i]
    product = await service.verify_product(listing)
    risk = await service.assess_seller_risk(listing, seller)
    return product, risk


async def test_record_is_idempotent_and_unanchored_without_keys(service, demo_items):
    product, risk = await _assessed(service, demo_items)
    first = await service.issue_verification_record(product.assessment_id, risk.assessment_id, "approved")
    again = await service.issue_verification_record(product.assessment_id, risk.assessment_id, "approved")
    assert first.record_id == again.record_id
    assert first.anchor.status == "unanchored"
    assert first.record_hash == record_hash(first.record)
    assert (await service.check_provenance(first.record_id)).status == "unanchored"


async def test_unknown_assessment_is_rejected(service):
    with pytest.raises(NotFound):
        await service.issue_verification_record("pv_nope")


async def test_tampering_is_detected(service, demo_items):
    product, risk = await _assessed(service, demo_items)
    rec = await service.issue_verification_record(product.assessment_id, risk.assessment_id, "approved")
    tampered = dict(rec.record, decision="skipped")
    with service.store._conn:
        service.store._conn.execute("UPDATE records SET record = ? WHERE id = ?",
                                    (json.dumps(tampered), rec.record_id))
    assert (await service.check_provenance(rec.record_id)).status == "hash_mismatch"


class FakeAnchorer:
    def __init__(self):
        self.messages = []

    async def anchor(self, message):
        self.messages.append(message)
        return Anchor(status="anchored", network="testnet", topic_id="0.0.1234", sequence_number=7,
                      explorer_url="https://hashscan.io/testnet/topic/0.0.1234")


@respx.mock
async def test_mirror_node_verification(service, demo_items):
    service.anchorer = FakeAnchorer()
    product, risk = await _assessed(service, demo_items)
    rec = await service.issue_verification_record(product.assessment_id, risk.assessment_id, "approved")

    message = service.anchorer.messages[0]
    assert json.loads(message)["h"] == rec.record_hash
    assert "Thandi" not in message  # only the hash and ids go on-chain

    route = respx.get("https://testnet.mirrornode.hedera.com/api/v1/topics/0.0.1234/messages/7")
    route.return_value = httpx.Response(200, json={
        "message": base64.b64encode(message.encode()).decode(), "consensus_timestamp": "1761000000.000000001"})
    check = await service.check_provenance(rec.record_id)
    assert check.status == "verified"
    assert check.anchor.consensus_timestamp == "1761000000.000000001"

    forged = canonical_json({"t": "vcx.record.v1", "id": rec.record_id, "h": "0" * 64})
    route.return_value = httpx.Response(200, json={"message": base64.b64encode(forged.encode()).decode()})
    assert (await service.check_provenance(rec.record_id)).status == "hash_mismatch"

    route.return_value = httpx.Response(503)
    assert (await service.check_provenance(rec.record_id)).status == "chain_unavailable"


async def test_passport_groups_records_for_same_listing(service, demo_items):
    product, risk = await _assessed(service, demo_items)
    a = await service.issue_verification_record(product.assessment_id, risk.assessment_id, "approved")
    product2, _ = await _assessed(service, demo_items)
    b = await service.issue_verification_record(product2.assessment_id, None, "none")
    assert a.product_key == b.product_key
    assert [r["record_id"] for r in service.passport(a.product_key)] == [a.record_id, b.record_id]
    assert len((await service.check_provenance(b.record_id)).history) == 2
