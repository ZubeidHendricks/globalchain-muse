import json

import httpx
import pytest
from openai import APIConnectionError

from globalchain import orders
from globalchain.agent import run_agent
from globalchain.engine import MockEngine
from globalchain.vendors import BUYER_WALLET, ensure_seeded, items

PHOTOS = ["/static/demo/tabby-a-1.png", "/static/demo/tabby-a-2.png"]
MAKER = "I'm Lerato Dlamini from Johannesburg. Register my Coach Tabby 26 bag and list it for R5,200."
NEED = "I need a bag for everyday use under $400"


def _call(i: int, name: str, **args) -> dict:
    return {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeMuse(MockEngine):
    """Muse Spark stand-in that plays a fixed list of assistant turns and records what it was sent."""

    name = "meta"
    models = {**MockEngine.models, "agent": "muse-spark-test"}

    def __init__(self, turns):
        self.turns, self.sent = list(turns), []

    async def chat(self, task, messages, tools):
        self.sent.append([dict(m) for m in messages])
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def _tools(out: dict) -> list[str]:
    return [t["tool"] for t in out["trace"] if t["kind"] == "call"]


def _offers(out: dict) -> dict[str, dict]:
    return {r["offer"]["vendor"]: r for r in out["results"]}


async def test_shopper_broadcasts_checks_and_ranks(service):
    out = await run_agent("shop", NEED, service)
    tools = _tools(out)
    assert tools[0] == "broadcast_request" and tools[-1] == "present_recommendations"
    offers = _offers(out)
    assert set(offers) == {"Thandi's Closet", "Cape Town Finds", "LuxDeals ZA"}  # Outlet Imports is over budget
    thandi, lux = offers["Thandi's Closet"]["offer"], offers["LuxDeals ZA"]["offer"]
    assert thandi["vendor_check"] == "verified" and thandi["nft"] == "owned"
    assert lux["vendor_check"] == "unverified" and lux["nft"] == "cloned"
    assert out["results"][-1]["offer"]["vendor"] == "LuxDeals ZA"
    assert offers["LuxDeals ZA"]["recommendation"] == "avoid"
    # Verified vendors sign their pitches; the counterfeiter can't.
    pitches = {m["from"]: m for m in out["conversation"] if m["type"] == "pitch"}
    assert pitches["v_thandi_closet"]["signature"] and "signature" not in pitches["v_luxdeals"]


async def test_order_pay_ship_and_own(service):
    out = await run_agent("shop", NEED, service)
    nxt = await run_agent("shop", "Get me the Coach Tabby from Thandi's Closet, pay with crypto", service,
                          session_id=out["session_id"])
    assert nxt["session_id"] == out["session_id"]
    [order] = nxt["orders"]
    assert order["status"] == "awaiting_payment" and order["payment_method"] == "crypto"
    buyer = service.store.get("wallet", BUYER_WALLET)["account_id"]
    before = await service.ledger.balance_usd(buyer)

    paid = await orders.pay(service, order["order_id"])
    again = await orders.pay(service, order["order_id"])  # a second click does not pay twice
    assert paid["status"] == again["status"] == "owned"
    assert await service.ledger.balance_usd(buyer) == before - order["amount_usd"]
    for _ in range(5):
        await orders.advance(service, order["order_id"])
    t = await orders.track(service, order["order_id"])
    assert [e["status"] for e in t["events"]] == ["owned", "preparing", "shipped", "out_for_delivery", "delivered"]
    assert t["owned_by_you"] and t["tracking"]["number"].startswith("GC")
    assert all(e["hash"] and e["anchor"]["status"] == "unanchored" for e in t["events"])

    # Sold items are no longer pitched, and can't be ordered twice.
    later = await run_agent("shop", NEED, service)
    assert "Thandi's Closet" not in _offers(later)
    with pytest.raises(ValueError):
        await orders.prepare_order(service, order["item_id"], "card")


async def test_simulated_off_chain_rail_moves_the_nft(service):
    out = await run_agent("shop", NEED, service)
    nxt = await run_agent("shop", "buy the Jet Set tote from Cape Town Finds by card", service,
                          session_id=out["session_id"])
    [order] = nxt["orders"]
    assert order["payment_method"] == "card"
    assert (await orders.track(service, (await orders.pay(service, order["order_id"]))["order_id"]))["owned_by_you"]


async def test_agent_refuses_to_order_flagged_offers(service):
    out = await run_agent("shop", NEED, service)
    nxt = await run_agent("shop", "buy the 1:1 Tabby from LuxDeals with eft", service, session_id=out["session_id"])
    assert not nxt["orders"]
    assert "not verified" in nxt["trace"][0]["error"]


async def test_vendor_rules_for_orders(service):
    await ensure_seeded(service)
    jet = next(i for i in items(service.store) if i["vendor_id"] == "v_capetown_finds")
    with pytest.raises(ValueError, match="accepts"):
        await orders.prepare_order(service, jet["item_id"], "mobile_money")
    order = await orders.prepare_order(service, jet["item_id"], "eft")
    with pytest.raises(ValueError, match="isn't paid"):
        await orders.advance(service, order["order_id"])


async def test_maker_becomes_a_vendor_whose_agent_pitches(service):
    made = await run_agent("make", MAKER, service, PHOTOS)
    assert _tools(made) == ["register_product", "list_for_sale"]
    assert made["registration"]["product"]["maker_name"] == "Lerato Dlamini"
    assert made["listing"]["currency"] == "ZAR" and made["listing"]["nft"]
    shop = await run_agent("shop", NEED, service)
    lerato = _offers(shop)["Lerato Dlamini"]["offer"]
    assert lerato["vendor_check"] == "verified" and lerato["nft"] == "owned"


async def test_maker_asks_when_details_missing(service):
    out = await run_agent("make", "Please register this", service, PHOTOS)
    assert out["registration"] is None and "name" in out["answer"]
    out = await run_agent("make", MAKER, service, [])
    assert out["registration"] is None and "photo" in out["answer"]


async def test_muse_policy_replays_tool_calls_and_guardrail_downgrades(service):
    await ensure_seeded(service)
    lux = next(i["item_id"] for i in items(service.store) if i["vendor_id"] == "v_luxdeals")
    service.engine = FakeMuse([
        {"role": "assistant", "content": None, "tool_calls": [_call(1, "broadcast_request", need="everyday bag")]},
        {"role": "assistant", "content": "Checking the cheapest Tabby.", "tool_calls": [
            _call(2, "verify_product", offer_id=lux), _call(3, "assess_seller_risk", offer_id=lux),
            _call(4, "verify_product", offer_id="itm_nope")]},
        {"role": "assistant", "content": None, "tool_calls": [_call(5, "present_recommendations", items=[
            {"offer_id": lux, "recommendation": "recommended", "reason": "Cheap."}])]},
        {"role": "assistant", "content": "Avoid the copy."},
    ])
    out = await run_agent("shop", "everyday bag", service)
    assert out["policy"] == "muse-spark-test" and out["answer"] == "Avoid the copy."
    assert any(t["kind"] == "think" for t in out["trace"])
    last = service.engine.sent[-1]
    assert [m["tool_call_id"] for m in last if m["role"] == "tool"] == ["call_1", "call_2", "call_3", "call_4", "call_5"]
    assert "itm_nope" in next(t for t in out["trace"] if t.get("error"))["error"]
    [card] = out["results"]
    assert card["recommendation"] == "caution" and card["overridden"] is True


async def test_present_requires_checks(service):
    service.engine = FakeMuse([
        {"role": "assistant", "content": None, "tool_calls": [_call(1, "present_recommendations", items=[
            {"offer_id": "itm_x", "recommendation": "recommended", "reason": "x"}])]},
        {"role": "assistant", "content": "ok"},
    ])
    out = await run_agent("shop", "bag", service)
    assert "verify_product" in out["trace"][0]["error"]


async def test_muse_failure_falls_back_to_scripted(service):
    err = APIConnectionError(request=httpx.Request("POST", "https://api.meta.ai/v1/chat/completions"))
    service.engine = FakeMuse([
        {"role": "assistant", "content": None, "tool_calls": [_call(1, "broadcast_request", need=NEED, max_price=400)]},
        err,
    ])
    out = await run_agent("shop", NEED, service)
    assert out["fallback_used"] is True and out["policy"] == "scripted"
    assert any(t["kind"] == "note" for t in out["trace"])
    assert _tools(out)[-1] == "present_recommendations" and out["results"]


async def test_shopper_cannot_call_maker_tools(service):
    service.engine = FakeMuse([
        {"role": "assistant", "content": None, "tool_calls": [_call(1, "register_product", maker_name="x",
                                                                    title="y", photo_ids=[])]},
        {"role": "assistant", "content": "done"},
    ])
    out = await run_agent("shop", "bag", service)
    assert "Unknown tool" in out["trace"][0]["error"]


def test_endpoints(service, monkeypatch):
    from fastapi.testclient import TestClient

    import globalchain.api as api_module

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    with TestClient(api_module.create_app(), base_url="http://localhost:8000") as client:
        out = client.post("/demo/agent", json={"message": NEED}).json()
        nxt = client.post("/demo/agent", json={"message": "get me the Tabby from Thandi's Closet with crypto",
                                               "session_id": out["session_id"]}).json()
        oid = nxt["orders"][0]["order_id"]
        assert client.post(f"/v1/orders/{oid}/advance").status_code == 409  # not paid yet
        assert client.post(f"/v1/orders/{oid}/pay").json()["owned_by_you"] is True
        assert client.post(f"/v1/orders/{oid}/advance").json()["status"] == "preparing"
        assert client.get("/v1/orders/ord_missing").status_code == 404
        assert client.get(f"/track/{oid}").status_code == 200
        w = client.get("/v1/wallet").json()
        assert [i["item_id"] for i in w["owned"]] == [nxt["orders"][0]["item_id"]]
        directory = client.get("/v1/vendors").json()["vendors"]
        assert {v["vendor_id"]: v["verified"] for v in directory}["v_luxdeals"] is False
        assert "wallet_key" not in json.dumps([w, directory]) and "der" not in json.dumps(directory)
        made = client.post("/demo/agent", json={"mode": "make", "message": MAKER, "photos": PHOTOS}).json()
        assert made["registration"]["registration_id"].startswith("reg_")
        assert client.post("/demo/agent", json={"mode": "steal", "message": "x"}).status_code == 422


@pytest.mark.parametrize("path", ["/static/demo/../../api.py", "/static/../api.py"])
def test_inline_rejects_paths_outside_static(path):
    from globalchain.demo import inline_static_urls

    with pytest.raises(ValueError):
        inline_static_urls([path])
