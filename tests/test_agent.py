import json

import httpx
import pytest
from openai import APIConnectionError

from globalchain.agent import run_agent
from globalchain.engine import MockEngine

PHOTOS = ["/static/demo/tabby-a-1.png", "/static/demo/tabby-a-2.png"]
MAKER = "I'm Thandi Mokoena from Johannesburg. Register my Coach Tabby 26 bag and list it for R5,200."


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


async def test_maker_then_shopper_scripted(service):
    made = await run_agent("make", MAKER, service, PHOTOS)
    assert _tools(made) == ["register_product", "list_for_sale"]
    reg_id = made["registration"]["registration_id"]
    assert made["listing"]["registration_id"] == reg_id
    assert made["listing"]["currency"] == "ZAR" and made["listing"]["price"] == 5200
    assert made["registration"]["product"]["maker_name"] == "Thandi Mokoena"
    assert made["policy"] == "scripted" and reg_id in made["answer"]

    shop = await run_agent("shop", "Find me an authentic Coach Tabby under $800", service)
    tools = _tools(shop)
    assert tools[0] == "search_listings" and tools[-1] == "present_recommendations"
    assert "get_registration" in tools
    by_id = {r["listing_id"]: r for r in shop["results"]}
    assert by_id[made["listing"]["listing_id"]]["product"]["registration_status"] == "found"
    assert shop["results"][-1]["recommendation"] == "avoid"  # the 1:1 replica
    assert "lst_4004" not in by_id  # $1,100 is over budget


async def test_maker_asks_when_details_missing(service):
    out = await run_agent("make", "Please register this", service, PHOTOS)
    assert out["registration"] is None and "name" in out["answer"]
    out = await run_agent("make", MAKER, service, [])
    assert out["registration"] is None and "photo" in out["answer"]


async def test_muse_policy_replays_tool_calls_and_guardrail_downgrades(service):
    service.engine = FakeMuse([
        {"role": "assistant", "content": None, "tool_calls": [_call(1, "search_listings", query="coach tabby")]},
        {"role": "assistant", "content": "Checking the replica first.", "tool_calls": [
            _call(2, "verify_product", listing_id="lst_2002"), _call(3, "assess_seller_risk", listing_id="lst_2002"),
            _call(4, "verify_product", listing_id="lst_nope")]},
        {"role": "assistant", "content": None, "tool_calls": [_call(5, "present_recommendations", items=[
            {"listing_id": "lst_2002", "recommendation": "recommended", "reason": "Cheap."}])]},
        {"role": "assistant", "content": "Avoid the replica."},
    ])
    out = await run_agent("shop", "coach tabby", service)
    assert out["policy"] == "muse-spark-test" and out["answer"] == "Avoid the replica."
    assert any(t["kind"] == "think" for t in out["trace"])
    # Each tool result goes back with the id of the call it answers.
    last = service.engine.sent[-1]
    assert [m["tool_call_id"] for m in last if m["role"] == "tool"] == ["call_1", "call_2", "call_3", "call_4", "call_5"]
    errors = [t for t in out["trace"] if t.get("error")]
    assert errors and "lst_nope" in errors[0]["error"]
    [card] = out["results"]
    assert card["recommendation"] == "caution" and card["overridden"] is True


async def test_present_requires_checks(service):
    service.engine = FakeMuse([
        {"role": "assistant", "content": None, "tool_calls": [_call(1, "present_recommendations", items=[
            {"listing_id": "lst_1001", "recommendation": "recommended", "reason": "x"}])]},
        {"role": "assistant", "content": "ok"},
    ])
    out = await run_agent("shop", "bag", service)
    assert "verify_product" in out["trace"][0]["error"]


async def test_muse_failure_falls_back_to_scripted(service):
    err = APIConnectionError(request=httpx.Request("POST", "https://api.meta.ai/v1/chat/completions"))
    service.engine = FakeMuse([
        {"role": "assistant", "content": None, "tool_calls": [_call(1, "search_listings", query="coach tabby")]},
        err,
    ])
    out = await run_agent("shop", "coach tabby", service)
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


def test_agent_endpoint(service, monkeypatch):
    from fastapi.testclient import TestClient

    import globalchain.api as api_module

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    with TestClient(api_module.create_app(), base_url="http://localhost:8000") as client:
        out = client.post("/demo/agent", json={"mode": "make", "message": MAKER, "photos": PHOTOS}).json()
        assert out["registration"]["registration_id"].startswith("reg_")
        assert client.post("/demo/agent", json={"mode": "steal", "message": "x"}).status_code == 422
        bad = client.post("/demo/agent", json={"mode": "make", "message": MAKER, "photos": ["/static/../api.py"]})
        assert "Not a demo image" in json.dumps(bad.json())


@pytest.mark.parametrize("path", ["/static/demo/../../api.py"])
def test_inline_rejects_paths_outside_static(path):
    from globalchain.demo import inline_static_urls

    with pytest.raises(ValueError):
        inline_static_urls([path])
