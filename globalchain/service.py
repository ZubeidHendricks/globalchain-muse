"""GlobalChain operations, shared by the REST API and the MCP server.

VeriChainX checks (verify, seller risk, records, provenance) for buyers and their agents, and
origin registration for makers.
"""

from functools import lru_cache

from .certificates import Minter, NullMinter, build_minter
from .config import Settings, get_settings
from .engine import Engine, build_engine
from .ledger import Ledger, SimulatedLedger, build_ledger
from .provenance import Anchorer, build_anchorer, check_provenance, issue_verification_record
from .registry import get_registration, register_product, registration_crop
from .schemas import (
    Listing,
    MakerProduct,
    ProductVerification,
    ProvenanceCheck,
    Registration,
    Seller,
    SellerRisk,
    VerificationRecord,
)
from .seller import assess_seller_risk
from .store import Store
from .verify import verify_product


class GlobalChain:
    def __init__(self, settings: Settings, engine: Engine, store: Store, anchorer: Anchorer,
                 minter: Minter | None = None, ledger: Ledger | None = None):
        self.settings = settings
        self.engine = engine
        self.store = store
        self.anchorer = anchorer
        self.minter = minter or NullMinter()
        self.ledger = ledger or SimulatedLedger(store)

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

    async def register_product(self, product: MakerProduct) -> Registration:
        return await register_product(product, self.engine, self.store, self.anchorer, self.minter, self.settings)

    def get_registration(self, registration_id: str) -> Registration:
        return get_registration(registration_id, self.store)

    def registration_crop(self, registration_id: str, region_id: str) -> str:
        return registration_crop(registration_id, region_id, self.store)


@lru_cache
def get_service() -> GlobalChain:
    settings = get_settings()
    store = Store(settings.database_path)
    return GlobalChain(settings, build_engine(settings), store, build_anchorer(settings), build_minter(settings),
                       build_ledger(settings, store))
