// Vanilla JS, no build step — talks to the Flask API defined in app.py.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

// ---------------------------------------------------------------- tabs ----

$$(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    $$(".tab-btn").forEach(b => b.classList.remove("active"));
    $$(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "review") loadReviewList();
    if (btn.dataset.tab === "history") loadHistory();
    if (btn.dataset.tab === "aliases") loadAliases();
  });
});

// -------------------------------------------------------------- run now ---

$("#run-btn").addEventListener("click", async () => {
  const btn = $("#run-btn");
  const statusEl = $("#run-status");
  const eventsEl = $("#run-events");
  eventsEl.innerHTML = "";
  statusEl.textContent = "Running…";
  btn.disabled = true;

  const body = { force: $("#force-checkbox").checked };
  const single = $("#date-single").value;
  const since = $("#date-since").value;
  const until = $("#date-until").value;
  if (single) body.date = single;
  else {
    if (since) body.since = since;
    if (until) body.until = until;
  }

  try {
    const result = await api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const s = result.summary;
    statusEl.textContent =
      `Done — ${s.created} created, ${s.needs_review} need review, ${s.skipped} skipped, ${s.excluded} excluded, ${s.errors} errors.`;
    result.events.forEach(ev => {
      const li = document.createElement("li");
      li.className = ev.type;
      li.textContent = formatEvent(ev);
      eventsEl.appendChild(li);
    });
    loadReviewList();
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
});

function formatEvent(ev) {
  switch (ev.type) {
    case "excluded": return `Excluded — ${ev.subject}`;
    case "skipped": return `Skipped (${ev.reason}) — ${ev.subject}`;
    case "needs_review": return `Needs review (${ev.reason}) — ${ev.subject}`;
    case "created": return `Created quotation #${ev.order_id} for ${ev.partner} — ${ev.subject}`;
    case "error": return `Error — ${ev.subject}: ${ev.detail}`;
    default: return JSON.stringify(ev);
  }
}

// ----------------------------------------------------------- needs review -

async function loadReviewList() {
  const container = $("#review-list");
  container.innerHTML = "Loading…";
  const items = (await api("/api/items?status=needs_review")).filter(i => !i.resolved);
  if (!items.length) {
    container.innerHTML = "<p class='hint'>Nothing waiting on review.</p>";
    return;
  }
  container.innerHTML = "";
  items.forEach(item => container.appendChild(renderReviewItem(item)));
}

function renderReviewItem(item) {
  const div = document.createElement("div");
  div.className = "review-item";
  const ex = item.extraction || {};
  const candidates = (ex.customer_name_candidates || []).join(", ") || "(none extracted)";
  const lines = ex.lines || [];
  div.innerHTML = `
    <div class="subject">${escapeHtml(item.subject)}</div>
    <div class="meta">From ${escapeHtml(item.sender)} · ${escapeHtml(item.source_label)}</div>
    <span class="reason">${escapeHtml(item.reason || "")}</span>
    <div class="candidates">
      Candidates: ${escapeHtml(candidates)}<br>
      Branch text: ${escapeHtml(ex.branch_text || "—")} · PO: ${escapeHtml(ex.po_number || "—")}<br>
      Lines: ${lines.length} product line(s)
    </div>
    <button class="small fix-btn">Fix and create</button>
  `;
  $(".fix-btn", div).addEventListener("click", () => openResolveModal(item));
  return div;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --------------------------------------------------------- resolve modal --

let resolveState = null;

function openResolveModal(item) {
  const ex = item.extraction || {};
  resolveState = {
    itemId: item.id,
    partnerId: null,
    partnerName: "",
    poNumber: ex.po_number || "",
    branchText: ex.branch_text || "",
    lines: (ex.lines || []).map(l => ({ barcode: l.barcode, description: l.description, qty: l.qty, productId: null, productLabel: "" })),
  };
  renderResolveModal();
  $("#resolve-modal").classList.remove("hidden");
}

function renderResolveModal() {
  const body = $("#resolve-body");
  body.innerHTML = `
    <div class="field-row">
      <label>Odoo customer</label>
      <div class="search-wrap">
        <input type="text" id="rz-customer-search" placeholder="Search customer…" value="${escapeHtml(resolveState.partnerName)}">
        <div id="rz-customer-results" class="dropdown-results"></div>
      </div>
    </div>
    <div class="field-row">
      <label>PO number</label>
      <input type="text" id="rz-po" value="${escapeHtml(resolveState.poNumber)}" style="width:100%">
    </div>
    <div class="field-row">
      <label>Branch text (written into the order reference, same as before)</label>
      <input type="text" id="rz-branch" value="${escapeHtml(resolveState.branchText)}" style="width:100%">
    </div>
    <div class="field-row">
      <label>Product lines</label>
      <div id="rz-lines"></div>
      <button class="small secondary" id="rz-add-line" type="button">+ Add line</button>
    </div>
  `;

  const custInput = $("#rz-customer-search");
  custInput.addEventListener("input", debounce(async () => {
    const q = custInput.value.trim();
    if (!q) return;
    const results = await api(`/api/odoo/customers?q=${encodeURIComponent(q)}`);
    showDropdown($("#rz-customer-results"), results.map(r => ({ label: r.name, value: r })), r => {
      resolveState.partnerId = r.id;
      resolveState.partnerName = r.name;
      custInput.value = r.name;
    });
  }, 250));

  $("#rz-po").addEventListener("input", e => resolveState.poNumber = e.target.value);
  $("#rz-branch").addEventListener("input", e => resolveState.branchText = e.target.value);

  $("#rz-add-line").addEventListener("click", () => {
    resolveState.lines.push({ barcode: "", description: "", qty: 1, productId: null, productLabel: "" });
    renderLines();
  });

  renderLines();
}

function renderLines() {
  const container = $("#rz-lines");
  container.innerHTML = "";
  resolveState.lines.forEach((line, idx) => {
    const row = document.createElement("div");
    row.className = "line-row";
    row.innerHTML = `
      <div class="search-wrap">
        <input type="text" class="rz-product-search" placeholder="Search product / barcode…"
               value="${escapeHtml(line.productLabel || line.description || line.barcode || "")}">
        <div class="dropdown-results rz-product-results"></div>
      </div>
      <input type="number" class="rz-qty" min="0" step="1" value="${line.qty ?? 1}">
      <button class="small secondary rz-remove-line" type="button">✕</button>
    `;
    const searchInput = $(".rz-product-search", row);
    const resultsEl = $(".rz-product-results", row);

    // Pre-search using the barcode from extraction so the likely match shows immediately.
    if (line.barcode && !line.productId) {
      searchProducts(line.barcode).then(results => {
        if (results.length === 1) {
          line.productId = results[0].id;
          line.productLabel = `${results[0].name} (${results[0].barcode || "no barcode"})`;
          searchInput.value = line.productLabel;
        }
      });
    }

    searchInput.addEventListener("input", debounce(async () => {
      const q = searchInput.value.trim();
      if (!q) return;
      const results = await searchProducts(q);
      showDropdown(resultsEl, results.map(r => ({ label: `${r.name} (${r.barcode || "no barcode"})`, value: r })), r => {
        line.productId = r.id;
        line.productLabel = `${r.name} (${r.barcode || "no barcode"})`;
        searchInput.value = line.productLabel;
      });
    }, 250));

    $(".rz-qty", row).addEventListener("input", e => line.qty = parseFloat(e.target.value) || 0);
    $(".rz-remove-line", row).addEventListener("click", () => {
      resolveState.lines.splice(idx, 1);
      renderLines();
    });

    container.appendChild(row);
  });
}

function searchProducts(q) {
  return api(`/api/odoo/products?q=${encodeURIComponent(q)}`);
}

function showDropdown(el, items, onPick) {
  if (!items.length) { el.classList.remove("show"); return; }
  el.innerHTML = "";
  items.forEach(({ label, value }) => {
    const d = document.createElement("div");
    d.textContent = label;
    d.addEventListener("click", () => {
      onPick(value);
      el.classList.remove("show");
    });
    el.appendChild(d);
  });
  el.classList.add("show");
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

$("#resolve-cancel").addEventListener("click", () => {
  $("#resolve-modal").classList.add("hidden");
  resolveState = null;
});

$("#resolve-submit").addEventListener("click", async () => {
  if (!resolveState) return;
  if (!resolveState.partnerId) { alert("Pick a customer first."); return; }
  const lines = resolveState.lines
    .filter(l => l.productId && l.qty > 0)
    .map(l => ({ product_id: l.productId, qty: l.qty }));
  if (!lines.length) { alert("At least one product line needs a matched product and quantity."); return; }

  try {
    const result = await api(`/api/items/${resolveState.itemId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        partner_id: resolveState.partnerId,
        po_number: resolveState.poNumber,
        branch_text: resolveState.branchText,
        lines,
      }),
    });
    alert(`Created quotation id ${result.order_id}.`);
    $("#resolve-modal").classList.add("hidden");
    resolveState = null;
    loadReviewList();
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
});

// ------------------------------------------------------------- history ----

async function loadHistory() {
  const list = $("#history-list");
  list.innerHTML = "Loading…";
  const runs = await api("/api/runs");
  if (!runs.length) { list.innerHTML = "<p class='hint'>No runs yet.</p>"; return; }
  const table = document.createElement("table");
  table.className = "data-table";
  table.innerHTML = `<thead><tr><th>Started</th><th>Source</th><th>Window</th><th>Messages</th><th></th></tr></thead><tbody></tbody>`;
  const tbody = $("tbody", table);
  runs.forEach(r => {
    const tr = document.createElement("tr");
    const window = r.since_date ? `${r.since_date}${r.until_date ? " → " + r.until_date : ""}` : "rolling window";
    tr.innerHTML = `
      <td>${new Date(r.started_at).toLocaleString()}</td>
      <td>${r.source}</td>
      <td>${window}</td>
      <td>${r.message_count}</td>
      <td><button class="small secondary view-run-btn">View</button></td>
    `;
    $(".view-run-btn", tr).addEventListener("click", () => loadRunItems(r.id));
    tbody.appendChild(tr);
  });
  list.innerHTML = "";
  list.appendChild(table);
}

async function loadRunItems(runId) {
  const container = $("#history-items");
  container.innerHTML = "Loading…";
  const items = await api(`/api/runs/${runId}/items`);
  if (!items.length) { container.innerHTML = "<p class='hint'>No items in this run.</p>"; return; }
  const table = document.createElement("table");
  table.className = "data-table";
  table.innerHTML = `<thead><tr><th>Subject</th><th>Status</th><th>Detail</th></tr></thead><tbody></tbody>`;
  const tbody = $("tbody", table);
  items.forEach(i => {
    const tr = document.createElement("tr");
    const detail = i.status === "created"
      ? `Quotation #${i.order_id} — ${i.partner}`
      : (i.reason || i.error_detail || "");
    tr.innerHTML = `<td>${escapeHtml(i.subject)}</td><td>${i.status}</td><td>${escapeHtml(detail)}</td>`;
    tbody.appendChild(tr);
  });
  container.innerHTML = "";
  container.appendChild(table);
}

// ------------------------------------------------------------- aliases ----

async function loadAliases() {
  const tbody = $("#alias-table tbody");
  tbody.innerHTML = "";
  const aliases = await api("/api/aliases");
  Object.entries(aliases).forEach(([key, value]) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(key)}</td><td>${escapeHtml(value)}</td><td><button class="small secondary del-alias-btn">Remove</button></td>`;
    $(".del-alias-btn", tr).addEventListener("click", async () => {
      await api(`/api/aliases/${encodeURIComponent(key)}`, { method: "DELETE" });
      loadAliases();
    });
    tbody.appendChild(tr);
  });
}

$("#alias-customer-search").addEventListener("input", debounce(async () => {
  const q = $("#alias-customer-search").value.trim();
  if (!q) return;
  const results = await api(`/api/odoo/customers?q=${encodeURIComponent(q)}`);
  showDropdown($("#alias-customer-results"), results.map(r => ({ label: r.name, value: r })), r => {
    $("#alias-value").value = r.name;
    $("#alias-customer-search").value = r.name;
  });
}, 250));

$("#alias-add-btn").addEventListener("click", async () => {
  const key = $("#alias-key").value.trim();
  const value = $("#alias-value").value.trim();
  if (!key || !value) { alert("Enter the email text and pick an Odoo customer from the search results."); return; }
  await api("/api/aliases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, value }),
  });
  $("#alias-key").value = "";
  $("#alias-value").value = "";
  $("#alias-customer-search").value = "";
  loadAliases();
});

// -------------------------------------------------------------- startup ---

loadReviewList();
