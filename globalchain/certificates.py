"""Origin certificates as Hedera Token Service NFTs.

Python port of AfriChain's backend/auth-service/src/services/hederaNftService.ts (token creation
and minting). One difference: HTS caps NFT metadata at 100 bytes, so the NFT carries a short
pointer to the registration and its hash ("gcm:<registration_id>:<hash prefix>") rather than
the full product JSON.
"""

import asyncio
import logging
from typing import Protocol

from .config import Settings
from .schemas import Certificate

log = logging.getLogger(__name__)

MAX_METADATA_BYTES = 100


def certificate_metadata(registration_id: str, registration_hash: str) -> bytes:
    meta = f"gcm:{registration_id}:{registration_hash[:32]}".encode()
    assert len(meta) <= MAX_METADATA_BYTES, meta
    return meta


class Minter(Protocol):
    async def mint(self, metadata: bytes) -> Certificate: ...


class NullMinter:
    async def mint(self, metadata: bytes) -> Certificate:
        return Certificate(status="not_minted", detail="Hedera operator credentials are not configured.")


class HederaMinter:
    """Mints one NFT per registration into a GlobalChain origin-certificate collection."""

    def __init__(self, settings: Settings):
        from hiero_sdk_python import AccountId, Client, Network, PrivateKey

        self.settings = settings
        self.client = Client(Network(settings.hedera_network))
        self.operator_id = AccountId.from_string(settings.operator_id)
        self.operator_key = PrivateKey.from_string(settings.operator_key)
        self.client.set_operator(self.operator_id, self.operator_key)
        self.token_id = settings.hedera_nft_token_id
        self._lock = asyncio.Lock()

    def _ensure_token(self) -> str:
        from hiero_sdk_python import SupplyType, TokenCreateTransaction, TokenType

        if self.token_id:
            return self.token_id
        receipt = (
            TokenCreateTransaction()
            .set_token_name("GlobalChain Origin Certificate")
            .set_token_symbol("GCMO")
            .set_token_type(TokenType.NON_FUNGIBLE_UNIQUE)
            .set_supply_type(SupplyType.INFINITE)
            .set_decimals(0)
            .set_initial_supply(0)
            .set_treasury_account_id(self.operator_id)
            .set_admin_key(self.operator_key.public_key())
            .set_supply_key(self.operator_key.public_key())
            .execute(self.client, validate_status=True)
        )
        self.token_id = str(receipt.token_id)
        log.warning("Created NFT collection %s; set HEDERA_NFT_TOKEN_ID to reuse it", self.token_id)
        return self.token_id

    def _mint(self, metadata: bytes) -> Certificate:
        from hiero_sdk_python import TokenId, TokenMintTransaction

        token = self._ensure_token()
        receipt = (
            TokenMintTransaction()
            .set_token_id(TokenId.from_string(token))
            .set_metadata([metadata])
            .execute(self.client, validate_status=True)
        )
        serial = receipt.serial_numbers[0]
        return Certificate(
            status="minted",
            network=self.settings.hedera_network,
            token_id=token,
            serial_number=serial,
            transaction_id=str(receipt.transaction_id) if receipt.transaction_id else None,
            explorer_url=f"{self.settings.hashscan_base}/token/{token}/{serial}",
        )

    async def mint(self, metadata: bytes) -> Certificate:
        async with self._lock:  # token creation is not concurrency-safe
            try:
                return await asyncio.to_thread(self._mint, metadata)
            except Exception as exc:
                log.exception("NFT mint failed")
                return Certificate(status="failed", network=self.settings.hedera_network, detail=str(exc))


def build_minter(settings: Settings) -> Minter:
    if settings.operator_id and settings.operator_key and settings.mint_certificates:
        return HederaMinter(settings)
    return NullMinter()
