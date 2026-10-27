"""MCP server exposing VeriChainX to any shopping agent.

Run over stdio (`globalchain-mcp`) for local agents, or use the streamable HTTP
endpoint the FastAPI app mounts at /mcp.

Tool classes follow the Muse Connector guidelines: three read tools and one write
(issue_verification_record, which writes to Hedera). None are destructive.
"""

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from .provenance import NotFound
from .schemas import Listing, ProductVerification, ProvenanceCheck, Seller, SellerRisk, VerificationRecord
from .service import get_service

INSTRUCTIONS = """VeriChainX is a trust check to run before checkout.
For each candidate listing: call verify_product and assess_seller_risk, show the shopper the
verdict, cited evidence and risk explanation, and let them decide. Once they approve or skip,
call issue_verification_record with both assessment ids and their decision.
Scores are evidence-based estimates, not guarantees. A Hedera anchor proves what was checked
and when, not that the item is genuine. Say so if the shopper asks."""

mcp = MCPServer(name="VeriChainX", instructions=INSTRUCTIONS, version="0.1.0")


@mcp.tool(annotations=ToolAnnotations(title="Verify product", read_only_hint=True, open_world_hint=True))
async def verify_product(listing: Listing) -> ProductVerification:
    """Check whether a listing's product is likely genuine.

    SAM 3.1 segments the regions that matter for the claimed brand (logos, labels, stitching,
    serial plates, packaging) and Muse Spark compares them with brand reference data. Every
    finding cites an evidence id. Returns an assessment_id to pass to issue_verification_record.
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


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
