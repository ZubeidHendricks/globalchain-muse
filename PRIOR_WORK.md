# Prior work and what was built during the hackathon

The Meta Global AI Developer Hackathon requires submissions to identify clearly what was built during
the event (building period Oct 27 – Nov 4, 2026). This file is that record. Git history backs it up:
the first commit of this repository is the import described below, and every later commit was made
during the building period.

## Imported (not built during the hackathon)

| Source | What came in | How |
|---|---|---|
| [verichainx-muse](https://github.com/ZubeidHendricks/verichainx-muse) at `623b7cb` (Oct 26, 2026) | The VeriChainX checks: `verify_product` (SAM 3.1 + Muse Spark), `assess_seller_risk`, Hedera HCS record anchoring, `check_provenance`, the MCP server, the REST API and the scripted web demo | Copied as the first commit of this repo, with the package renamed from `verichainx` to `globalchain` |
| [AfriChain](https://github.com/ZubeidHendricks/AfriChain) (Aug–Oct 2025, TypeScript) | Ideas and flows for maker product registration, Hedera NFT authenticity certificates, QR verification codes, and SMS/USSD access | Re-implemented in Python here; no AfriChain code is copied. Each ported module names the AfriChain file it follows |
| [verichainX-hedera](https://github.com/ZubeidHendricks/verichainX-hedera) (Hello Future hackathon, 2025) | The original VeriChainX idea: AI counterfeit detection with Hedera-anchored public product passports | Concept only |

## Built during the hackathon

Listed as it lands, newest last.

- **Oct 27 · Maker registration** (`registry.py`, `certificates.py`, `static/origin.html`,
  `register_product` and `get_registration` MCP tools, `/v1/registrations` REST endpoints). Muse Spark
  chooses an item's distinctive marks from the maker's photos; SAM 3.1 crops each one as the origin
  fingerprint. The registration hash is anchored on Hedera HCS and minted as an HTS NFT certificate
  (metadata kept under HTS's 100-byte limit), with a QR code that opens a public origin page.
- **Oct 27 · Origin comparison** (`verify.py`). A listing that carries a registration id is segmented for
  the features its maker registered, and Muse Spark compares each listing region (E1, E2, …) with the
  item's origin crop (R1, R2, …). An id that does not exist counts against the listing. Verification
  records include the registration, and passports group every resale of a registered item together.
- **Oct 27 · Muse Spark agents** (`agent.py`, `/demo/agent`, the web demo). A maker agent registers an
  item from its photos and lists it; a shopper agent searches, verifies every candidate (against the
  origin fingerprint when a listing claims one), checks sellers and ranks them through Muse Spark tool
  calling, with a guardrail on its recommendations and a scripted fallback. Replaces the scripted demo.
- **Oct 27 · Hedera testnet live**: registrations, NFT certificates and verification records running
  on testnet (topic `0.0.11188588`, certificate collection `0.0.11188589`).
- **Oct 27 · Agent-to-agent commerce** (`vendors.py`, `orders.py`, `ledger.py`, `/track/{order_id}`). The
  shopper's Muse agent broadcasts a need to vendors' Muse agents, which pitch from their own catalogues
  in signed envelopes; it checks signatures and NFT ownership on Hedera, verifies and ranks offers, and
  prepares the order the shopper asks for. Paying in tUSDC swaps payment and the item's NFT in one
  atomic Hedera transaction; the vendor's agent then ships, with each step anchored on HCS.
