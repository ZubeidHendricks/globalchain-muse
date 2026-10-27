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
      <p><span class="rec ${result.recommendation}">${REC[result.recommendation]}</span> <span class="muted">${esc(result.reason)}</span></p>
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
      <div class="actions">
        <button class="${result.recommendation === "avoid" ? "" : "primary"}" data-decision="approved">Approve purchase</button>
        <button class="${result.recommendation === "avoid" ? "primary" : ""}" data-decision="skipped">Skip</button>
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

$("#ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#go").disabled = true;
  $("#trace").innerHTML = "";
  $("#results").innerHTML = "";
  trace(`Searching listings for “${esc($("#query").value)}”`);
  trace(`<code>verify_product</code> + <code>assess_seller_risk</code> on each candidate`);
  try {
    const data = await api("/demo/shop", { query: $("#query").value });
    if (data.budget_usd) trace(`Budget ≈ $${data.budget_usd}; ${data.skipped_over_budget.length} listing(s) over budget skipped`);
    trace(`${data.results.length} candidate(s) checked; ranked by evidence and seller risk`);
    data.results.forEach((r) => $("#results").append(card(r)));
  } catch (err) {
    trace(`Failed: ${esc(err.message)}`);
  } finally {
    $("#go").disabled = false;
  }
});

api("/healthz").then((h) => {
  const tag = $("#engine");
  const m = h.models;
  const seller = m.seller !== m.verify ? ` · seller: ${m.seller}` : "";
  tag.textContent = h.engine === "mock" ? "Mock engine (no MODEL_API_KEY)" : `${m.verify} + ${m.segmentation}${seller}`;
  tag.classList.toggle("mock", h.engine === "mock");
});
