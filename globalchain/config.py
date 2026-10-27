"""Runtime settings, read from the environment (and a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Meta Model API. With no key the service runs on the deterministic mock engine.
    model_api_key: str | None = None
    meta_api_base: str = "https://api.meta.ai/v1"
    muse_model: str = "muse-spark-1.3"
    # Optional per-task overrides of muse_model, e.g. Muse Wonder for verification only.
    verify_model: str | None = None
    seller_model: str | None = None
    agent_model: str | None = None
    sam_model: str = "sam-3.1"
    reasoning_effort: str = "medium"

    # Hedera. With no operator credentials, records are hashed and stored but not anchored.
    hedera_network: str = "testnet"
    operator_id: str | None = None
    operator_key: str | None = None
    hedera_topic_id: str | None = None
    # Origin registrations can also mint an HTS NFT certificate (needs the operator above).
    mint_certificates: bool = True
    hedera_nft_token_id: str | None = None

    database_path: str = "globalchain.db"
    public_base_url: str = "http://localhost:8000"

    # Listing prices may arrive in ZAR; brand reference prices are in USD.
    usd_zar_rate: float = 18.0

    max_images: int = 4
    http_timeout: float = 30.0

    @property
    def mock_mode(self) -> bool:
        return not self.model_api_key

    @property
    def mirror_base(self) -> str:
        if self.hedera_network == "mainnet":
            return "https://mainnet-public.mirrornode.hedera.com/api/v1"
        return f"https://{self.hedera_network}.mirrornode.hedera.com/api/v1"

    @property
    def hashscan_base(self) -> str:
        return f"https://hashscan.io/{self.hedera_network}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
