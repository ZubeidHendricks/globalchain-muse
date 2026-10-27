import json

import httpx
from openai import AsyncOpenAI

from globalchain.config import Settings
from globalchain.engine import MetaEngine

BASE = "https://api.meta.ai/v1"
SCHEMA = {"name": "t", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}


def _engine(*responses: httpx.Response) -> tuple[MetaEngine, list[httpx.Request]]:
    """MetaEngine whose HTTP calls are answered in order by `responses` and never leave the process."""
    calls: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return queue.pop(0)

    engine = MetaEngine(Settings(model_api_key="test", meta_api_base=BASE))
    engine.client = AsyncOpenAI(
        base_url=BASE, api_key="test", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return engine, calls


def _completion(content: str) -> dict:
    return {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "muse-spark-1.3",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    }


async def test_sends_json_schema_response_format():
    engine, calls = _engine(httpx.Response(200, json=_completion('{"ok": true}')))
    out = await engine.reason_json("verify", "dev", [{"type": "text", "text": "hi"}], lambda: {"fb": 1}, schema=SCHEMA)
    assert out == ({"ok": True}, False)
    assert calls[0].url == f"{BASE}/chat/completions"
    body = json.loads(calls[0].content)
    assert body["response_format"] == {"type": "json_schema", "json_schema": SCHEMA}
    assert body["messages"][0]["role"] == "developer"


async def test_retries_without_response_format_when_rejected():
    engine, calls = _engine(
        httpx.Response(400, json={"error": {"message": "unsupported response_format"}}),
        httpx.Response(200, json=_completion('{"ok": true}')),
    )
    assert await engine.reason_json("verify", "dev", [], lambda: {"fb": 1}, schema=SCHEMA) == ({"ok": True}, False)
    assert "response_format" not in json.loads(calls[1].content)
    assert engine._json_mode is False


async def test_api_failure_degrades_to_fallback():
    engine, calls = _engine(httpx.Response(503, json={"error": {"message": "busy"}}))
    assert await engine.reason_json("verify", "dev", [], lambda: {"fb": 1}, schema=SCHEMA) == ({"fb": 1}, True)
    assert len(calls) == 1


async def test_unparseable_reply_is_flagged_as_fallback():
    engine, _ = _engine(httpx.Response(200, json=_completion("no json here")))
    assert await engine.reason_json("verify", "dev", [], lambda: {"fb": 1}) == ({"fb": 1}, True)


async def test_routes_each_task_to_its_model():
    engine, calls = _engine(
        httpx.Response(200, json=_completion("{}")),
        httpx.Response(200, json=_completion("{}")),
    )
    engine.models = MetaEngine(Settings(model_api_key="test", verify_model="muse-wonder-1.0")).models
    await engine.reason_json("verify", "dev", [], lambda: {})
    await engine.reason_json("seller", "dev", [], lambda: {})
    assert [json.loads(c.content)["model"] for c in calls] == ["muse-wonder-1.0", "muse-spark-1.3"]
