"""Wallets, payments and NFT ownership for agent-to-agent commerce.

Every item for sale is an HTS NFT held in its vendor's wallet. A crypto purchase is one atomic
Hedera transfer: the buyer's stablecoin goes to the vendor and the NFT goes to the buyer in the
same transaction, so neither side can end up with both or neither. Off-chain payment rails (card,
EFT, mobile money) are simulated in this demo; the NFT moves once the payment is marked settled.

The crypto rail uses tUSDC, a test stablecoin GlobalChain creates on testnet, because test HBAR
cannot cover real item prices. On mainnet it would be USDC on Hedera.

Wallets are custodial (the service holds their keys) and testnet-only. HederaLedger refuses to
create them on mainnet. SimulatedLedger keeps the same state in SQLite for offline runs and tests.
"""

import asyncio
import hashlib
import logging

import httpx
from typing import Protocol

from pydantic import BaseModel

from .config import Settings
from .store import Store, new_id

log = logging.getLogger(__name__)

TOKEN_DECIMALS = 2  # tUSDC cents


class Wallet(BaseModel):
    account_id: str
    label: str
    public_key: str
    network: str
    simulated: bool
    explorer_url: str | None = None


class Tx(BaseModel):
    status: str  # "success" | "failed"
    transaction_id: str | None = None
    explorer_url: str | None = None
    simulated: bool
    detail: str | None = None


def nft_ref(token_id: str, serial: int) -> str:
    return f"{token_id}/{serial}"


class Ledger(Protocol):
    simulated: bool
    payment_token: str

    async def create_wallet(self, label: str, fund_usd: float = 0) -> Wallet: ...
    async def mint_item(self, metadata: bytes, to_account: str, certificate: dict | None) -> tuple[str, int, Tx]: ...
    async def swap(self, token_id: str, serial: int, seller: str, buyer: str, amount_usd: float,
                   receiver: str | None = None) -> Tx: ...
    async def transfer_nft(self, token_id: str, serial: int, sender: str, receiver: str) -> Tx: ...
    async def nft_owner(self, token_id: str, serial: int) -> str | None: ...
    async def public_key(self, account_id: str) -> str | None: ...
    async def balance_usd(self, account_id: str) -> float | None: ...
    def sign(self, account_id: str, data: bytes) -> str: ...


def verify_signature(public_key_hex: str, signature_hex: str, data: bytes) -> bool:
    from hiero_sdk_python import PublicKey

    try:
        # Every GlobalChain wallet key is ECDSA, so parse it as one (raw hex is ambiguous otherwise).
        PublicKey.from_string_ecdsa(public_key_hex).verify(bytes.fromhex(signature_hex), data)
        return True
    except Exception:  # bad key, bad hex or a signature that does not match
        return False


class _Keys:
    """Custodial key storage. Keys never leave this class except as signatures."""

    def __init__(self, store: Store):
        self.store = store

    def save(self, account_id: str, key) -> None:
        self.store.put("wallet_key", account_id, {"der": key.to_string_der()})

    def load(self, account_id: str):
        from hiero_sdk_python import PrivateKey

        doc = self.store.get("wallet_key", account_id)
        if not doc:
            raise LookupError(f"No custodial key for {account_id}")
        return PrivateKey.from_string(doc["der"])

    def sign(self, account_id: str, data: bytes) -> str:
        return self.load(account_id).sign(data).hex()


class SimulatedLedger:
    """Offline ledger with the same behaviour, kept in SQLite. Every result says simulated."""

    simulated = True
    payment_token = "sim-tusdc"

    def __init__(self, store: Store):
        self.store = store
        self.keys = _Keys(store)

    def _tx(self, detail: str) -> Tx:
        return Tx(status="success", transaction_id=new_id("simtx"), simulated=True, detail=detail)

    async def create_wallet(self, label: str, fund_usd: float = 0) -> Wallet:
        from hiero_sdk_python import PrivateKey

        key = PrivateKey.generate_ecdsa()
        account = "sim-" + hashlib.sha256(key.public_key().to_string_raw().encode()).hexdigest()[:10]
        self.keys.save(account, key)
        self.store.put("sim_balance", account, {"cents": round(fund_usd * 100)})
        return Wallet(account_id=account, label=label, public_key=key.public_key().to_string_raw(),
                      network="simulated", simulated=True)

    async def mint_item(self, metadata, to_account, certificate=None):
        if certificate and certificate.get("status") == "minted":
            token, serial = certificate["token_id"], certificate["serial_number"]
        else:
            n = len(self.store.all("sim_nft")) + 1
            token, serial = "sim-gcmo", n
        self.store.put("sim_nft", nft_ref(token, serial), {"owner": to_account})
        return token, serial, self._tx(f"NFT {nft_ref(token, serial)} issued to {to_account}")

    async def swap(self, token_id, serial, seller, buyer, amount_usd, receiver=None):
        receiver = receiver or buyer
        cents = round(amount_usd * 100)
        nft = self.store.get("sim_nft", nft_ref(token_id, serial))
        if not nft or nft["owner"] != seller:
            return Tx(status="failed", simulated=True, detail="Seller does not hold this NFT")
        bal = self.store.get("sim_balance", buyer) or {"cents": 0}
        if bal["cents"] < cents:
            return Tx(status="failed", simulated=True, detail="INSUFFICIENT_TOKEN_BALANCE")
        seller_bal = self.store.get("sim_balance", seller) or {"cents": 0}
        self.store.put("sim_balance", buyer, {"cents": bal["cents"] - cents})
        self.store.put("sim_balance", seller, {"cents": seller_bal["cents"] + cents})
        self.store.put("sim_nft", nft_ref(token_id, serial), {"owner": receiver})
        return self._tx(f"{amount_usd:.2f} tUSDC {buyer} -> {seller}; NFT {seller} -> {receiver}")

    async def transfer_nft(self, token_id, serial, sender, receiver):
        nft = self.store.get("sim_nft", nft_ref(token_id, serial))
        if not nft or nft["owner"] != sender:
            return Tx(status="failed", simulated=True, detail="Sender does not hold this NFT")
        self.store.put("sim_nft", nft_ref(token_id, serial), {"owner": receiver})
        return self._tx(f"NFT {sender} -> {receiver}")

    async def nft_owner(self, token_id, serial):
        nft = self.store.get("sim_nft", nft_ref(token_id, serial))
        return nft["owner"] if nft else None

    async def public_key(self, account_id):
        try:
            return self.keys.load(account_id).public_key().to_string_raw()
        except LookupError:
            return None

    async def balance_usd(self, account_id):
        bal = self.store.get("sim_balance", account_id)
        return bal["cents"] / 100 if bal else None

    def sign(self, account_id, data):
        return self.keys.sign(account_id, data)


class HederaLedger:
    """The same operations on Hedera. The operator pays fees and is treasury for both tokens."""

    simulated = False

    def __init__(self, settings: Settings, store: Store):
        from hiero_sdk_python import AccountId, Client, Network, PrivateKey

        if settings.hedera_network == "mainnet":
            raise ValueError("Custodial demo wallets are testnet-only")
        self.settings = settings
        self.store = store
        self.keys = _Keys(store)
        self.client = Client(Network(settings.hedera_network))
        self.operator_id = AccountId.from_string(settings.operator_id)
        self.operator_key = PrivateKey.from_string(settings.operator_key)
        self.client.set_operator(self.operator_id, self.operator_key)
        self.payment_token = settings.hedera_payment_token_id or ""
        self._lock = asyncio.Lock()

    def _explorer(self, tx_id) -> str | None:
        return f"{self.settings.hashscan_base}/transaction/{tx_id}" if tx_id else None

    def _ok(self, receipt, detail: str) -> Tx:
        tx_id = str(receipt.transaction_id) if getattr(receipt, "transaction_id", None) else None
        return Tx(status="success", transaction_id=tx_id, explorer_url=self._explorer(tx_id), simulated=False,
                  detail=detail)

    async def _run(self, fn, *args) -> Tx:
        async with self._lock:  # one transaction at a time from the operator account
            try:
                return await asyncio.to_thread(fn, *args)
            except Exception as exc:
                log.exception("Hedera transaction failed")
                return Tx(status="failed", simulated=False, detail=str(exc))

    def _ensure_payment_token(self) -> str:
        from hiero_sdk_python import SupplyType, TokenCreateTransaction, TokenType

        if self.payment_token:
            return self.payment_token
        receipt = (
            TokenCreateTransaction()
            .set_token_name("GlobalChain Test USD Coin")
            .set_token_symbol("tUSDC")
            .set_token_type(TokenType.FUNGIBLE_COMMON)
            .set_supply_type(SupplyType.INFINITE)
            .set_decimals(TOKEN_DECIMALS)
            .set_initial_supply(100_000_000 * 10**TOKEN_DECIMALS)
            .set_treasury_account_id(self.operator_id)
            .set_admin_key(self.operator_key.public_key())
            .set_supply_key(self.operator_key.public_key())
            .execute(self.client, validate_status=True)
        )
        self.payment_token = str(receipt.token_id)
        log.warning("Created payment token %s; set HEDERA_PAYMENT_TOKEN_ID to reuse it", self.payment_token)
        return self.payment_token

    def _create_wallet(self, label: str, fund_usd: float) -> Wallet:
        from hiero_sdk_python import AccountCreateTransaction, AccountId, Hbar, PrivateKey, TokenId, TransferTransaction

        key = PrivateKey.generate_ecdsa()
        receipt = (
            AccountCreateTransaction()
            .set_key_without_alias(key.public_key())
            .set_initial_balance(Hbar(1))
            .set_max_automatic_token_associations(20)  # receive NFTs and tUSDC without a separate association
            .set_account_memo(f"GlobalChain demo wallet: {label}"[:100])
            .execute(self.client, validate_status=True)
        )
        account = str(receipt.account_id)
        self.keys.save(account, key)
        if fund_usd > 0:
            token = TokenId.from_string(self._ensure_payment_token())
            cents = round(fund_usd * 10**TOKEN_DECIMALS)
            (TransferTransaction()
             .add_token_transfer(token, self.operator_id, -cents)
             .add_token_transfer(token, AccountId.from_string(account), cents)
             .execute(self.client, validate_status=True))
        return Wallet(account_id=account, label=label, public_key=key.public_key().to_string_raw(),
                      network=self.settings.hedera_network, simulated=False,
                      explorer_url=f"{self.settings.hashscan_base}/account/{account}")

    async def create_wallet(self, label: str, fund_usd: float = 0) -> Wallet:
        async with self._lock:
            return await asyncio.to_thread(self._create_wallet, label, fund_usd)

    def _send_nft(self, token_id: str, serial: int, sender: str, receiver: str, signer=None):
        from hiero_sdk_python import AccountId, NftId, TokenId, TransferTransaction

        tx = TransferTransaction().add_nft_transfer(
            NftId(TokenId.from_string(token_id), serial), AccountId.from_string(sender), AccountId.from_string(receiver))
        if signer is not None:
            tx.freeze_with(self.client)
            tx.sign(signer)
        return tx.execute(self.client, validate_status=True)

    async def mint_item(self, metadata, to_account, certificate=None):
        """Use the registration's certificate NFT (minted into the treasury) and hand it to the vendor."""
        if not certificate or certificate.get("status") != "minted":
            return "", 0, Tx(status="failed", simulated=False, detail="Registration has no minted certificate")
        token, serial = certificate["token_id"], certificate["serial_number"]
        tx = await self._run(lambda: self._ok(self._send_nft(token, serial, str(self.operator_id), to_account),
                                              f"NFT {nft_ref(token, serial)} to vendor {to_account}"))
        return token, serial, tx

    def _swap(self, token_id, serial, seller, buyer, amount_usd, receiver=None) -> Tx:
        from hiero_sdk_python import AccountId, NftId, TokenId, TransferTransaction

        receiver = receiver or buyer
        pay = TokenId.from_string(self._ensure_payment_token())
        cents = round(amount_usd * 10**TOKEN_DECIMALS)
        tx = (TransferTransaction()
              .add_token_transfer(pay, AccountId.from_string(buyer), -cents)
              .add_token_transfer(pay, AccountId.from_string(seller), cents)
              .add_nft_transfer(NftId(TokenId.from_string(token_id), serial),
                                AccountId.from_string(seller), AccountId.from_string(receiver))
              .set_transaction_memo("GlobalChain purchase: payment and ownership in one transaction"))
        tx.freeze_with(self.client)
        tx.sign(self.keys.load(buyer))   # authorises the payment
        tx.sign(self.keys.load(seller))  # authorises handing over the NFT
        receipt = tx.execute(self.client, validate_status=True)
        return self._ok(receipt, f"{amount_usd:.2f} tUSDC {buyer} -> {seller}; NFT {seller} -> {receiver}")

    async def swap(self, token_id, serial, seller, buyer, amount_usd, receiver=None):
        return await self._run(self._swap, token_id, serial, seller, buyer, amount_usd, receiver)

    async def transfer_nft(self, token_id, serial, sender, receiver):
        return await self._run(lambda: self._ok(
            self._send_nft(token_id, serial, sender, receiver, signer=self.keys.load(sender)),
            f"NFT {sender} -> {receiver}"))

    def _owner(self, token_id, serial) -> str | None:
        from hiero_sdk_python import NftId, TokenId, TokenNftInfoQuery

        info = TokenNftInfoQuery().set_nft_id(NftId(TokenId.from_string(token_id), serial)).execute(self.client)
        info = info[0] if isinstance(info, list) else info
        return str(info.account_id) if info and info.account_id else None

    async def _mirror(self, path: str) -> dict:
        async with httpx.AsyncClient(timeout=self.settings.http_timeout) as http:
            resp = await http.get(self.settings.mirror_base + path)
            resp.raise_for_status()
            return resp.json()

    async def nft_owner(self, token_id, serial):
        # A consensus-node query sees a transfer immediately; the mirror node lags a few seconds.
        try:
            return await asyncio.to_thread(self._owner, token_id, serial)
        except Exception as exc:
            log.warning("NFT owner query failed for %s (%s); asking the mirror node", nft_ref(token_id, serial), exc)
        try:
            return (await self._mirror(f"/tokens/{token_id}/nfts/{serial}")).get("account_id")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Mirror NFT lookup failed for %s: %s", nft_ref(token_id, serial), exc)
            return None

    def _key(self, account_id) -> str | None:
        from hiero_sdk_python import AccountId, AccountInfoQuery

        info = AccountInfoQuery().set_account_id(AccountId.from_string(account_id)).execute(self.client)
        return info.key.to_string_raw() if info and info.key else None

    async def public_key(self, account_id):
        """The key Hedera holds for the account: what a pitch signature is checked against."""
        try:
            key = (await self._mirror(f"/accounts/{account_id}")).get("key") or {}
            if key.get("key"):
                return key["key"]
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Mirror key lookup failed for %s (%s); querying a node", account_id, exc)
        try:
            return await asyncio.to_thread(self._key, account_id)
        except Exception as exc:
            log.warning("Account key lookup failed for %s: %s", account_id, exc)
            return None

    def _balance(self, account_id) -> float | None:
        from hiero_sdk_python import AccountId, CryptoGetAccountBalanceQuery, TokenId

        bal = CryptoGetAccountBalanceQuery().set_account_id(AccountId.from_string(account_id)).execute(self.client)
        cents = (bal.token_balances or {}).get(TokenId.from_string(self._ensure_payment_token()), 0)
        return cents / 10**TOKEN_DECIMALS

    async def balance_usd(self, account_id):
        token = self._ensure_payment_token()
        try:
            rows = (await self._mirror(f"/accounts/{account_id}/tokens?token.id={token}")).get("tokens") or []
            return rows[0]["balance"] / 10**TOKEN_DECIMALS if rows else 0.0
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("Mirror balance lookup failed for %s (%s); querying a node", account_id, exc)
        try:
            return await asyncio.to_thread(self._balance, account_id)
        except Exception as exc:
            log.warning("Balance lookup failed for %s: %s", account_id, exc)
            return None

    def sign(self, account_id, data):
        return self.keys.sign(account_id, data)


def build_ledger(settings: Settings, store: Store) -> Ledger:
    if settings.operator_id and settings.operator_key and settings.mint_certificates:
        return HederaLedger(settings, store)
    return SimulatedLedger(store)
