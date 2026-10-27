"""Request and response models shared by the REST API and the MCP tools."""

from typing import Literal

from pydantic import BaseModel, Field

Currency = Literal["USD", "ZAR"]


class Listing(BaseModel):
    """A marketplace listing as a shopping agent sees it."""

    title: str
    description: str = ""
    price: float = Field(ge=0)
    currency: Currency = "USD"
    image_urls: list[str] = Field(default_factory=list, description="Public URLs or data: URLs")
    url: str | None = None
    marketplace: str | None = None
    brand: str | None = Field(None, description="Claimed brand, e.g. 'Coach'")
    model: str | None = Field(None, description="Claimed model, e.g. 'Tabby 26'")


class Seller(BaseModel):
    seller_id: str
    name: str | None = None
    account_age_days: int | None = None
    rating: float | None = Field(None, ge=0, le=5)
    review_count: int | None = None
    recent_reviews: list[str] = Field(default_factory=list)
    active_listings: int | None = None
    ships_from: str | None = None
    payment_methods: list[str] = Field(default_factory=list)


class Box(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int
    frame_w: int
    frame_h: int


class EvidenceRegion(BaseModel):
    """A region SAM 3.1 segmented out of a listing photo."""

    id: str = Field(description="Stable id the reasoning cites, e.g. 'E3'")
    image_index: int
    feature: str = Field(description="Reference feature this region was segmented for")
    prompt: str = Field(description="Noun phrase sent to SAM 3.1")
    box: Box
    crop_sha256: str


class Finding(BaseModel):
    evidence_id: str
    observation: str
    supports: Literal["authentic", "counterfeit", "neutral"]


class ProductVerification(BaseModel):
    assessment_id: str
    brand: str | None
    model: str | None
    reference_id: str | None
    verdict: Literal["likely_authentic", "inconclusive", "likely_counterfeit"]
    authenticity_score: int = Field(ge=0, le=100)
    confidence: Literal["low", "medium", "high"]
    summary: str
    findings: list[Finding]
    missing_evidence: list[str]
    evidence: list[EvidenceRegion]
    engine: str = Field(description="'meta' for live Muse Spark + SAM 3.1, 'mock' for the offline engine")
    models: dict[str, str]
    fallback_used: bool = Field(False, description="True when Muse Spark's output was not used and the result is rule-based")


class RiskSignal(BaseModel):
    id: str = Field(description="Stable id the reasoning cites, e.g. 'S2'")
    name: str
    value: str
    weight: int = Field(description="Points this signal adds to the base risk score")
    detail: str


class SellerRisk(BaseModel):
    assessment_id: str
    seller_id: str
    risk_score: int = Field(ge=0, le=100)
    risk_level: Literal["low", "medium", "high"]
    base_score: int
    adjustment: int = Field(description="Muse Spark's bounded adjustment to the rule-based base score")
    explanation: str
    cited_signals: list[str]
    signals: list[RiskSignal]
    engine: str
    models: dict[str, str]
    fallback_used: bool = Field(False, description="True when Muse Spark's output was not used and the score is rule-based")


class VerifyProductRequest(BaseModel):
    listing: Listing


class SellerRiskRequest(BaseModel):
    listing: Listing
    seller: Seller


class IssueRecordRequest(BaseModel):
    product_assessment_id: str
    seller_assessment_id: str | None = None
    decision: Literal["approved", "skipped", "none"] = "none"


class Anchor(BaseModel):
    status: Literal["anchored", "unanchored", "failed"]
    network: str | None = None
    topic_id: str | None = None
    sequence_number: int | None = None
    transaction_id: str | None = None
    consensus_timestamp: str | None = None
    explorer_url: str | None = None
    detail: str | None = None


class VerificationRecord(BaseModel):
    record_id: str
    product_key: str
    record_hash: str
    record: dict
    anchor: Anchor
    passport_url: str


class ProvenanceCheck(BaseModel):
    record_id: str
    product_key: str
    status: Literal["verified", "hash_mismatch", "unanchored", "chain_unavailable"]
    record_hash: str
    recomputed_hash: str
    on_chain_hash: str | None
    anchor: Anchor
    history: list[dict] = Field(description="Earlier records for the same product key, oldest first")
    note: str = (
        "The chain proves what was checked and when. It does not prove the item is genuine."
    )
