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

- *(in progress)* Maker registration: Muse Spark chooses an item's distinctive marks, SAM 3.1 captures
  them as an origin fingerprint, and the registration is anchored on Hedera with a QR code to a
  public origin page.
- *(in progress)* Origin comparison: when a listing carries a registration id, Muse Spark compares the
  listing photos against the item's own registration photos instead of generic brand data.
