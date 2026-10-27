"""The four VeriChainX operations, shared by the REST API and the MCP server."""

from functools import lru_cache

from .config import Settings, get_settings
from .engine import Engine, build_engine
from .provenance import Anchorer, build_anchorer, check_provenance, issue_verification_record
from .schemas import Listing, ProductVerification, ProvenanceCheck, Seller, SellerRisk, VerificationRecord
from .seller import assess_seller_risk
from .store import Store
from .verify import verify_product


class VeriChainX:
    def __init__(self, settings: Settings, engine: Engine, store: Store, anchorer: Anchorer):
        self.settings = settings
        self.engine = engine
        self.store = store
        self.anchorer = anchorer

    async def verify_product(self, listing: Listing) -> ProductVerification:
        return await verify_product(listing, self.engine, self.store, self.settings)

    async def assess_seller_risk(self, listing: Listing, seller: Seller) -> SellerRisk:
        return await assess_seller_risk(listing, seller, self.engine, self.store, self.settings)

    async def issue_verification_record(
        self, product_assessment_id: str, seller_assessment_id: str | None = None, decision: str = "none"
    ) -> VerificationRecord:
        return await issue_verification_record(
            product_assessment_id, seller_assessment_id, decision, self.store, self.anchorer, self.settings
        )

    async def check_provenance(self, record_id: str) -> ProvenanceCheck:
        return await check_provenance(record_id, self.store, self.settings)

    def passport(self, product_key: str) -> list[dict]:
        return self.store.records_for_product(product_key)


@lru_cache
def get_service() -> VeriChainX:
    settings = get_settings()
    return VeriChainX(settings, build_engine(settings), Store(settings.database_path), build_anchorer(settings))
