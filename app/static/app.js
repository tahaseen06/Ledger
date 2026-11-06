// All dynamic text goes through textContent (account names are user-supplied -> no HTML injection).
let token = sessionStorage.getItem("ll_token"), role = sessionStorage.getItem("ll_role"), pendingKey = null, pendingSig = null;
const $ = (id) => document.getElementById(id);
const inr = (p) => (p / 100).toLocaleString("en-IN", { style: "currency", currency: "INR" });
function say(text, ok) { const m = $("msg"); m.textContent = text; m.className = ok ? "ok" : "err"; }
async function api(path, method = "GET", body, key) {
  const h = { "Content-Type": "application/json" };
  if (token) h.Authorization = "Bearer " + token;
  if (key) h["Idempotency-Key"] = key;
  const r = await fetch("/api" + path, { method, headers: h, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  return { status: r.status, data };
}
function cell(tr, text, cls, tag = "td") { const c = document.createElement(tag); c.textContent = text ?? ""; if (cls) c.className = cls; tr.appendChild(c); return c; }
function table(el, head, rows) {
  el.replaceChildren(); const h = document.createElement("tr"); head.forEach((t) => cell(h, t, "", "th")); el.appendChild(h);
  rows.forEach((r) => { const tr = document.createElement("tr"); if (r.cls) tr.className = r.cls; r.cells.forEach((c) => cell(tr, c.t, c.cls)); if (r.onclick) { tr.style.cursor = "pointer"; tr.onclick = r.onclick; } el.appendChild(tr); });
}
async function loadAccounts() {
  const { status, data } = await api("/accounts"); if (status !== 200) return;
  table($("accts"), ["Number", "Name", "Owner", "Balance", "Overdraft", "State"], data.map((a) => ({
    cells: [{ t: a.number }, { t: a.name }, { t: a.owner }, { t: inr(a.balance_paise), cls: "num" }, { t: inr(a.overdraft_limit_paise), cls: "num" }, { t: a.frozen ? "FROZEN" : "active" }],
    onclick: () => loadStatement(a.number) })));
  if (data[0] && !$("stNo").textContent) loadStatement(data[0].number);
}
async function loadStatement(no) {
  $("stNo").textContent = no; const { status, data } = await api(`/accounts/${no}/statement`); if (status !== 200) return;
  table($("stmt"), ["When (UTC)", "Type", "Counterparty", "By", "Debit", "Credit", "Balance", "Status"], data.entries.map((e) => ({
    cls: e.status === "refused" ? "refused" : "",
    cells: [{ t: e.created_at.slice(0, 19).replace("T", " ") }, { t: e.type }, { t: e.counterparty }, { t: e.initiated_by },
      { t: e.debit_paise ? inr(e.debit_paise) : (e.status === "refused" ? inr(e.amount) : ""), cls: "num" }, { t: e.credit_paise ? inr(e.credit_paise) : "", cls: "num" },
      { t: inr(e.balance_after), cls: "num" }, { t: e.status === "refused" ? "REFUSED: " + e.reason : "posted" + (e.flagged ? " (flagged)" : ""), cls: e.flagged ? "flag" : "" }] })));
}
function show() {
  $("login").classList.toggle("hidden", !!token); $("app").classList.toggle("hidden", !token);
  $("who").textContent = token ? role : "";
  if (!token) return;
  const kinds = role === "admin" ? ["deposit", "withdrawal"] : role === "holder" ? ["transfer", "withdrawal"] : [];
  $("kind").replaceChildren(...kinds.map((k) => { const o = document.createElement("option"); o.value = o.textContent = k; return o; }));
  $("moveBox").classList.toggle("hidden", !kinds.length);
  $("auditBox").classList.toggle("hidden", role === "holder");
  loadAccounts();
}
$("loginBtn").onclick = async () => {
  const { status, data } = await api("/auth/login", "POST", { username: $("u").value, password: $("p").value });
  if (status !== 200) return say(data.message || "login failed");
  token = data.token; role = data.role; sessionStorage.setItem("ll_token", token); sessionStorage.setItem("ll_role", role); $("msg").className = ""; $("msg").style.display = "none"; show();
};
$("logout").onclick = async () => { await api("/auth/logout", "POST"); token = null; sessionStorage.clear(); $("stNo").textContent = ""; show(); };
$("go").onclick = async () => {
  const kind = $("kind").value, amt = Math.round(parseFloat($("amt").value) * 100);
  if (!Number.isInteger(amt) || amt <= 0) return say("Enter a positive amount");
  const payload = kind === "transfer" ? { from_account: $("from").value.trim(), to_account: $("to").value.trim(), amount_paise: amt }
    : { account: ($("from").value || $("to").value).trim(), amount_paise: amt };
  const sig = kind + JSON.stringify(payload);
  if (sig !== pendingSig) { pendingSig = sig; pendingKey = crypto.randomUUID(); }  // same intent -> same key (retries/double-clicks are safe)
  $("go").disabled = true;
  try {
    const { status, data } = await api(kind === "transfer" ? "/transfers" : kind === "deposit" ? "/deposits" : "/withdrawals", "POST", payload, pendingKey);
    if (status === 201) { say("Posted " + inr(amt) + " (txn " + data.txn_id.slice(0, 8) + ")", true); pendingSig = null; }
    else if (status === 503) say("Server busy - press Submit again (same key, safe to retry).");
    else { say((data.reason || data.message || "failed") + " (" + status + ")"); pendingSig = null; }
    loadAccounts(); if ($("stNo").textContent) loadStatement($("stNo").textContent);
  } catch (e) { say("Network error - press Submit again; the same key makes the retry safe."); }
  finally { $("go").disabled = false; }
};
async function audit(path) { const { status, data } = await api(path); $("auditOut").textContent = JSON.stringify(data, null, 1); }
$("verifyBtn").onclick = () => audit("/audit/verify");
$("flagBtn").onclick = () => audit("/audit/journal?filter=flagged");
$("refBtn").onclick = () => audit("/audit/journal?filter=refused");
$("repBtn").onclick = () => audit("/audit/replays");
show();
