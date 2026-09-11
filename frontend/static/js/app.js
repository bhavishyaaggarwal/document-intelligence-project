const $ = (id) => document.getElementById(id);
const statusEl = $("status");

function setStatus(message, type="") {
  statusEl.textContent = message;
  statusEl.className = `status ${type}`;
}

async function api(path, options={}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body?.detail?.error?.message || body?.error?.message || "Request failed";
    throw new Error(message);
  }
  return body;
}

async function loadDocuments() {
  const items = await api("/api/v1/documents");
  const host = $("documents");
  if (!items.length) {
    host.innerHTML = '<p class="muted">No processed documents yet.</p>';
    return;
  }
  // Do not use inline onclick handlers here. A filename such as
  // `Invoice "March".pdf` would break the HTML attribute because the
  // JSON string is inserted inside a quoted onclick attribute. Use a
  // data attribute + delegated click handler instead.
  host.innerHTML = `<table><thead><tr><th>Document</th><th>Type</th><th>Status</th><th>Processed</th><th></th></tr></thead><tbody>
    ${items.map(d => `<tr>
      <td>${escapeHtml(d.document_name)}</td>
      <td>${escapeHtml(d.document_type)}</td>
      <td><span class="badge ${d.processing_status !== "PASS" ? "fail" : ""}">${escapeHtml(d.processing_status)}</span></td>
      <td>${new Date(d.processing_metadata.processed_at).toLocaleString()}</td>
      <td><button type="button" class="link-btn open-result-btn" data-document-name="${escapeHtml(d.document_name)}">Open</button></td>
    </tr>`).join("")}
  </tbody></table>`;

  host.onclick = (event) => {
    const button = event.target.closest(".open-result-btn");
    if (!button || !host.contains(button)) return;
    openResult(button.dataset.documentName);
  };
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#039;" }[c]));
}

function fieldHtml(key, field) {
  const value = field?.value;
  const shown = value === null || value === undefined || value === "" ? '<span class="value-null">Missing / unreadable</span>' : escapeHtml(typeof value === "object" ? JSON.stringify(value) : value);
  const ev = field?.evidence;
  return `<div>${escapeHtml(key)}</div><div>${shown}${ev ? `<div class="evidence">${escapeHtml(ev.source_text || "")}${ev.page_number ? ` · page ${ev.page_number}` : ""}</div>` : ""}</div>`;
}

function renderResult(d) {
  const extracted = d.extracted_data || {};
  const fields = extracted.fields || {};
  let html = `<div class="kv">${Object.entries(fields).map(([k,v]) => fieldHtml(k,v)).join("")}</div>`;

  if (extracted.header && Object.keys(extracted.header).length) {
    html = `<h3>Header</h3><div class="kv">${Object.entries(extracted.header).map(([k,v]) => fieldHtml(k,{value:v})).join("")}</div>` + html;
  }

  if ((extracted.line_items || []).length) {
    const rows = extracted.line_items;
    html += `<h3>Invoice line items</h3><div class="table-wrap"><table><thead><tr><th>Description</th><th>Qty</th><th>Unit price</th><th>Amount</th></tr></thead><tbody>
      ${rows.map(x => `<tr><td>${escapeHtml(x.description)}</td><td>${escapeHtml(x.quantity)}</td><td>${escapeHtml(x.unit_price)}</td><td>${escapeHtml(x.amount)}</td></tr>`).join("")}
    </tbody></table></div>`;
  }

  if ((extracted.financial_line_items || []).length) {
    const rows = extracted.financial_line_items;
    const periods = [...new Set(rows.flatMap(x => Object.keys(x.values || {})))];
    html += `<h3>Financial line items</h3><div class="table-wrap"><table><thead><tr><th>Line item</th>${periods.map(p=>`<th>${escapeHtml(p)}</th>`).join("")}</tr></thead><tbody>
      ${rows.map(x => `<tr><td>${escapeHtml(x.label)}</td>${periods.map(p=>`<td>${x.values?.[p] == null ? '<span class="value-null">—</span>' : escapeHtml(x.values[p])}</td>`).join("")}</tr>`).join("")}
    </tbody></table></div>`;
  }

  const checks = d.validation?.checks || [];
  html += `<h3>Financial validation</h3><div class="table-wrap"><table><thead><tr><th>Check</th><th>Formula</th><th>Calculated</th><th>Reported</th><th>Variance</th><th>Status</th></tr></thead><tbody>
    ${checks.map(c => `<tr class="${c.status === "FAIL" ? "validation-fail" : c.status === "PASS" ? "validation-pass" : ""}">
      <td>${escapeHtml(c.name)}${c.period ? `<div class="evidence">${escapeHtml(c.period)}</div>` : ""}</td>
      <td>${escapeHtml(c.formula)}</td><td>${escapeHtml(c.calculated_value)}</td><td>${escapeHtml(c.reported_value)}</td><td>${escapeHtml(c.variance)}</td>
      <td><span class="badge ${c.status !== "PASS" ? "fail" : ""}">${escapeHtml(c.status)}</span></td>
    </tr>`).join("")}
  </tbody></table></div>`;

  html += `<details><summary>Raw JSON</summary><pre>${escapeHtml(JSON.stringify(d, null, 2))}</pre></details>`;
  $("result").innerHTML = html;
  $("resultTitle").textContent = d.document_name;
  $("resultMeta").textContent = `${d.document_type} · ${d.processing_status} · ${d.processing_metadata.extraction_method}`;
  $("resultCard").classList.remove("hidden");
  window.scrollTo({top: document.body.scrollHeight, behavior:"smooth"});
}

async function openResult(name) {
  try { renderResult(await api(`/api/v1/documents/${encodeURIComponent(name)}`)); }
  catch (e) { setStatus(e.message, "error"); }
}
window.openResult = openResult;

$("uploadForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("fileInput").files[0];
  if (!file) return;
  const btn = $("processBtn");
  btn.disabled = true;
  setStatus("Processing document…");
  const form = new FormData();
  form.append("file", file);
  form.append("document_type", $("documentType").value);
  try {
    const result = await api("/api/v1/documents/process", {method:"POST", body:form});
    setStatus(`Processed ${result.document_name}.`, "success");
    renderResult(result);
    await loadDocuments();
  } catch (e) {
    setStatus(e.message, "error");
  } finally { btn.disabled = false; }
});

$("refreshBtn").addEventListener("click", () => loadDocuments().catch(e => setStatus(e.message, "error")));
$("closeResult").addEventListener("click", () => $("resultCard").classList.add("hidden"));
loadDocuments().catch(e => setStatus(e.message, "error"));
