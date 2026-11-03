import pytest

from globalchain import agent_wallets, orders
from globalchain.limits import RateLimiter, client_ip
from globalchain.scorecard import combine, value_row

NEED = "I need a bag for everyday use"


def _by_vendor(found: dict) -> dict[str, dict]:
    return {r["vendor"]: r for r in found["results"]}


async def test_find_products_returns_checked_offers_with_scorecards(service):
    found = await agent_wallets.find_products(service, NEED, max_price_usd=400)
    offers = _by_vendor(found)
    assert found["session_id"].startswith("conv_")
    lux, thandi = offers["LuxDeals ZA"], offers["Thandi's Closet"]
    assert lux["recommendation"] == "avoid" and lux["buy"] == "blocked"
    assert lux["scorecard"]["overall"] <= 20 and lux["scorecard"]["origin"]["score"] == 0
    assert thandi["scorecard"]["origin"]["score"] == 100
    assert thandi["scorecard"]["overall"] > lux["scorecard"]["overall"]


async def test_agent_wallet_buys_for_its_owner_within_the_cap(service):
    found = await agent_wallets.find_products(service, NEED, max_price_usd=400)
    offers = _by_vendor(found)
    thandi = offers["Thandi's Closet"]
    other = next(r for r in found["results"] if r["recommendation"] != "avoid" and r["offer_id"] != thandi["offer_id"])
    w = await agent_wallets.create_agent_wallet(service, "test bot", thandi["price_usd"] + 1)
    owner = await service.ledger.create_wallet("owner")

    with pytest.raises(ValueError, match="max_price_usd"):
        await agent_wallets.buy_with_agent_wallet(service, found["session_id"], thandi["offer_id"], w["wallet_key"], 10)
    with pytest.raises(ValueError, match="Unknown wallet_key"):
        await agent_wallets.buy_with_agent_wallet(service, found["session_id"], thandi["offer_id"], "nope", 400)
    with pytest.raises(ValueError, match="Won't order"):
        await agent_wallets.buy_with_agent_wallet(service, found["session_id"], offers["LuxDeals ZA"]["offer_id"],
                                                  w["wallet_key"], 400)

    bought = await agent_wallets.buy_with_agent_wallet(service, found["session_id"], thandi["offer_id"],
                                                       w["wallet_key"], 400, recipient_account=owner.account_id)
    assert bought["status"] == "owned" and bought["nft_owner"] == owner.account_id
    assert bought["payer"] == w["wallet"]["account_id"] and bought["spent_usd"] == thandi["price_usd"]
    assert await service.ledger.balance_usd(w["wallet"]["account_id"]) == pytest.approx(1)
    status = await agent_wallets.order_status(service, bought["order_id"])
    assert status["owned_by_recipient"] and status["events"][0]["status"] == "owned"

    # The cap counts what's already spent.
    with pytest.raises(ValueError, match="spending cap"):
        await agent_wallets.buy_with_agent_wallet(service, found["session_id"], other["offer_id"], w["wallet_key"], 400)


async def test_agent_wallet_limits(service):
    with pytest.raises(ValueError):
        await agent_wallets.create_agent_wallet(service, "greedy", 5000)
    w = await agent_wallets.create_agent_wallet(service, "bot", 10)
    with pytest.raises(ValueError, match="find_products"):
        await agent_wallets.buy_with_agent_wallet(service, "conv_missing", "itm_x", w["wallet_key"], 10)


def test_scorecard_caps_avoid_and_skips_unknown_parts():
    rows = [{"key": "authentic", "label": "Authentic", "score": 90, "note": ""},
            {"key": "delivery", "label": "Delivery", "score": None, "note": ""}]
    assert combine(rows, "recommended")["overall"] == 90
    assert combine(rows, "avoid")["overall"] == 20
    assert combine(rows, "caution")["grade"] == "Fair"
    assert value_row(100, [100, 200]) ["score"] is None
    assert value_row(50, [100, 100, 100])["score"] > value_row(150, [100, 100, 100])["score"]


def test_rate_limiter_window_and_proxy_ip():
    lim = RateLimiter(2)
    assert lim.allow("a", 0) is None and lim.allow("a", 1) is None
    assert lim.allow("a", 2) == pytest.approx(58)
    assert lim.allow("b", 2) is None
    assert lim.allow("a", 61) is None
    scope = {"client": ("10.0.0.1", 1), "headers": [(b"x-forwarded-for", b"6.6.6.6, 1.2.3.4")]}
    assert client_ip(scope, trust_proxy=True) == "1.2.3.4"
    assert client_ip(scope, trust_proxy=False) == "10.0.0.1"


def test_rate_limit_returns_429(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from globalchain.limits import RateLimitMiddleware

    app = FastAPI()

    @app.post("/demo/agent")
    async def agent():
        return {"ok": True}

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, per_minute=2)
    c = TestClient(app)
    assert [c.post("/demo/agent").status_code for _ in range(3)] == [200, 200, 429]
    assert c.get("/healthz").status_code == 200  # only model and Hedera routes are limited


async def test_daily_model_budget_falls_back_to_rules(settings):
    from globalchain.engine import BudgetExceeded, DailyBudget

    b = DailyBudget(2)
    b.spend(); b.spend()
    with pytest.raises(BudgetExceeded):
        b.spend()
    DailyBudget(0).spend()  # 0 means no cap
