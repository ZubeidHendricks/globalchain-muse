from globalchain.ledger import SimulatedLedger, verify_signature
from globalchain.vendors import check_envelope, ensure_seeded, envelope, vendor


async def test_signatures_verify_against_the_account_key(service):
    ledger = SimulatedLedger(service.store)
    w = await ledger.create_wallet("v")
    sig = ledger.sign(w.account_id, b"pitch")
    assert verify_signature(w.public_key, sig, b"pitch")
    assert not verify_signature(w.public_key, sig, b"pitch, edited")
    assert not verify_signature("zz", sig, b"pitch")


async def test_tampered_pitch_is_caught(service):
    await ensure_seeded(service)
    v = vendor(service.store, "v_thandi_closet")
    env = envelope(service, "conv_t", v, "shopper", "pitch", {"offers": [], "message": "hi"})
    assert (await check_envelope(service, env))["status"] == "verified"
    tampered = {**env, "body": {"offers": [], "message": "50% off, EFT only"}}
    assert (await check_envelope(service, tampered))["status"] == "invalid_signature"
    # Claiming to be a verified vendor without its key doesn't work either.
    spoof = {**env, "signature": None}
    assert (await check_envelope(service, spoof))["status"] == "unverified"


async def test_swap_is_all_or_nothing(service):
    ledger = SimulatedLedger(service.store)
    seller, buyer = await ledger.create_wallet("s"), await ledger.create_wallet("b", fund_usd=10)
    token, serial, _ = await ledger.mint_item(b"m", seller.account_id)
    short = await ledger.swap(token, serial, seller.account_id, buyer.account_id, 25)
    assert short.status == "failed"
    assert await ledger.nft_owner(token, serial) == seller.account_id  # nothing moved
    assert await ledger.balance_usd(buyer.account_id) == 10
    ok = await ledger.swap(token, serial, seller.account_id, buyer.account_id, 7.5)
    assert ok.status == "success" and await ledger.nft_owner(token, serial) == buyer.account_id
    assert await ledger.balance_usd(seller.account_id) == 7.5
    # The seller can't sell it again.
    assert (await ledger.swap(token, serial, seller.account_id, buyer.account_id, 1)).status == "failed"


def test_hedera_ledger_refuses_mainnet(service):
    import pytest

    from globalchain.ledger import HederaLedger

    settings = service.settings.model_copy(update={"hedera_network": "mainnet", "operator_id": "0.0.2",
                                                   "operator_key": "x"})
    with pytest.raises(ValueError, match="testnet-only"):
        HederaLedger(settings, service.store)
