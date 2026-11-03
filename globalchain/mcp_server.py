"""MCP server exposing GlobalChain to shopping agents and maker agents.

Run over stdio (`globalchain-mcp`) for local agents, or use the streamable HTTP
endpoint the FastAPI app mounts at /mcp.

Tool classes follow the Muse Connector guidelines: six read tools and four writes
(issue_verification_record, register_product, create_agent_wallet and buy_with_agent_wallet, which
write to Hedera). None are destructive. buy_with_agent_wallet is the only one that spends, from a
demo wallet with a spending cap the caller set.
"""

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import agent_wallets
from .provenance import NotFound
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
from .service import get_service

INSTRUCTIONS = """GlobalChain links an item from its maker to every later buyer.
Buying: VeriChainX is a trust check to run before checkout. For each candidate listing, call
verify_product and assess_seller_risk, show the shopper the verdict, cited evidence and risk
explanation, and let them decide. If the listing or its QR code gives a registration id
("reg_..."), pass it as listing.registration_id so the photos are compared with the item's origin
fingerprint. Once the shopper approves or skips, call issue_verification_record with both
assessment ids and their decision.
Making: when a maker wants to register an item, collect clear photos (close-ups of marks, tags and
signatures help) and call register_product. Give the maker the origin_url and qr_url to attach.
Shopping for someone: call find_products with what they want; GlobalChain's shopper agent asks
the vendor agents, checks every offer (signatures and NFTs on Hedera, photos, seller risk) and
returns them ranked with a 0-100 scorecard. To buy, create a wallet once with create_agent_wallet
(the owner sets the spending cap), then call buy_with_agent_wallet with the session_id and
offer_id; it refuses anything the checks rule out, over max_price_usd or over the cap. Confirm
with the person before buying. Track with order_status.
Scores are evidence-based estimates, not guarantees. A Hedera anchor proves what was recorded and
when, not that the item is genuine. Say so if asked."""

mcp = MCPServer(name="GlobalChain", instructions=INSTRUCTIONS, version="0.2.0")


@mcp.tool(annotations=ToolAnnotations(title="Verify product", read_only_hint=True, open_world_hint=True))
async def verify_product(listing: Listing) -> ProductVerification:
    """Check whether a listing's product is likely genuine.

    SAM 3.1 segments the regions that matter for the claimed brand (logos, labels, stitching,
    serial plates, packaging) and Muse Spark compares them with brand reference data. If
    listing.registration_id is set, the photos are compared with that item's origin fingerprint
    instead, and an unknown id counts against the listing. Every finding cites an evidence id.
    Returns an assessment_id to pass to issue_verification_record.
    """
    return await get_service().verify_product(listing)


@mcp.tool(annotations=ToolAnnotations(title="Assess seller risk", read_only_hint=True, open_world_hint=True))
async def assess_seller_risk(listing: Listing, seller: Seller) -> SellerRisk:
    """Score how risky it is to buy this listing from this seller (0 = low, 100 = high).

    Rule-based signals (pricing anomalies, account age, reviews, listing language, payment
    methods) set a base score; Muse Spark can adjust it by at most 15 points and must cite the
    signals it relied on. Returns an assessment_id to pass to issue_verification_record.
    """
    return await get_service().assess_seller_risk(listing, seller)


@mcp.tool(annotations=ToolAnnotations(
    title="Issue verification record", read_only_hint=False, destructive_hint=False,
    idempotent_hint=True, open_world_hint=True,
))
async def issue_verification_record(
    product_assessment_id: str, seller_assessment_id: str | None = None, decision: str = "none"
) -> VerificationRecord:
    """Hash the assessments and anchor the hash on Hedera (Consensus Service).

    decision is the shopper's choice: "approved", "skipped" or "none". Calling again with the
    same arguments returns the same record. Returns a product passport URL for resale.
    """
    if decision not in ("approved", "skipped", "none"):
        raise ValueError("decision must be 'approved', 'skipped' or 'none'")
    try:
        return await get_service().issue_verification_record(product_assessment_id, seller_assessment_id, decision)
    except NotFound as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(title="Check provenance", read_only_hint=True, open_world_hint=True))
async def check_provenance(record_id: str) -> ProvenanceCheck:
    """Recompute a record's hash and compare it with the copy on Hedera.

    Also returns earlier records for the same product. "verified" means the record is unchanged
    since it was anchored. It does not mean the item is genuine.
    """
    try:
        return await get_service().check_provenance(record_id)
    except NotFound as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(
    title="Register product at origin", read_only_hint=False, destructive_hint=False,
    idempotent_hint=False, open_world_hint=True,
))
async def register_product(product: MakerProduct) -> Registration:
    """Register a maker's item so later resales can be checked against it.

    Muse Spark picks the item's distinctive marks from the photos and SAM 3.1 captures each one as
    the origin fingerprint. The registration hash is anchored on Hedera and, when configured, an
    NFT certificate is minted. Returns the registration_id, an origin page URL and a QR code URL.
    """
    return await get_service().register_product(product)


@mcp.tool(annotations=ToolAnnotations(title="Get registration", read_only_hint=True, open_world_hint=False))
async def get_registration(registration_id: str) -> Registration:
    """Look up an origin registration: maker, product details, fingerprint features and Hedera proof."""
    try:
        return get_service().get_registration(registration_id)
    except NotFound as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(title="Find products", read_only_hint=True, open_world_hint=True))
async def find_products(query: str, max_price_usd: float | None = None) -> dict:
    """Find checked products for a shopper: vendor agents pitch, and every offer is verified.

    GlobalChain's shopper agent broadcasts the need to the vendor agents, checks each pitch's
    signature and NFT on Hedera, runs verify_product and assess_seller_risk, and ranks the offers.
    Each result has a recommendation (recommended, caution, avoid), the reason, and a 0-100
    scorecard (authentic, origin, seller, value, delivery). Returns a session_id for
    buy_with_agent_wallet. Takes up to a minute.
    """
    return await agent_wallets.find_products(get_service(), query, max_price_usd)


@mcp.tool(annotations=ToolAnnotations(
    title="Create agent wallet", read_only_hint=False, destructive_hint=False,
    idempotent_hint=False, open_world_hint=True,
))
async def create_agent_wallet(label: str, spending_cap_usd: float) -> dict:
    """Create a demo wallet for an agent, funded with test USDC (tUSDC) up to spending_cap_usd (max 500).

    Returns wallet_key, shown once: pass it to buy_with_agent_wallet. Purchases from the wallet can
    never add up to more than the cap. Testnet or simulated funds only; no real money.
    """
    return await agent_wallets.create_agent_wallet(get_service(), label, spending_cap_usd)


@mcp.tool(annotations=ToolAnnotations(
    title="Buy with agent wallet", read_only_hint=False, destructive_hint=False,
    idempotent_hint=False, open_world_hint=True,
))
async def buy_with_agent_wallet(session_id: str, offer_id: str, wallet_key: str, max_price_usd: float,
                                recipient_account: str | None = None) -> dict:
    """Buy an offer from a find_products session, paying from an agent wallet.

    The offer is ordered under the same guardrail as the shopper's own agent: nothing from an
    unverified vendor, with a copied NFT, likely counterfeit or high risk. Refused over
    max_price_usd or the wallet's spending cap. Payment (tUSDC) and the item's ownership NFT move in
    one Hedera transaction; the NFT goes to recipient_account (the owner's Hedera account, which must
    accept the token) or stays in the agent wallet. Shopify store offers sell only on the store's
    own checkout and are refused here.
    """
    return await agent_wallets.buy_with_agent_wallet(get_service(), session_id, offer_id, wallet_key,
                                                     max_price_usd, recipient_account)


@mcp.tool(annotations=ToolAnnotations(title="Order status", read_only_hint=True, open_world_hint=True))
async def order_status(order_id: str) -> dict:
    """An order's status, its fulfilment steps (each anchored on Hedera) and who holds the item's NFT now."""
    try:
        return await agent_wallets.order_status(get_service(), order_id)
    except NotFound as exc:
        raise ValueError(str(exc)) from exc


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
