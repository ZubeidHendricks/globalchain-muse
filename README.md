# VeriChainX: a trust layer for agentic shopping

Shopping agents can find and buy almost anything, but they can't tell whether a listing, a seller or the
product itself should be trusted. VeriChainX is the check an agent calls before checkout.

Built for the Meta Global AI Developer Hackathon (theme: **Build for Your World**) with Muse Spark,
SAM 3.1 and Hedera.

| Tool | Class | What it does |
|---|---|---|
| `verify_product` | Read | SAM 3.1 segments the regions that matter for the claimed brand (logo hardware, labels, stitching, serial patches, packaging). Muse Spark compares the crops with brand reference data. Every finding cites an evidence id (`E1`, `E2`, …), and findings that cite missing evidence are dropped. |
| `assess_seller_risk` | Read | Rule-based signals (`S1`, `S2`, …) set a base score from price against resale, account age, reviews, listing language and payment methods. Muse Spark reads the context and can adjust the score by at most ±15 points, citing the signals it used. |
| `issue_verification_record` | Write | Hashes the assessments (canonical JSON, SHA-256) and anchors **only the hash** on the Hedera Consensus Service. Records are only issued from assessments this service produced, and calls are idempotent. |
| `check_provenance` | Read | Recomputes the hash, fetches the HCS message from the mirror node and compares the two. Also returns the product passport history. |

The chain proves what was checked and when. It does not prove an item is genuine, so the scores stay
evidence-based.

## Run it

```bash
uv sync
uv run python scripts/make_demo_images.py   # placeholder photos; replace with real ones
uv run uvicorn verichainx.api:app --reload
```

Open http://localhost:8000 and ask: *"Find me an authentic designer handbag under $800"*.

- **No `MODEL_API_KEY`**: the mock engine runs. SAM boxes are pseudo-random and the verdict is
  rule-based (price and language only). The UI labels this mode.
- **No Hedera operator**: records are hashed and stored but marked `unanchored`.

Copy `.env.example` to `.env` to configure both.

## Surfaces

- **MCP, streamable HTTP**: `POST /mcp` on the same server.
- **MCP, stdio**: `uv run verichainx-mcp`. Example client config:
  ```json
  {"mcpServers": {"verichainx": {"command": "uv", "args": ["--directory", "/path/to/verichainx-muse", "run", "verichainx-mcp"]}}}
  ```
- **REST**: `POST /v1/verify-product`, `POST /v1/seller-risk`, `POST /v1/records`,
  `GET /v1/records/{id}/provenance`, `GET /v1/passports/{product_key}`. OpenAPI docs at `/docs`.
- **Web demo**: `/` (scripted shopping agent) and `/passport/{product_key}`.

## Layout

```
verichainx/
  engine.py       Meta Model API client (Muse Spark chat + SAM 3.1 responses) and the mock engine
  sam.py          SAM 3.1 output grammar parser and evidence cropping
  verify.py       verify_product pipeline
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
- `product_key` groups records by listing. Reading a serial or date code from the evidence would make
  passports follow the item across resales.
- The demo photos are flat placeholder drawings, so SAM 3.1 finds no evidence regions in them and verdicts
  come back inconclusive. Replace them with real photos, including close-ups of each reference feature.
- If a Muse Spark call fails or its reply can't be parsed, the result is rule-based and the assessment sets
  `fallback_used: true`. The web demo and passport page show this, and it is part of the anchored record.
