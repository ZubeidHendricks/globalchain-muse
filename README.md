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
- **Web**: `/` (scripted shopping agent), `/origin/{registration_id}` (public origin page the QR code opens)
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
  demo.py         scripted agent behind the web demo
  data/           brand reference data and demo listings
```

## Tests

```bash
uv run pytest
```

## Known gaps

- Brand reference data in `data/brands.json` is a demo set of general cues, not verified brand criteria.
- `product_key` follows a registered item across resales. Unregistered items are still grouped by listing.
- The origin page and its crops are public, so a counterfeiter can copy the photos. The comparison prompt
  says so, and price, listing text and seller risk still count.
- The demo photos are flat placeholder drawings, so SAM 3.1 finds no evidence regions in them and verdicts
  come back inconclusive. Replace them with real photos, including close-ups of each reference feature.
- If a Muse Spark call fails or its reply can't be parsed, the result is rule-based and the assessment sets
  `fallback_used: true`. The web demo and passport page show this, and it is part of the anchored record.
