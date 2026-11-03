import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(service, monkeypatch):
    import globalchain.api as api_module
    import globalchain.mcp_server as mcp_module

    monkeypatch.setattr(api_module, "get_service", lambda: service)
    monkeypatch.setattr(mcp_module, "get_service", lambda: service)
    with TestClient(api_module.create_app(), base_url="http://localhost:8000") as c:
        yield c


def test_rest_flow(client):
    out = client.post("/demo/shop", json={"query": "authentic designer handbag under $800"}).json()
    top = out["results"][0]
    rec = client.post("/v1/records", json={
        "product_assessment_id": top["product"]["assessment_id"],
        "seller_assessment_id": top["seller_risk"]["assessment_id"],
        "decision": "approved",
    }).json()
    prov = client.get(f"/v1/records/{rec['record_id']}/provenance").json()
    assert prov["status"] == "unanchored"
    assert client.get(f"/v1/passports/{rec['product_key']}").json()["records"][0]["record_id"] == rec["record_id"]
    assert client.get(f"/passport/{rec['product_key']}").status_code == 200


def test_unknown_record_is_404(client):
    assert client.get("/v1/records/vr_missing/provenance").status_code == 404
    assert client.post("/v1/records", json={"product_assessment_id": "pv_missing"}).status_code == 404


def test_rejects_bad_decision(client):
    assert client.post("/v1/records", json={"product_assessment_id": "x", "decision": "maybe"}).status_code == 422


def test_mcp_http_endpoint_initialises(client):
    resp = client.post("/mcp", headers={"accept": "application/json, text/event-stream"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    })
    assert resp.status_code == 200
    assert "VeriChainX" in resp.text


async def test_mcp_tools_are_listed_with_classes():
    from globalchain.mcp_server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    assert set(tools) == {"verify_product", "assess_seller_risk", "issue_verification_record", "check_provenance",
                          "register_product", "get_registration", "find_products", "create_agent_wallet",
                          "buy_with_agent_wallet", "order_status"}
    for name in ("verify_product", "assess_seller_risk", "check_provenance", "get_registration", "find_products",
                 "order_status"):
        assert tools[name].annotations.read_only_hint is True, name
    for name in ("issue_verification_record", "register_product", "create_agent_wallet", "buy_with_agent_wallet"):
        write = tools[name].annotations
        assert write.read_only_hint is False and write.destructive_hint is False, name


async def test_mcp_tool_call_round_trip(service, demo_items, monkeypatch):
    import globalchain.mcp_server as mcp_module

    monkeypatch.setattr(mcp_module, "get_service", lambda: service)
    listing, seller = demo_items[1]
    result = await mcp_module.mcp.call_tool("assess_seller_risk", {
        "listing": listing.model_dump(), "seller": seller.model_dump()})
    assert "risk_score" in str(result)
