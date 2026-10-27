"""FastAPI app: REST API, the MCP streamable HTTP endpoint at /mcp, and the web demo."""

import base64
import contextlib
import logging
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import run_agent
from .demo import shop
from .mcp_server import mcp
from .provenance import NotFound
from .registry import qr_png
from .schemas import (
    IssueRecordRequest,
    ProductVerification,
    ProvenanceCheck,
    RegisterProductRequest,
    Registration,
    SellerRisk,
    SellerRiskRequest,
    VerificationRecord,
    VerifyProductRequest,
)
from .service import get_service

logging.basicConfig(level=logging.INFO)
_STATIC = Path(__file__).parent / "static"

router = APIRouter()


@router.get("/healthz", tags=["system"])
async def healthz() -> dict:
    service = get_service()
    return {"ok": True, "engine": service.engine.name, "models": service.engine.models,
            "anchoring": type(service.anchorer).__name__, "certificates": type(service.minter).__name__,
            "network": service.settings.hedera_network}


@router.post("/v1/registrations", response_model=Registration, tags=["origin"])
async def register(req: RegisterProductRequest):
    try:
        return await get_service().register_product(req.product)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/v1/registrations/{registration_id}", response_model=Registration, tags=["origin"])
async def registration(registration_id: str):
    try:
        return get_service().get_registration(registration_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/v1/registrations/{registration_id}/qr.png", tags=["origin"])
async def registration_qr(registration_id: str):
    try:
        reg = get_service().get_registration(registration_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(qr_png(reg.origin_url), media_type="image/png")


@router.get("/v1/registrations/{registration_id}/crops/{region_id}.jpg", tags=["origin"])
async def registration_crop(registration_id: str, region_id: str):
    try:
        data_url = get_service().registration_crop(registration_id, region_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(base64.b64decode(data_url.partition(",")[2]), media_type="image/jpeg")


@router.post("/v1/verify-product", response_model=ProductVerification, tags=["verification"])
async def verify_product(req: VerifyProductRequest):
    return await get_service().verify_product(req.listing)


@router.post("/v1/seller-risk", response_model=SellerRisk, tags=["verification"])
async def seller_risk(req: SellerRiskRequest):
    return await get_service().assess_seller_risk(req.listing, req.seller)


@router.post("/v1/records", response_model=VerificationRecord, tags=["provenance"])
async def issue_record(req: IssueRecordRequest):
    try:
        return await get_service().issue_verification_record(
            req.product_assessment_id, req.seller_assessment_id, req.decision
        )
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/v1/records/{record_id}/provenance", response_model=ProvenanceCheck, tags=["provenance"])
async def provenance(record_id: str):
    try:
        return await get_service().check_provenance(record_id)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/v1/passports/{product_key}", tags=["provenance"])
async def passport(product_key: str) -> dict:
    records = get_service().passport(product_key)
    if not records:
        raise HTTPException(404, f"No records for {product_key}")
    return {"product_key": product_key, "records": records}


class ShopRequest(BaseModel):
    query: str


class AgentRequest(BaseModel):
    mode: Literal["shop", "make"] = "shop"
    message: str
    photos: list[str] = Field(default_factory=list, description="Demo image paths (/static/demo/...) or data: URLs")


@router.post("/demo/agent", tags=["demo"])
async def demo_agent(req: AgentRequest) -> dict:
    try:
        return await run_agent(req.mode, req.message, get_service(), req.photos[:4])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/demo/shop", tags=["demo"])
async def demo_shop(req: ShopRequest) -> dict:
    return await shop(req.query, get_service())


@router.get("/", include_in_schema=False)
async def index():
    return FileResponse(_STATIC / "index.html")


@router.get("/passport/{product_key}", include_in_schema=False)
async def passport_page(product_key: str):
    return FileResponse(_STATIC / "passport.html")


@router.get("/origin/{registration_id}", include_in_schema=False)
async def origin_page(registration_id: str):
    return FileResponse(_STATIC / "origin.html")


def create_app() -> FastAPI:
    settings = get_service().settings
    host = urlparse(settings.public_base_url).hostname or "127.0.0.1"
    # Localhost keeps the SDK's DNS-rebinding protection; a public host serves remote agents.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", host=host if host in ("localhost", "127.0.0.1") else "0.0.0.0"
    )
    session_manager = mcp.session_manager  # one per streamable_http_app() call

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        async with session_manager.run():
            yield

    app = FastAPI(
        title="VeriChainX",
        version="0.1.0",
        description="Trust layer for agentic shopping: verify products, assess sellers, anchor records on Hedera.",
        lifespan=lifespan,
    )
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    # Last, so the routes above take precedence; serves POST/GET /mcp.
    app.mount("/", mcp_app)
    return app


app = create_app()
