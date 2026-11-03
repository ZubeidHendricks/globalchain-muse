const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const VERDICT = { likely_authentic: "Likely authentic", inconclusive: "Inconclusive", likely_counterfeit: "Likely counterfeit" };
const REC = { recommended: "Recommended", caution: "Proceed with caution", avoid: "Avoid" };

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) } : {});
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

function trace(html) {
  const li = document.createElement("li");
  li.innerHTML = html;
  $("#trace").append(li);
}

function scoreColor(v, invert) {
  const good = invert ? v < 30 : v >= 75;
  const bad = invert ? v >= 60 : v < 35;
  return good ? "var(--ok)" : bad ? "var(--bad)" : "var(--warn)";
}

function price(listing) {
  return listing.currency === "ZAR" ? `R${listing.price.toLocaleString()}` : `$${listing.price.toLocaleString()}`;
}

function photo(result, imageIndex) {
  const { listing, product } = result;
  const boxes = product.evidence.filter((e) => e.image_index === imageIndex);
  const fw = boxes[0]?.box.frame_w ?? 900, fh = boxes[0]?.box.frame_h ?? 900;
  const rects = boxes.map((e) => `
    <rect data-ev="${e.id}" x="${e.box.x1}" y="${e.box.y1}" width="${e.box.x2 - e.box.x1}" height="${e.box.y2 - e.box.y1}" rx="8"/>
    <text x="${e.box.x1 + 8}" y="${e.box.y1 + 38}">${e.id}</text>`).join("");
  return `<img src="${esc(listing.image_urls[imageIndex])}" alt="Listing photo ${imageIndex + 1}">
    <svg viewBox="0 0 ${fw} ${fh}" preserveAspectRatio="none">${rects}</svg>`;
}

// The 0-100 scorecard: computed from the checks, it ranks within a recommendation and never lifts one.
function scoreCard(sc) {
  const rows = sc.rows.map((r) => `
    <li><span class="sc-label">${esc(r.label)}</span>
      <span class="bar">${r.score === null ? "" : `<span style="width:${r.score}%;background:${scoreColor(r.score)}"></span>`}</span>
      <span class="sc-num">${r.score === null ? "–" : r.score}</span>
      <span class="muted sc-note">${esc(r.note)}</span></li>`).join("");
  return `<details class="scorecard">
    <summary><span class="seal" style="--c:${scoreColor(sc.overall)}">${sc.overall}</span>
      <span><strong>${esc(sc.grade)}</strong> <span class="muted">overall score · see how it's made up</span></span></summary>
    <ul>${rows}</ul></details>`;
}

function card(result) {
  const { listing, seller, product, seller_risk: risk } = result;
  const el = document.createElement("article");
  el.className = "card";
  const thumbs = listing.image_urls.length > 1
    ? `<div class="thumbs">${listing.image_urls.map((u, i) => `<img data-i="${i}" class="${i ? "" : "on"}" src="${esc(u)}" alt="">`).join("")}</div>` : "";
  const findings = product.findings.map((f) => `
    <li data-ev="${esc(f.evidence_id)}"><span class="chip ${esc(f.supports)}">${esc(f.evidence_id)}</span><span>${esc(f.observation)}</span></li>`).join("");
  const signals = risk.signals.filter((s) => s.weight).sort((a, b) => b.weight - a.weight).map((s) => `
    <li><span class="chip counterfeit">${esc(s.id)} +${s.weight}</span><span>${esc(s.detail)} <span class="muted">(${esc(s.value)})</span></span></li>`).join("")
    || `<li><span class="chip authentic">ok</span><span>No risk signals triggered.</span></li>`;
  const missing = product.missing_evidence.length
    ? `<h3>Not visible in photos</h3><div class="muted">${product.missing_evidence.map(esc).join(", ")}</div>` : "";

  el.innerHTML = `
    <div>
      <div class="photo">${photo(result, 0)}</div>
      ${thumbs}
    </div>
    <div>
      <div class="head">
        <div>
          <h2>${esc(listing.title)}</h2>
          <div class="muted">${esc(seller.name || seller.seller_id)} · ${esc(seller.ships_from || "")}</div>
        </div>
        <div style="text-align:right">
          <div class="price">${price(listing)}</div>
          ${listing.currency !== "USD" ? `<div class="muted">≈ $${result.price_usd}</div>` : ""}
        </div>
      </div>
      ${result.offer ? offerChecks(result.offer) : ""}
      <p><span class="rec ${result.recommendation}">${REC[result.recommendation]}</span> <span class="muted">${esc(result.reason)}</span></p>
      ${result.scorecard ? scoreCard(result.scorecard) : ""}
      <div class="scores">
        <div class="meter"><div class="label">Product · ${VERDICT[product.verdict]} · ${product.confidence} confidence</div>
          <div class="value">${product.authenticity_score}<span class="muted">/100</span></div>
          <div class="bar"><span style="width:${product.authenticity_score}%;background:${scoreColor(product.authenticity_score)}"></span></div></div>
        <div class="meter"><div class="label">Seller risk · ${risk.risk_level}</div>
          <div class="value">${risk.risk_score}<span class="muted">/100</span></div>
          <div class="bar"><span style="width:${risk.risk_score}%;background:${scoreColor(risk.risk_score, true)}"></span></div></div>
      </div>
      ${product.engine === "meta" && (product.fallback_used || risk.fallback_used)
        ? `<p><span class="tag mock">Rule-based fallback: Muse Spark output unavailable for this ${product.fallback_used ? "verification" : "seller check"}</span></p>` : ""}
      <h3>Evidence</h3>
      <p style="margin:0 0 6px">${esc(product.summary)}</p>
      <ul class="findings">${findings}</ul>
      ${missing}
      <h3>Seller</h3>
      <p style="margin:0 0 6px">${esc(risk.explanation)}</p>
      <ul class="findings">${signals}</ul>
      <div class="actions">${result.offer
        ? (result.status === "sold" ? `<span class="muted">Sold.</span>`
          : `<button class="${result.recommendation === "avoid" ? "" : "primary"}" data-get>Ask my agent to get this</button>`)
        : `<button class="${result.recommendation === "avoid" ? "" : "primary"}" data-decision="approved">Approve purchase</button>
        <button class="${result.recommendation === "avoid" ? "primary" : ""}" data-decision="skipped">Skip</button>`}
      </div>
      <div class="record" hidden></div>
    </div>`;

  // Hovering a finding highlights the SAM region it cites; thumbnails switch photos.
  el.addEventListener("mouseover", (e) => {
    const id = e.target.closest("[data-ev]")?.dataset.ev;
    el.querySelectorAll("rect").forEach((r) => r.classList.toggle("on", r.dataset.ev === id));
  });
  el.querySelectorAll(".thumbs img").forEach((t) => t.addEventListener("click", () => {
    $(".photo", el).innerHTML = photo(result, Number(t.dataset.i));
    el.querySelectorAll(".thumbs img").forEach((x) => x.classList.toggle("on", x === t));
  }));
  el.querySelectorAll("[data-decision]").forEach((b) => b.addEventListener("click", () => decide(el, result, b.dataset.decision)));
  el.querySelector("[data-get]")?.addEventListener("click", () => {
    $("#query").value = `Get me the ${result.listing.title} from ${result.offer.vendor}, pay with crypto`;
    $("#query").focus();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  return el;
}

async function decide(el, result, decision) {
  el.querySelectorAll("[data-decision]").forEach((b) => (b.disabled = true));
  const box = $(".record", el);
  box.hidden = false;
  box.textContent = "Issuing verification record…";
  trace(`<code>issue_verification_record</code> (${decision})`);
  try {
    const rec = await api("/v1/records", {
      product_assessment_id: result.product.assessment_id,
      seller_assessment_id: result.seller_risk.assessment_id,
      decision,
    });
    trace(`<code>check_provenance</code> ${esc(rec.record_id)}`);
    const prov = await api(`/v1/records/${rec.record_id}/provenance`);
    const a = rec.anchor;
    const chain = a.status === "anchored"
      ? `Anchored on Hedera ${esc(a.network)} · topic ${esc(a.topic_id)} #${a.sequence_number} · <a href="${esc(a.explorer_url)}" target="_blank" rel="noopener">HashScan</a>`
      : `Not anchored (${esc(a.detail || a.status)})`;
    box.innerHTML = `
      <strong>${decision === "approved" ? "Approved" : "Skipped"}.</strong> Verification record issued.
      <dl>
        <dt>Record</dt><dd class="mono">${esc(rec.record_id)}</dd>
        <dt>SHA-256</dt><dd class="mono">${esc(rec.record_hash)}</dd>
        <dt>Chain</dt><dd>${chain}</dd>
        <dt>Provenance check</dt><dd>${esc(prov.status)}</dd>
        <dt>Product passport</dt><dd><a href="${esc(new URL(rec.passport_url).pathname)}">${esc(rec.product_key)}</a></dd>
      </dl>`;
  } catch (err) {
    box.textContent = `Could not issue record: ${err.message}`;
  }
}

const PROMPTS = {
  shop: "I need a bag for everyday use, under $400",
  make: "I'm Lerato Dlamini from Johannesburg. Register my Coach Tabby 26 bag and list it for R5,200.",
};
const DEMO_PHOTOS = ["tabby-a-1", "tabby-a-2", "tabby-b-1", "tabby-b-2", "jetset-c-1", "tabby-d-1"].map((n) => `/static/demo/${n}.png`);
let mode = "shop";
const sessions = { shop: null, make: null };
const CHECK = {
  verified: ["authentic", "Verified vendor"], unverified: ["counterfeit", "Unverified vendor"],
  invalid_signature: ["counterfeit", "Bad signature"], shopify_store: ["neutral", "Shopify store (not a partner)"],
};
const RISK = { low: "authentic", medium: "neutral", high: "counterfeit" };

function insightChip(ins) {
  if (!ins) return "";
  const days = ins.delivery_days ? ` · ${ins.delivery_days[0]}–${ins.delivery_days[1]} days` : "";
  const why = ins.signals.map((s) => (typeof s === "string" ? s : `${s.id}: ${s.detail}`)).join("\n") || "No dropshipping signals";
  return `<span class="chip ${RISK[ins.dropship_risk] || "neutral"}" title="${esc(why)}">Dropship risk: ${esc(ins.dropship_risk)}${days}</span>`;
}
const NFT = {
  owned: ["authentic", "Holds the item's NFT"], cloned: ["counterfeit", "Copied QR: NFT belongs to someone else"],
  no_registration: ["neutral", "No origin record"], no_nft: ["neutral", "No NFT"], not_held: ["counterfeit", "NFT not held"],
};

function offerChecks(o) {
  const [vc, vl] = CHECK[o.vendor_check] || ["neutral", o.vendor_check];
  const [nc, nl] = NFT[o.nft] || ["neutral", o.nft];
  return `<div class="checks"><span class="chip ${vc}" title="${esc(o.vendor_detail)}">${vl}</span>
    <span class="chip ${nc}" title="${esc(o.nft_detail)}">${nl}</span>${insightChip(o.store_insights)}</div>
    <blockquote class="pitch">“${esc(o.pitch)}” <span class="muted">— ${esc(o.vendor)}'s agent</span></blockquote>`;
}

const KIND = { request: "asks everyone", pitch: "pitches", question: "asks", answer: "answers", order: "orders", status: "updates" };

function transcript(messages) {
  if (!messages.length) return "";
  const rows = messages.map((m) => {
    const b = m.body || {};
    const text = b.need ? `“${b.need}”${b.budget_usd ? ` · budget $${b.budget_usd}` : ""}`
      : b.offers ? (b.offers.length ? `${b.offers.length} offer(s). ${b.message || ""}` : (b.message || "nothing to offer"))
      : b.question || b.answer || b.message || (b.order_id ? `order ${b.order_id}` : "");
    const sig = m.from === "shopper" ? "" : m.signature
      ? `<span class="chip authentic" title="Signed by ${esc(m.signer)}">signed ${esc(m.signer)}</span>`
      : `<span class="chip counterfeit">unsigned</span>`;
    return `<li><span class="who">${esc(m.from_name || m.from)}</span>
      ${esc(KIND[m.type] || m.type)} ${sig}<div class="muted">${esc(text)}</div></li>`;
  }).join("");
  return `<details class="a2a" open><summary>Agent-to-agent messages (${messages.length})</summary><ul>${rows}</ul></details>`;
}

function shopifyOrderCard(o) {
  const el = document.createElement("article");
  el.className = "card order";
  el.style.gridTemplateColumns = "120px 1fr";
  el.innerHTML = `<div>${o.photo ? `<img src="${esc(o.photo)}" alt="" style="width:100%;border-radius:8px">` : ""}</div>
    <div><div class="head"><div><h2>${esc(o.title)}</h2><div class="muted">from ${esc(o.vendor_name)} · ${esc(o.payment_label)}</div></div>
      <div class="price">$${o.amount_usd.toLocaleString(undefined, { minimumFractionDigits: 2 })}</div></div>
      <p class="muted">Your agent built a cart on the store's own Shopify checkout. You pay there, with the store's payment methods. ${esc(o.ownership)}</p>
      <div class="actions"><a class="button primary" href="${esc(o.checkout.continue_url)}" target="_blank" rel="noopener">Continue to ${esc(o.vendor_name)} checkout</a></div></div>`;
  return el;
}

function orderCard(o) {
  if (o.payment_method === "shopify_checkout") return shopifyOrderCard(o);
  const el = document.createElement("article");
  el.className = "card order";
  el.style.gridTemplateColumns = "120px 1fr";
  el.innerHTML = `<div><img src="${esc(o.photo)}" alt="" style="width:100%;border-radius:8px"></div>
    <div><div class="head"><div><h2>${esc(o.title)}</h2><div class="muted">from ${esc(o.vendor_name)} · ${esc(o.payment_label)}</div></div>
      <div class="price">$${o.amount_usd.toLocaleString(undefined, { minimumFractionDigits: 2 })}</div></div>
      <p class="muted">Your agent prepared this order. Nothing is paid until you confirm. When you do, the item's NFT moves to your wallet before it ships.</p>
      <div class="actions"><button class="primary" data-pay>Confirm &amp; pay</button></div>
      <div class="record" hidden></div></div>`;
  $("[data-pay]", el).addEventListener("click", async (e) => {
    e.target.disabled = true;
    const box = $(".record", el);
    box.hidden = false;
    box.textContent = "Settling on Hedera…";
    try {
      const t = await api(`/v1/orders/${o.order_id}/pay`, {});
      box.innerHTML = `<strong>${t.owned_by_you ? "The item is yours." : "Paid."}</strong> ${esc(t.settlement.detail)}
        <dl><dt>NFT owner on ${t.simulated_ledger ? "the simulated ledger" : "Hedera"}</dt><dd class="mono">${esc(t.on_chain_owner)}</dd>
        ${t.settlement.explorer_url ? `<dt>Transaction</dt><dd><a href="${esc(t.settlement.explorer_url)}" target="_blank" rel="noopener">HashScan</a></dd>` : ""}
        <dt>Track it</dt><dd><a href="/track/${esc(o.order_id)}">/track/${esc(o.order_id)}</a></dd></dl>`;
      refreshWallet();
    } catch (err) {
      box.textContent = `Payment failed: ${err.message}`;
      e.target.disabled = false;
    }
  });
  return el;
}

async function refreshWallet() {
  try {
    const w = await api("/v1/wallet");
    const owned = w.owned.length ? ` · owns ${w.owned.length} item(s)` : "";
    $("#wallet").innerHTML = `Wallet <span class="mono">${esc(w.wallet.account_id)}</span> · ${w.balance_tusdc ?? "?"} tUSDC${owned}`;
    $("#wallet").hidden = false;
  } catch { /* the wallet appears once the network is seeded */ }
}
const picked = new Set(DEMO_PHOTOS.slice(0, 2));

function setMode(m) {
  mode = m;
  $("#reset").hidden = !sessions[m];
  document.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
  $("#query").value = PROMPTS[m];
  $("#picker").hidden = m !== "make";
}

function renderPicker() {
  $("#picker").innerHTML = `<span class="muted">Photos to attach:</span>` + DEMO_PHOTOS.map((u, i) =>
    `<label><input type="checkbox" value="${u}" ${picked.has(u) ? "checked" : ""}><img src="${u}" alt="Demo photo ${i + 1}"></label>`).join("");
  $("#picker").querySelectorAll("input").forEach((c) => c.addEventListener("change", () => {
    c.checked ? picked.add(c.value) : picked.delete(c.value);
  }));
}

function step(t) {
  const who = t.policy && t.policy !== "scripted" ? `<span class="tag">${esc(t.policy)}</span> ` : "";
  if (t.kind === "note") return `<span class="tag mock">fallback</span> ${esc(t.text)}`;
  if (t.kind === "think") return `${who}<em>${esc(t.text)}</em>`;
  const args = Object.entries(t.args || {}).filter(([k]) => k !== "items")
    .map(([k, v]) => `${esc(k)}=${esc(typeof v === "string" ? v : JSON.stringify(v))}`).join(", ");
  const out = t.error ? `<span class="bad">error: ${esc(t.error)}</span>` : esc(t.summary || "");
  return `${who}<code>${esc(t.tool)}</code>(${args}) → ${out}`;
}

function registrationCard(reg, listing) {
  const el = document.createElement("article");
  el.className = "card";
  el.style.gridTemplateColumns = "1fr";
  const a = reg.anchor, c = reg.certificate;
  const crops = reg.fingerprint.map((f) => `
    <figure class="crop"><img src="/v1/registrations/${esc(reg.registration_id)}/crops/${esc(f.id)}.jpg" alt="${esc(f.feature)}">
      <figcaption><span class="chip neutral">${esc(f.id)}</span> ${esc(f.feature)}<div class="muted">${esc(reg.rationale[f.feature] || "")}</div></figcaption></figure>`).join("");
  el.innerHTML = `<div>
    <div class="head"><div><h2>${esc(reg.product.title)}</h2>
      <div class="muted">Registered by ${esc(reg.product.maker_name)} · <a href="${esc(new URL(reg.origin_url).pathname)}">origin page</a></div></div>
      <img class="qr" src="${esc(new URL(reg.qr_url).pathname)}" alt="QR code" width="96" height="96"></div>
    <h3>Origin fingerprint</h3><div class="crops">${crops}</div>
    <dl class="record">
      <dt>Hedera record</dt><dd>${a.status === "anchored" ? `topic ${esc(a.topic_id)} #${a.sequence_number} · <a href="${esc(a.explorer_url)}" target="_blank" rel="noopener">HashScan</a>` : esc(a.detail || a.status)}</dd>
      <dt>NFT certificate</dt><dd>${c.status === "minted" ? `${esc(c.token_id)} #${c.serial_number} · <a href="${esc(c.explorer_url)}" target="_blank" rel="noopener">HashScan</a>` : esc(c.detail || c.status)}</dd>
      ${listing ? `<dt>Listed on the agent network</dt><dd>${esc(listing.listing_id)} · ${price(listing)} · NFT in your wallet <span class="mono">${esc(listing.wallet)}</span>. Your vendor agent now pitches it to shoppers' agents.</dd>` : ""}
    </dl></div>`;
  return el;
}

$("#ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#go").disabled = true;
  $("#trace").innerHTML = "";
  $("#results").innerHTML = "";
  $("#a2a")?.remove();
  $("#answer").hidden = true;
  trace(`${mode === "shop" ? "Shopper" : "Maker"} agent is working…`);
  try {
    const data = await api("/demo/agent", { mode, message: $("#query").value, photos: mode === "make" ? [...picked] : [],
                                            session_id: sessions[mode] });
    sessions[mode] = data.session_id;
    $("#reset").hidden = false;
    $("#trace").innerHTML = "";
    trace(`<span class="muted">${esc(data.engine === "mock" ? "Scripted policy (no MODEL_API_KEY)" : `Driven by ${data.model}`)} · ${data.tool_calls} tool call(s)</span>`);
    data.trace.forEach((t) => trace(step(t)));
    $("#answer").hidden = false;
    $("#answer").textContent = data.answer;
    if (data.conversation?.length) $("#trace").insertAdjacentHTML("afterend", `<div id="a2a">${transcript(data.conversation)}</div>`);
    data.orders.forEach((o) => $("#results").append(orderCard(o)));
    if (data.registration) $("#results").append(registrationCard(data.registration, data.listing));
    data.results.forEach((r) => {
      const el = card(r);
      if (r.overridden) $(".rec", el).insertAdjacentHTML("afterend", ` <span class="tag mock">guardrail: downgraded from “recommended”</span>`);
      $("#results").append(el);
    });
  } catch (err) {
    trace(`Failed: ${esc(err.message)}`);
  } finally {
    $("#go").disabled = false;
    refreshWallet();
  }
});

$("#reset").addEventListener("click", () => {
  sessions[mode] = null;
  setMode(mode);
  $("#trace").innerHTML = "";
  $("#results").innerHTML = "";
  $("#a2a")?.remove();
  $("#answer").hidden = true;
  $("#reset").hidden = true;
});

document.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
renderPicker();
setMode("shop");
refreshWallet();

api("/healthz").then((h) => {
  const tag = $("#engine");
  const m = h.models;
  const seller = m.seller !== m.verify ? ` · seller: ${m.seller}` : "";
  tag.textContent = h.engine === "mock" ? "Mock engine (no MODEL_API_KEY)" : `${m.verify} + ${m.segmentation}${seller}`;
  tag.classList.toggle("mock", h.engine === "mock");
});
