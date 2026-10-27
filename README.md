# GlobalChain Muse: trust from maker to buyer, for agentic shopping

Shopping agents can find and buy almost anything, but they can't tell whether a listing, a seller or the
product itself should be trusted. GlobalChain links an item from the person who made it to every later
buyer, and gives agents the checks to run before checkout.

- **Makers register items at origin.** Muse Spark picks the marks that identify one specific item
  (hallmarks, signatures, weave or bead patterns, stitching, serial tags), and SAM 3.1 captures each one as
  the item's origin fingerprint. The registration is anchored on Hedera, minted as an NFT certificate, and
  printed as a QR code.
- **Buyers' agents check listings.** VeriChainX compares the listing photos with the item's own origin
  fingerprint (or brand reference data when there is none), scores the seller, and records the decision on
  Hedera.

Built for the Meta Global AI Developer Hackathon (theme: **Build for Your World**) with Muse Spark,
SAM 3.1 and Hedera. It builds on two earlier projects, VeriChainX and AfriChain;
[PRIOR_WORK.md](PRIOR_WORK.md) lists exactly what was imported and what was built during the hackathon.

| Tool | Class | What it does |
|---|---|---|
| `register_product` | Write | Maker side. Muse Spark chooses the item's distinctive features from the maker's photos; SAM 3.1 crops each one into the origin fingerprint (`R1`, `R2`, …). The registration hash is anchored on the Hedera Consensus Service and minted as an HTS NFT certificate, with an origin page and a QR code. |
| `get_registration` | Read | Returns a registration: maker, product details, fingerprint features and the Hedera proof. |
| `verify_product` | Read | SAM 3.1 segments the regions that matter for the claimed brand (logo hardware, labels, stitching, serial patches, packaging). Muse Spark compares the crops with brand reference data. If the listing carries a `registration_id` (from the item's QR code), the item's registered features are segmented instead and compared with its origin fingerprint; an id that does not exist counts against the listing. Every finding cites an evidence id (`E1`, `E2`, …), and findings that cite missing evidence are dropped. |
| `assess_seller_risk` | Read | Rule-based signals (`S1`, `S2`, …) set a base score from price against resale, account age, reviews, listing language and payment methods. Muse Spark reads the context and can adjust the score by at most ±15 points, citing the signals it used. |
| `issue_verification_record` | Write | Hashes the assessments (canonical JSON, SHA-256) and anchors **only the hash** on the Hedera Consensus Service. Records are only issued from assessments this service produced, and calls are idempotent. |
| `check_provenance` | Read | Recomputes the hash, fetches the HCS message from the mirror node and compares the two. Also returns the product passport history. |

The chain proves what was checked and when. It does not prove an item is genuine, so the scores stay
evidence-based.

## Run it

```bash
uv sync
uv run python scripts/make_demo_images.py   # placeholder photos; replace with real ones
uv run uvicorn globalchain.api:app --reload
```

Open http://localhost:8000 and ask: *"Find me an authentic designer handbag under $800"*.

- **No `MODEL_API_KEY`**: the mock engine runs. SAM boxes are pseudo-random and the verdict is
  rule-based (price and language only). The UI labels this mode.
- **No Hedera operator**: records and registrations are hashed and stored but marked `unanchored`, and no
  NFT certificate is minted.

Copy `.env.example` to `.env` to configure both.

## Surfaces

- **MCP, streamable HTTP**: `POST /mcp` on the same server.
- **MCP, stdio**: `uv run globalchain-mcp`. Example client config:
  ```json
  {"mcpServers": {"globalchain": {"command": "uv", "args": ["--directory", "/path/to/globalchain-muse", "run", "globalchain-mcp"]}}}
  ```
- **REST**: `POST /v1/registrations`, `GET /v1/registrations/{id}` (plus `/qr.png` and `/crops/{R1}.jpg`),
  `POST /v1/verify-product`, `POST /v1/seller-risk`, `POST /v1/records`, `GET /v1/records/{id}/provenance`,
  `GET /v1/passports/{product_key}`. OpenAPI docs at `/docs`.
- **Agents**: `POST /demo/agent` with `{"mode": "shop" | "make", "message": "...", "photos": [...],
  "session_id": ...}` runs the shopper's or maker's agent; pass `session_id` back to continue the chat.
  Orders: `POST /v1/orders/{id}/pay`, `POST /v1/orders/{id}/advance`, `GET /v1/orders/{id}`; also
  `GET /v1/wallet`, `GET /v1/vendors`, `GET /v1/conversations/{id}`.
- **Web**: `/` (shopping and maker agents), `/track/{order_id}`, `/origin/{registration_id}` (public origin page the QR code opens)
  and `/passport/{product_key}`.

## Layout

```
globalchain/
  engine.py       Meta Model API client (Muse Spark chat + SAM 3.1 responses) and the mock engine
  sam.py          SAM 3.1 output grammar parser and evidence cropping
  registry.py     register_product: Muse Spark feature choice, SAM 3.1 origin fingerprint, QR codes
  certificates.py HTS NFT origin certificates (port of AfriChain's hederaNftService.ts)
  verify.py       verify_product pipeline, including origin-fingerprint comparison
  seller.py       assess_seller_risk signals and bounded adjustment
  provenance.py   record hashing, HCS anchoring (hiero-sdk-python), mirror-node checks
  mcp_server.py   MCP tools (mcp 2.x MCPServer)
  api.py          FastAPI app; mounts MCP at /mcp
  agent.py        the shopper's and maker's Muse Spark agents (tool calling), sessions, scripted fallback
  vendors.py      vendor directory, vendor Muse agents, signed agent-to-agent messages, NFT ownership checks
  orders.py       prepare, pay (atomic swap), fulfil and track orders; events anchored on HCS
  ledger.py       Hedera wallets, tUSDC, NFT transfers and swaps (hiero-sdk-python), plus a simulated ledger
  data/vendors.json demo vendors and catalogues
  demo.py         demo marketplace data and the older scripted /demo/shop flow
  data/           brand reference data and demo listings
```

## Tests

```bash
uv run pytest
```

## Agent network

Your Muse agent doesn't search listings: it talks to vendors' Muse agents.

1. **Request.** The shopper's agent broadcasts the need and budget (`broadcast_request`) to every vendor
   agent on the network.
2. **Pitch.** Each vendor runs its own Muse Spark agent with its own persona and catalogue; it decides what
   to pitch and writes the pitch. Pitches are envelopes signed with the vendor's Hedera account key.
3. **Vet.** The shopper's agent checks each signature against the key Hedera holds for the vendor's
   account, checks who holds each item's NFT (a copied QR code shows up as an NFT in someone else's
   wallet), then runs `verify_product` (against the origin fingerprint) and `assess_seller_risk`. It can
   ask a vendor follow-up questions (`ask_vendor`), and ranks offers with `present_recommendations`. A
   guardrail stops it recommending or ordering anything from an unverified vendor, with a cloned NFT, or
   flagged counterfeit or high risk.
4. **Buy.** "Get me the Tabby, pay with crypto" makes it call `prepare_order`. Nothing is paid until the
   shopper clicks Confirm & pay (`POST /v1/orders/{id}/pay`). With crypto, the payment and the NFT move in
   **one atomic Hedera transaction** (tUSDC buyer → vendor, NFT vendor → buyer). Card, EFT and mobile money
   are simulated here; the NFT moves once payment is marked settled.
5. **Own, then ship.** The item is the shopper's before it ships. The vendor's agent prepares and ships
   (`POST /v1/orders/{id}/advance`), each step anchored on HCS against the NFT; `/track/{order_id}` shows
   the on-chain owner and the journey.

The maker's agent (`register_product`, `list_for_sale`) turns a maker into a verified vendor: a wallet,
the item's NFT in it, and a vendor agent that pitches it.

Vendor agents use `VENDOR_MODEL` and the shopper's agent `AGENT_MODEL` (both default to `MUSE_MODEL`).
With no Model API key, or if a Muse Spark turn fails, a scripted policy continues from the same state.

Hedera (testnet): wallets are custodial demo accounts (keys stored server-side, never returned by the
API; refused on mainnet). tUSDC is a test stablecoin GlobalChain creates (`HEDERA_PAYMENT_TOKEN_ID`); on
mainnet this would be USDC on Hedera. Without operator credentials, `SimulatedLedger` keeps the same state
in SQLite and every result says simulated.

## Known gaps

- Vendor agents run in this process. The envelopes are what a remote vendor agent would send over
  HTTP or A2A; the network endpoint for outside vendors isn't built yet.
- Demo wallets are custodial. A real deployment would have shoppers and vendors sign with their own
  wallets (HashPack, WalletConnect).

- Brand reference data in `data/brands.json` is a demo set of general cues, not verified brand criteria.
- `product_key` follows a registered item across resales. Unregistered items are still grouped by listing.
- The origin page and its crops are public, so a counterfeiter can copy the photos. The comparison prompt
  says so, and price, listing text and seller risk still count.
- The demo photos are flat placeholder drawings, so SAM 3.1 finds no evidence regions in them and verdicts
  come back inconclusive. Replace them with real photos, including close-ups of each reference feature.
- If a Muse Spark call fails or its reply can't be parsed, the result is rule-based and the assessment sets
  `fallback_used: true`. The web demo and passport page show this, and it is part of the anchored record.
