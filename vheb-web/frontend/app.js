"use strict";
/* VHEB frontend -- vanilla JS, no build step. One file, hash-routed.
   Mobile-first: everything below works down to a ~320px viewport. */

const MONTH_NAMES = ["January","February","March","April","May","June","July",
                     "August","September","October","November","December"];

const state = {
  token: localStorage.getItem("vheb_token") || "",
  meters: [],
  settings: {},
  wizard: null,
};

/* ------------------------------------------------------------------ API -- */
async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401) {
    logout();
    throw new Error("Session expired -- please unlock again.");
  }
  const isJson = resp.headers.get("content-type")?.includes("application/json");
  const data = isJson ? await resp.json() : null;
  if (!resp.ok) throw new Error((data && data.detail) || `Request failed (${resp.status})`);
  return data;
}

/* ---------------------------------------------------------------- toast -- */
function ensureToastRoot() {
  let root = document.getElementById("toast-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "toast-root";
    document.body.appendChild(root);
  }
  return root;
}
function toast(message, { error = false } = {}) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.textContent = message;
  ensureToastRoot().appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

/* ----------------------------------------------------------------- auth -- */
function showLogin() {
  document.getElementById("login-screen").hidden = false;
  document.getElementById("app-shell").hidden = true;
}
function showApp() {
  document.getElementById("login-screen").hidden = true;
  document.getElementById("app-shell").hidden = false;
}
function logout() {
  state.token = "";
  localStorage.removeItem("vheb_token");
  showLogin();
}
async function tryLogin(token) {
  const prev = state.token;
  state.token = token;
  try {
    await api("/api/auth/verify", { method: "POST" });
    localStorage.setItem("vheb_token", token);
    showApp();
    route();
  } catch (e) {
    state.token = prev;
    const err = document.getElementById("login-error");
    err.hidden = false;
    err.textContent = "That token was rejected.";
  }
}

document.getElementById("login-btn").addEventListener("click", () => {
  const val = document.getElementById("token-input").value.trim();
  if (val) tryLogin(val);
});
document.getElementById("token-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("login-btn").click();
});
document.getElementById("logout-btn").addEventListener("click", logout);

/* --------------------------------------------------------------- router -- */
const routes = {
  dashboard: renderDashboard,
  readings: renderWizard,
  meters: renderMeters,
  history: renderHistory,
  settings: renderSettings,
};
const ROUTE_TITLES = { dashboard: "Home", readings: "New bill", meters: "Meters", history: "History", settings: "Settings" };
function route() {
  const hash = (location.hash || "#/dashboard").split("?")[0].replace("#/", "");
  const name = routes[hash] ? hash : "dashboard";
  document.querySelectorAll(".spine-tab").forEach((a) => {
    a.classList.toggle("active", a.dataset.route === name);
  });
  const tb = document.getElementById("tb-title");
  if (tb) tb.textContent = ROUTE_TITLES[name] || "VHEB";
  window.scrollTo(0, 0);
  routes[name]();
}
window.addEventListener("hashchange", route);
document.getElementById("logout-btn-m")?.addEventListener("click", logout);

/* ---------------------------------------------------- water split (local) -- */
const round2 = (n) => Math.round((Number(n) + Number.EPSILON) * 100) / 100;

/** Spread `pool` across `tenants` so the total is always exactly `pool`.
 *  excluded -> 0 and out of the split. pinned -> held exact. everyone else
 *  shares what's left, weighted by mode (proportional = by units, else equal).
 *  Mirrors calculations.compute_month's water block byte-for-byte. */
function localWaterSplit(tenants, pool, mode, pinned, excluded) {
  const out = {};
  let pinSum = 0;
  const auto = [];
  for (const m of tenants) {
    if (excluded[m.meter_id]) { out[m.meter_id] = 0; continue; }
    const p = pinned[m.meter_id];
    if (p !== undefined && p !== "" && Number.isFinite(Number(p))) {
      out[m.meter_id] = round2(p); pinSum += out[m.meter_id];
    } else auto.push(m);
  }
  const remaining = round2(pool - pinSum);
  if (auto.length) {
    let weights = auto.map((m) => (mode === "proportional" ? Math.max(Number(m.units) || 0, 0) : 1));
    if (weights.every((x) => !x)) weights = auto.map(() => 1);
    const wsum = weights.reduce((a, b) => a + b, 0) || 1;
    let acc = 0;
    auto.forEach((m, i) => {
      const v = i === auto.length - 1 ? round2(remaining - acc) : round2((remaining * weights[i]) / wsum);
      out[m.meter_id] = v; acc = round2(acc + v);
    });
  }
  return out;
}

/* ------------------------------------------------------------- helpers -- */
function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
function cur() { return state.settings.currency_symbol || "Rs"; }
function fmtMoney(n) { return `${cur()}${Number(n || 0).toFixed(2)}`; }
function num(v) { const n = Number(v); return Number.isFinite(n) ? n : null; }
function page() { return document.getElementById("page"); }
function setPage(html) { page().innerHTML = html; }
function monthLabel(ym) {
  if (!ym) return "";
  const [y, m] = ym.split("-").map(Number);
  return `${MONTH_NAMES[m - 1]} ${y}`;
}
function addMonth(ym, delta) {
  let [y, m] = ym.split("-").map(Number);
  m += delta;
  y += Math.floor((m - 1) / 12);
  m = ((m - 1) % 12 + 12) % 12 + 1;
  return `${y}-${String(m).padStart(2, "0")}`;
}
function thisMonthKey() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
function animateNumber(elm, target) {
  const dur = 700, t0 = performance.now(), pre = cur();
  function tick(t) {
    const p = Math.min(1, (t - t0) / dur);
    elm.textContent = pre + Math.round(target * (1 - Math.pow(1 - p, 3))).toLocaleString();
    if (p < 1) requestAnimationFrame(tick);
    else elm.textContent = pre + Math.round(target).toLocaleString();
  }
  requestAnimationFrame(tick);
}

/* ------------------------------------------------------------ dashboard -- */
async function renderDashboard() {
  setPage(`<h1>Home</h1><div class="panel"><div class="sk" style="width:60%;height:2.4rem"></div></div>`);
  try {
    const [months, settingsDict] = await Promise.all([api("/api/months"), api("/api/settings")]);
    state.settings = settingsDict;
    const last = months[months.length - 1];
    const building = settingsDict.building_name || "Your building";
    const margin = last && last.main_bill_amount
      ? (((last.total_collected - last.main_bill_amount) / last.main_bill_amount) * 100).toFixed(1) + "%" : "—";

    const hero = last ? `
      <div class="panel">
        <div class="hero">
          <span class="hero-figure"><span id="hero-num">0</span></span>
          <span class="hero-label">collected in ${monthLabel(last.month)}</span>
        </div>
        <div class="stat-row" style="margin-bottom:0">
          <div class="stat"><div class="k">Utility bill</div><div class="v">${fmtMoney(last.main_bill_amount)}</div></div>
          <div class="stat"><div class="k">Margin</div><div class="v">${margin}</div></div>
          <div class="stat"><div class="k">Water pool</div><div class="v">${fmtMoney(last.water_pool_amount)}</div></div>
          <div class="stat"><div class="k">Status</div><div class="v" style="font-size:1rem;text-transform:capitalize">${esc(last.status || "draft")}</div></div>
        </div>
      </div>` : `<div class="panel"><p class="muted">No months recorded yet — start with “New bill”.</p></div>`;

    setPage(`
      <h1>${esc(building)}</h1>
      ${hero}
      <div class="btn-row" style="margin:0 0 1.4rem"><a href="#/readings" class="btn btn-primary btn-inline">＋ Start this month's bill</a></div>
      <h2>Recent months</h2>
      <div class="panel">${months.length ? monthList(months.slice(-6).reverse()) : "<p class='muted'>Nothing yet.</p>"}</div>
    `);
    if (last) animateNumber(document.getElementById("hero-num"), Number(last.total_collected || 0));
  } catch (e) {
    setPage(`<h1>Home</h1><p class="warning-box">${esc(e.message)}</p>`);
  }
}

function monthList(months) {
  return `<div class="month-list">
    ${months.map((m) => {
      const margin = m.main_bill_amount
        ? (((m.total_collected - m.main_bill_amount) / m.main_bill_amount) * 100).toFixed(1) + "%" : "—";
      const st = m.status || "draft";
      return `<a class="month-row" href="#/history?month=${m.month}">
        <span class="month-row-main">
          <span class="month-row-name">${monthLabel(m.month)}</span>
          <span class="month-row-sub">bill ${fmtMoney(m.main_bill_amount)} · margin ${margin}
            <span class="pill ${st === "finalized" ? "pill-green" : st === "imported" ? "pill-brass" : "pill-brass"}">${esc(st)}</span></span>
        </span>
        <span class="month-row-amt mono">${fmtMoney(m.total_collected)}</span>
      </a>`;
    }).join("")}
  </div>`;
}

/* =====================================================================
   NEW BILL WIZARD
   ===================================================================== */
async function renderWizard() {
  if (!state.wizard) {
    state.wizard = {
      step: 1,
      month: null,
      main: {},           // { current, bill, last }
      unitPrice: "",      // string, blank = use suggestion
      waterMode: "even",  // "even" | "proportional" | "manual"
      rows: {},           // meter_id -> { mode:'auto'|'manual', current, lastOverride, units, price }
      overrides: {},      // meter_id -> total (step 2, hard override)
      waterPinned: {},    // meter_id -> water amount typed by hand (step 2)
      waterExcluded: {},  // meter_id -> true (flat away this month, no water share)
      draft: null,
      compute: null,
    };
  }
  const w = state.wizard;
  setPage(`
    <h1 class="hide-mobile">New bill</h1>
    <div class="steps">
      <div class="step ${w.step === 1 ? "active" : w.step > 1 ? "done" : ""}">1&nbsp;·&nbsp;Readings</div>
      <div class="step ${w.step === 2 ? "active" : w.step > 2 ? "done" : ""}">2&nbsp;·&nbsp;Review</div>
      <div class="step ${w.step === 3 ? "active" : ""}">3&nbsp;·&nbsp;Send</div>
    </div>
    <div id="wizard-body">Loading…</div>
  `);
  if (w.step === 1) await wizardStep1();
  else if (w.step === 2) wizardStep2();
  else wizardStep3();
}

/* ------- shared: build the compute request from wizard state ------------ */
function computePayload(extra = {}) {
  const w = state.wizard;
  const readings = {}, last_readings = {}, units_overrides = {}, unit_price_overrides = {};
  for (const m of billableMeters()) {
    const r = w.rows[m.meter_id] || {};
    if (r.lastOverride !== undefined && r.lastOverride !== "" && num(r.lastOverride) !== null)
      last_readings[m.meter_id] = num(r.lastOverride);
    if (r.mode === "manual") {
      if (num(r.units) !== null) units_overrides[m.meter_id] = num(r.units);
    } else if (r.current !== undefined && r.current !== "" && num(r.current) !== null) {
      readings[m.meter_id] = num(r.current);
    }
    if (r.price !== undefined && r.price !== "" && num(r.price) !== null)
      unit_price_overrides[m.meter_id] = num(r.price);
  }
  const derived = derivedMeter();
  if (derived) {
    const r = w.rows[derived.meter_id] || {};
    if (r.mode === "manual" && num(r.units) !== null) units_overrides[derived.meter_id] = num(r.units);
    if (r.price !== undefined && r.price !== "" && num(r.price) !== null)
      unit_price_overrides[derived.meter_id] = num(r.price);
  }
  const water_overrides = {}, overrides = {};
  for (const [mid, v] of Object.entries(w.waterPinned || {}))
    if (v !== "" && num(v) !== null) water_overrides[mid] = num(v);
  for (const [mid, v] of Object.entries(w.overrides || {}))
    if (v !== "" && num(v) !== null) overrides[mid] = num(v);
  const water_excluded = Object.keys(w.waterExcluded || {}).filter((k) => w.waterExcluded[k]);

  return {
    month: w.month,
    main_current: num(w.main.current),
    main_bill_amount: num(w.main.bill),
    main_last: w.draft?.needs_main_last_bootstrap ? num(w.main.last) : undefined,
    unit_price: w.unitPrice === "" ? null : num(w.unitPrice),
    water_mode: w.waterMode,
    readings, last_readings, units_overrides, unit_price_overrides,
    overrides, water_overrides, water_excluded,
    ...extra,
  };
}

function billableMeters() {
  return state.meters.filter((m) => m.type === "physical" || m.type === "motor");
}
function derivedMeter() {
  return state.meters.find((m) => m.type === "derived");
}
function recommendedPrice() {
  const w = state.wizard;
  const bill = num(w.main.bill);
  const cur = num(w.main.current);
  const last = w.draft?.needs_main_last_bootstrap ? num(w.main.last) : w.draft?.main_last_reading;
  const margin = num(state.settings.default_margin_pct) ?? 7;
  if (!bill || !cur || last == null || cur - last <= 0) return null;
  return Math.round((bill * (1 + margin / 100) / (cur - last)) * 100) / 100;
}

/* --------------------------------- STEP 1 ------------------------------- */
async function wizardStep1() {
  const w = state.wizard;
  try {
    const [draft, settingsDict] = await Promise.all([
      api("/api/months/draft/new"), api("/api/settings"),
    ]);
    state.settings = settingsDict;
    state.meters = draft.meters;
    w.draft = draft;
    if (!w.month) {
      const months = await api("/api/months").catch(() => []);
      const lastReal = [...months].map((m) => m.month).sort().pop();
      w.month = lastReal ? addMonth(lastReal, 1) : thisMonthKey();
    }
    if (w.waterMode === "even" && settingsDict.default_water_mode === "proportional")
      w.waterMode = "proportional";

    // seed row state
    for (const m of [...billableMeters(), derivedMeter()].filter(Boolean)) {
      if (!w.rows[m.meter_id]) w.rows[m.meter_id] = { mode: "auto", current: "", lastOverride: "", units: "", price: "" };
    }

    document.getElementById("wizard-body").innerHTML = step1Html();
    wireStep1();
    refreshPreview();
  } catch (e) {
    document.getElementById("wizard-body").innerHTML = `<p class="warning-box">${esc(e.message)}</p>`;
  }
}

function step1Html() {
  const w = state.wizard;
  const draft = w.draft;
  const missingBaseline = billableMeters().filter((m) => draft.last_readings[m.meter_id] == null);
  const rec = recommendedPrice();

  return `
  <div class="wizard-grid">
    <div class="wizard-main">
      ${missingBaseline.length ? `
        <div class="warning-box">
          No previous reading on file for: <strong>${missingBaseline.map((m) => esc(m.label)).join(", ")}</strong>.
          Enter last month's reading for each below (one time), or switch that meter to "set units directly".
        </div>` : ""}

      <div class="panel">
        <h3>This month</h3>
        <div class="grid-2">
          <label class="fl">Month
            <input id="f-month" type="text" inputmode="numeric" value="${esc(w.month)}" placeholder="YYYY-MM">
          </label>
          <label class="fl">Main meter — current reading
            <input id="f-main-current" type="number" step="any" value="${esc(w.main.current ?? "")}">
          </label>
        </div>
        ${draft.needs_main_last_bootstrap ? `
          <label class="fl">Main meter — last month's reading (first run only)
            <input id="f-main-last" type="number" step="any" value="${esc(w.main.last ?? "")}">
          </label>` : `<p class="hint">Last main reading on file: <span class="tabular">${draft.main_last_reading ?? "—"}</span></p>`}
        <label class="fl">Main utility (NagarNigam) bill amount — ${cur()}
          <input id="f-main-bill" type="number" step="any" value="${esc(w.main.bill ?? "")}">
        </label>
        <label class="fl">Unit price to charge — ${cur()}/unit
          <input id="f-unit-price" type="number" step="any" value="${esc(w.unitPrice)}"
                 placeholder="${rec ?? "auto"}">
          <span class="hint" id="rec-hint">${rec ? `Recommended ${cur()}${rec}/unit (this month's cost + ${num(state.settings.default_margin_pct) ?? 7}% margin). Leave blank to use it.` : "Fill main reading + bill to get a recommendation."}</span>
        </label>
        <fieldset class="fl">
          <legend>Water split (motor cost across the flats)</legend>
          <label class="radio"><input type="radio" name="water" value="even" ${w.waterMode === "even" ? "checked" : ""}> Equal share each</label>
          <label class="radio"><input type="radio" name="water" value="proportional" ${w.waterMode === "proportional" ? "checked" : ""}> By electricity units used this month</label>
          <label class="radio"><input type="radio" name="water" value="manual" ${w.waterMode === "manual" ? "checked" : ""}> Manual — type some shares on the next screen, the rest auto-split</label>
        </fieldset>
      </div>

      <div class="panel">
        <h3>Meter readings</h3>
        <div class="meter-cards">
          ${billableMeters().map(meterCardHtml).join("")}
          ${derivedMeter() ? derivedCardHtml(derivedMeter()) : ""}
        </div>
      </div>

      <div class="btn-row sticky"><button id="f-next" class="btn btn-primary btn-block">Review bill →</button></div>
    </div>

    <aside class="wizard-preview" id="wizard-preview">
      <h3>Live preview</h3>
      <div id="preview-body"><p class="hint">Enter readings to see each flat's bill.</p></div>
    </aside>
  </div>`;
}

function meterCardHtml(m) {
  const w = state.wizard;
  const r = w.rows[m.meter_id];
  const known = w.draft.last_readings[m.meter_id];
  const isMotor = m.type === "motor";
  return `
  <div class="meter-card" data-meter="${m.meter_id}">
    <div class="meter-card-head">
      <span class="meter-card-name">${esc(m.label)}${isMotor ? ' <span class="pill pill-brass">motor</span>' : ""}
        ${m.tenant_name ? `<span class="meter-card-sub">${esc(m.tenant_name)}</span>` : ""}</span>
      <button type="button" class="link-btn" data-toggle-adjust>${r.mode === "manual" || r.lastOverride ? "adjust ▲" : "adjust ▾"}</button>
    </div>
    <div class="meter-card-body">
      ${r.mode === "manual" ? `
        <label class="fl">Units this month (meter stuck / reset / new)
          <input type="number" step="any" data-field="units" value="${esc(r.units)}">
        </label>` : `
        <div class="grid-2">
          <label class="fl">Last reading
            <input type="number" step="any" data-field="lastOverride"
                   value="${esc(r.lastOverride !== "" ? r.lastOverride : (known ?? ""))}"
                   ${known != null && r.lastOverride === "" ? "" : ""}>
          </label>
          <label class="fl">Current reading
            <input type="number" step="any" data-field="current" value="${esc(r.current)}">
          </label>
        </div>`}
      <div class="adjust-panel" ${r.mode === "manual" || r.lastOverride || r.price ? "" : "hidden"}>
        <label class="radio"><input type="checkbox" data-field="manualMode" ${r.mode === "manual" ? "checked" : ""}> Set units directly (stuck / reset / newly installed meter)</label>
        ${!isMotor ? `<label class="fl">This flat's own unit price (optional — overrides the month's price)
          <input type="number" step="any" data-field="price" value="${esc(r.price)}" placeholder="${w.unitPrice || recommendedPrice() || "month price"}">
        </label>` : `<label class="fl">Motor unit price (optional)
          <input type="number" step="any" data-field="price" value="${esc(r.price)}" placeholder="month price">
        </label>`}
      </div>
      <div class="meter-card-calc" data-calc></div>
    </div>
  </div>`;
}

function derivedCardHtml(m) {
  const w = state.wizard;
  const r = w.rows[m.meter_id];
  return `
  <div class="meter-card" data-meter="${m.meter_id}">
    <div class="meter-card-head">
      <span class="meter-card-name">${esc(m.label)} <span class="pill pill-brass">no meter</span>
        ${m.tenant_name ? `<span class="meter-card-sub">${esc(m.tenant_name)}</span>` : ""}</span>
      <button type="button" class="link-btn" data-toggle-adjust>${r.mode === "manual" || r.price ? "adjust ▲" : "adjust ▾"}</button>
    </div>
    <div class="meter-card-body">
      <p class="hint">Units = main meter's rise minus every other meter. Shown in the preview.</p>
      <div class="adjust-panel" ${r.mode === "manual" || r.price ? "" : "hidden"}>
        <label class="radio"><input type="checkbox" data-field="manualMode" ${r.mode === "manual" ? "checked" : ""}> Set this flat's units by hand instead</label>
        ${r.mode === "manual" ? `<label class="fl">Units this month<input type="number" step="any" data-field="units" value="${esc(r.units)}"></label>` : ""}
        <label class="fl">This flat's own unit price (optional)
          <input type="number" step="any" data-field="price" value="${esc(r.price)}" placeholder="month price">
        </label>
      </div>
      <div class="meter-card-calc" data-calc></div>
    </div>
  </div>`;
}

function wireStep1() {
  const w = state.wizard;
  const body = document.getElementById("wizard-body");

  const bindMain = (id, key, obj) => {
    const inp = document.getElementById(id);
    if (!inp) return;
    inp.addEventListener("input", () => { obj[key] = inp.value; onStep1Change(); });
  };
  bindMain("f-month", "month", w);
  bindMain("f-main-current", "current", w.main);
  bindMain("f-main-last", "last", w.main);
  bindMain("f-main-bill", "bill", w.main);
  bindMain("f-unit-price", "unitPrice", w);

  body.querySelectorAll('input[name="water"]').forEach((radio) => {
    radio.addEventListener("change", () => { w.waterMode = radio.value; refreshPreview(); });
  });

  body.querySelectorAll(".meter-card").forEach((card) => {
    const meterId = card.dataset.meter;
    const r = w.rows[meterId];
    card.querySelector("[data-toggle-adjust]")?.addEventListener("click", () => {
      const panel = card.querySelector(".adjust-panel");
      panel.hidden = !panel.hidden;
      card.querySelector("[data-toggle-adjust]").textContent = panel.hidden ? "adjust ▾" : "adjust ▲";
    });
    card.querySelectorAll("[data-field]").forEach((inp) => {
      const field = inp.dataset.field;
      const evt = inp.type === "checkbox" ? "change" : "input";
      inp.addEventListener(evt, () => {
        if (field === "manualMode") {
          r.mode = inp.checked ? "manual" : "auto";
          renderStep1Cards();
        } else {
          r[field] = inp.value;
          onStep1Change();
        }
      });
    });
  });

  document.getElementById("f-next").addEventListener("click", goToReview);
}

function renderStep1Cards() {
  const container = document.querySelector(".meter-cards");
  if (!container) return;
  container.innerHTML =
    billableMeters().map(meterCardHtml).join("") +
    (derivedMeter() ? derivedCardHtml(derivedMeter()) : "");
  // re-wire just the cards
  const w = state.wizard;
  container.querySelectorAll(".meter-card").forEach((card) => {
    const meterId = card.dataset.meter;
    const r = w.rows[meterId];
    card.querySelector("[data-toggle-adjust]")?.addEventListener("click", () => {
      const panel = card.querySelector(".adjust-panel");
      panel.hidden = !panel.hidden;
      card.querySelector("[data-toggle-adjust]").textContent = panel.hidden ? "adjust ▾" : "adjust ▲";
    });
    card.querySelectorAll("[data-field]").forEach((inp) => {
      const field = inp.dataset.field;
      const evt = inp.type === "checkbox" ? "change" : "input";
      inp.addEventListener(evt, () => {
        if (field === "manualMode") { r.mode = inp.checked ? "manual" : "auto"; renderStep1Cards(); }
        else { r[field] = inp.value; onStep1Change(); }
      });
    });
  });
  refreshPreview();
}

function onStep1Change() {
  const rec = recommendedPrice();
  const hint = document.getElementById("rec-hint");
  const priceInput = document.getElementById("f-unit-price");
  if (hint && priceInput) {
    priceInput.placeholder = rec ?? "auto";
    hint.textContent = rec
      ? `Recommended ${cur()}${rec}/unit (this month's cost + ${num(state.settings.default_margin_pct) ?? 7}% margin). Leave blank to use it.`
      : "Fill main reading + bill to get a recommendation.";
  }
  refreshPreview();
}

const refreshPreview = debounce(async () => {
  const w = state.wizard;
  const box = document.getElementById("preview-body");
  if (!box) return;
  if (!w.month || num(w.main.current) === null || num(w.main.bill) === null) {
    box.innerHTML = `<p class="hint">Enter the month, main reading and bill amount to see the preview.</p>`;
    return;
  }
  try {
    const result = await api("/api/months/compute", { method: "POST", body: computePayload() });
    w.compute = result;
    box.innerHTML = previewHtml(result);
    // per-card calc lines
    document.querySelectorAll(".meter-card[data-meter]").forEach((card) => {
      const mr = result.meters.find((x) => x.meter_id === card.dataset.meter);
      const calc = card.querySelector("[data-calc]");
      if (mr && calc) {
        calc.innerHTML = mr.type === "motor"
          ? `<span class="tabular">${mr.units}</span> units → pool ${fmtMoney(mr.electricity_amount)}`
          : `<span class="tabular">${mr.units}</span> units × ${cur()}${mr.unit_price} = ${fmtMoney(mr.electricity_amount)}${mr.warning ? ` · <span class="warn-text">${esc(mr.warning)}</span>` : ""}`;
      }
    });
  } catch (e) {
    box.innerHTML = `<p class="warn-text">${esc(e.message)}</p>`;
    w.compute = null;
  }
}, 350);

function previewHtml(r) {
  const w = state.wizard;
  const tenants = r.meters.filter((m) => m.type === "physical" || m.type === "derived");
  return `
    <p class="hint">Main rise <span class="tabular">${r.main_delta}</span> ·
      pool ${fmtMoney(r.water_pool_amount)} ·
      collected <span class="tabular">${fmtMoney(r.total_collected)}</span> ·
      margin ${r.achieved_margin_pct}%</p>
    ${tenants.map((m) => `
      <div class="bill-card">
        <pre>${esc(billText(m, w.month))}</pre>
      </div>`).join("")}
  `;
}

function billText(m, month) {
  const c = cur();
  const lines = [
    `*Electricity Bill for ${m.label}*`,
    `${monthLabel(month)}  |  _${c}${m.unit_price}/Unit_`,
    ``,
    `Units   :  ${m.units}`,
    `Amount  :  ${c}${Number(m.electricity_amount).toFixed(2)}`,
  ];
  lines.push(`Water   :  ${c}${Number(m.water_share_amount || 0).toFixed(2)}`);
  lines.push(`*Total   :  ${c}${Number(m.total_amount).toFixed(2)}*`);
  return lines.join("\n");
}

async function goToReview() {
  const w = state.wizard;
  if (!w.month || !/^\d{4}-\d{2}$/.test(w.month)) return toast("Month must look like 2026-09.", { error: true });
  if (num(w.main.current) === null) return toast("Enter the main meter's current reading.", { error: true });
  if (num(w.main.bill) === null) return toast("Enter the main utility bill amount.", { error: true });
  try {
    w.compute = await api("/api/months/compute", { method: "POST", body: computePayload() });
    w.step = 2;
    renderWizard();
  } catch (e) { toast(e.message, { error: true }); }
}

/* --------------------------------- STEP 2 ------------------------------- */
const WATER_MODES = [
  ["even", "Equal"],
  ["proportional", "By units"],
  ["manual", "Manual"],
];

function wizardStep2() {
  const w = state.wizard;
  const r = w.compute;
  const body = document.getElementById("wizard-body");
  const tenants = r.meters.filter((m) => m.type === "physical" || m.type === "derived");

  body.innerHTML = `
    ${r.meters.some((m) => m.warning) ? `<div class="warning-box">${esc(r.meters.find((m) => m.warning).warning)}</div>` : ""}
    <div class="panel">
      <h3>${monthLabel(w.month)} — review</h3>
      <div class="stat-row">
        <div class="stat"><div class="k">Main rise</div><div class="v">${r.main_delta}</div></div>
        <div class="stat"><div class="k">Unit price</div><div class="v">${fmtMoney(r.unit_price)}</div></div>
        <div class="stat"><div class="k">Recommended</div><div class="v">${r.recommended_unit_price ? fmtMoney(r.recommended_unit_price) : "—"}</div></div>
        <div class="stat"><div class="k">Collected</div><div class="v" id="s2-collected">${fmtMoney(r.total_collected)}</div></div>
      </div>

      <div class="fl" style="margin-bottom:.6rem">Water split
        <div class="segmented" id="s2-modes" style="margin-top:.35rem">
          ${WATER_MODES.map(([v, lbl]) => `<button type="button" data-mode="${v}" class="${w.waterMode === v ? "on" : ""}">${lbl}</button>`).join("")}
        </div>
      </div>
      <p class="hint" id="s2-balance"></p>

      <div class="rowcards" id="s2-rows">
        ${tenants.map((m) => `
          <div class="rowcard" data-row="${m.meter_id}">
            <div class="rowcard-top">
              <div>
                <div class="rowcard-name">${esc(m.label)}
                  <span class="pill pill-brass" data-pin-tag="${m.meter_id}" hidden>pinned</span></div>
                <div class="rowcard-sub">${esc(m.tenant_name || "")} · ${m.units} units · elec ${fmtMoney(m.electricity_amount)}</div>
              </div>
              <label class="switch" title="include in water split">
                <input type="checkbox" data-include="${m.meter_id}" checked>
                <span class="track"></span>
              </label>
            </div>
            <div class="rowcard-grid">
              <label class="fl">Water share
                <input type="number" step="any" inputmode="decimal" data-water="${m.meter_id}">
              </label>
              <label class="fl">Total (this flat pays)
                <input type="number" step="any" inputmode="decimal" data-total="${m.meter_id}">
              </label>
            </div>
          </div>`).join("")}
      </div>
      <p class="hint" style="margin-top:.8rem">
        <button type="button" class="link-btn" id="s2-clear">reset water &amp; totals</button>
        · to change a meter reading, go back.
      </p>
    </div>
    <div class="btn-row sticky">
      <button id="s2-back" class="btn btn-quiet btn-inline">← Back</button>
      <button id="s2-confirm" class="btn btn-primary btn-inline">Confirm →</button>
    </div>
  `;

  const q = (sel) => body.querySelector(sel);

  function s2Apply() {
    const pool = r.water_pool_amount;
    const split = localWaterSplit(tenants, pool, w.waterMode, w.waterPinned, w.waterExcluded);
    let collected = 0, pinSum = 0, autoCount = 0;
    tenants.forEach((m) => {
      const mid = m.meter_id;
      const excl = !!w.waterExcluded[mid];
      const pinRaw = w.waterPinned[mid];
      const pinned = !excl && pinRaw !== undefined && pinRaw !== "" && num(pinRaw) !== null;
      if (pinned) pinSum += round2(pinRaw); else if (!excl) autoCount++;
      const water = excl ? 0 : split[mid];
      const ovrRaw = w.overrides[mid];
      const hasOvr = ovrRaw !== undefined && ovrRaw !== "" && num(ovrRaw) !== null;
      const total = hasOvr ? round2(ovrRaw) : round2(m.electricity_amount + water);
      collected = round2(collected + total);

      const wi = q(`[data-water="${mid}"]`);
      wi.disabled = excl;
      wi.classList.toggle("pinned-input", pinned);
      if (document.activeElement !== wi) wi.value = water.toFixed(2);
      const ti = q(`[data-total="${mid}"]`);
      if (document.activeElement !== ti) ti.value = total.toFixed(2);
      const cb = q(`[data-include="${mid}"]`); cb.checked = !excl;
      q(`[data-row="${mid}"]`).classList.toggle("is-off", excl);
      q(`[data-pin-tag="${mid}"]`).hidden = !pinned;
    });
    q("#s2-collected").textContent = fmtMoney(collected);
    const leftover = round2(pool - pinSum);
    let msg;
    if (autoCount === 0 && Math.abs(leftover) > 0.01)
      msg = `<span class="warn-text">${fmtMoney(Math.abs(leftover))} ${leftover > 0 ? "unallocated — pin/untick fewer flats" : "over the pool"}</span>`;
    else if (leftover < -0.01)
      msg = `<span class="warn-text">pinned amounts exceed the pool — the rest go negative</span>`;
    else msg = "the rest auto-adjust so the water pool is always fully shared";
    q("#s2-balance").innerHTML = `Water pool <strong>${fmtMoney(pool)}</strong> · ${msg}`;
  }

  q("#s2-modes").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-mode]");
    if (!btn) return;
    w.waterMode = btn.dataset.mode;
    q("#s2-modes").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    s2Apply();
  });

  body.querySelectorAll("[data-include]").forEach((cb) => {
    cb.addEventListener("change", () => {
      const mid = cb.dataset.include;
      if (cb.checked) delete w.waterExcluded[mid];
      else { w.waterExcluded[mid] = true; delete w.waterPinned[mid]; }
      s2Apply();
    });
  });
  body.querySelectorAll("[data-water]").forEach((inp) => {
    const onEdit = () => {
      const mid = inp.dataset.water;
      if (inp.value === "") delete w.waterPinned[mid];
      else w.waterPinned[mid] = inp.value;
      s2Apply();
    };
    inp.addEventListener("input", onEdit);
    inp.addEventListener("blur", () => { onEdit(); });
  });
  body.querySelectorAll("[data-total]").forEach((inp) => {
    inp.addEventListener("input", () => {
      const mid = inp.dataset.total;
      if (inp.value === "") delete w.overrides[mid];
      else w.overrides[mid] = inp.value;
      s2Apply();
    });
  });
  q("#s2-clear").addEventListener("click", () => {
    w.waterPinned = {}; w.waterExcluded = {}; w.overrides = {};
    s2Apply();
  });
  q("#s2-back").addEventListener("click", () => { w.step = 1; renderWizard(); });
  q("#s2-confirm").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      w.compute = await api("/api/months/finalize", { method: "POST", body: computePayload() });
      w.step = 3;
      renderWizard();
      toast("Saved to the sheet.");
    } catch (err) { toast(err.message, { error: true }); e.target.disabled = false; }
  });

  s2Apply();
}

/* --------------------------------- STEP 3 ------------------------------- */
function wizardStep3() {
  const w = state.wizard;
  const r = w.compute;
  const tenants = r.meters.filter((m) => m.type === "physical" || m.type === "derived");
  const body = document.getElementById("wizard-body");

  body.innerHTML = `
    <div class="panel">
      <p class="muted" style="margin-bottom:.8rem">Every bill is sent to <strong>your own WhatsApp number</strong> first — check each one, then forward it to the tenant.</p>
      <button id="s3-send" class="btn btn-primary btn-block">Send all to my WhatsApp</button>
    </div>
    <div id="s3-results"></div>
    <div class="panel">
      <h3>${monthLabel(w.month)} — bills</h3>
      <div class="preview-list">
        ${tenants.map((m) => `<div class="bill-card"><pre>${esc(billText(m, w.month))}</pre></div>`).join("")}
      </div>
    </div>
    <div class="btn-row"><button id="s3-new" class="btn btn-quiet btn-block">Done — start another month</button></div>
  `;

  body.querySelector("#s3-send").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const res = await api("/api/months/send", { method: "POST", body: { month: w.month } });
      body.querySelector("#s3-results").innerHTML = `
        <div class="panel"><div class="rowcards">
          ${res.results.map((x) => `
            <div class="rowcard">
              <div class="rowcard-top">
                <div class="rowcard-name">${esc(x.tenant_name)}</div>
                <span class="pill ${x.ok ? "pill-green" : "pill-rust"}">${x.ok ? "sent" : "action needed"}</span>
              </div>
              <p class="rowcard-sub" style="margin:.4rem 0 0">${esc(x.detail)}</p>
              <p style="margin:.5rem 0 0;font-size:.88rem">
                ${x.self_wa_me_link ? `<a href="${x.self_wa_me_link}" target="_blank" rel="noopener">Open in WhatsApp (to me)</a>` : ""}
                ${x.forward_wa_me_link ? ` · <a href="${x.forward_wa_me_link}" target="_blank" rel="noopener">Forward to ${esc(x.tenant_name)}</a>` : ""}
              </p>
            </div>`).join("")}
        </div></div>`;
    } catch (err) { toast(err.message, { error: true }); e.target.disabled = false; }
  });
  body.querySelector("#s3-new").addEventListener("click", () => {
    state.wizard = null;
    location.hash = "#/dashboard";
  });
}

/* =====================================================================
   METERS  (locked roster -- names / tenant / phone only)
   ===================================================================== */
async function renderMeters() {
  setPage(`<h1 class="hide-mobile">Meters</h1><p>Loading…</p>`);
  try {
    const meters = await api("/api/meters");
    state.meters = meters;
    setPage(`
      <h1 class="hide-mobile">Meters</h1>
      <p class="hero-label">The building's meters are fixed. You can update a flat's display name, tenant name and phone number — readings and history stay linked to the meter.</p>
      <div class="meter-cards">
        ${meters.map((m) => `
          <div class="panel meter-edit" data-meter="${m.meter_id}">
            <div class="meter-card-head">
              <span class="meter-card-name">${esc(m.label)}
                <span class="pill pill-brass">${esc(m.type)}</span>
                ${m.meter_number ? `<span class="meter-card-sub">No. ${esc(m.meter_number)}</span>` : ""}</span>
            </div>
            ${m.type === "main" ? `<p class="hint">The utility's own meter — nothing to edit.</p>` : `
            <div class="grid-2">
              <label class="fl">Display name<input data-f="label" value="${esc(m.label)}"></label>
              <label class="fl">Tenant name<input data-f="tenant_name" value="${esc(m.tenant_name || "")}"></label>
            </div>
            <label class="fl">Tenant phone (for the forward link — optional)
              <input data-f="phone_number" value="${esc(m.phone_number || "")}" inputmode="tel">
            </label>
            <button class="btn btn-brass btn-inline" data-save>Save</button>`}
          </div>`).join("")}
      </div>
    `);
    page().querySelectorAll(".meter-edit [data-save]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const card = btn.closest(".meter-edit");
        const meterId = card.dataset.meter;
        const base = state.meters.find((x) => x.meter_id == meterId);
        const body = { ...base };
        card.querySelectorAll("[data-f]").forEach((inp) => { body[inp.dataset.f] = inp.value; });
        body.active = String(base.active).toUpperCase() === "TRUE";
        delete body.initial_reading;
        try {
          await api("/api/meters", { method: "POST", body });
          toast("Saved.");
          renderMeters();
        } catch (e) { toast(e.message, { error: true }); }
      });
    });
  } catch (e) {
    setPage(`<h1 class="hide-mobile">Meters</h1><p class="warning-box">${esc(e.message)}</p>`);
  }
}

/* =====================================================================
   HISTORY
   ===================================================================== */
async function renderHistory() {
  setPage(`<h1>History</h1><p>Loading…</p>`);
  try {
    const [months, settingsDict, meters] = await Promise.all([
      api("/api/months"),
      api("/api/settings").catch(() => state.settings),
      api("/api/meters").catch(() => state.meters),
    ]);
    state.settings = settingsDict;
    state.meters = meters;
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const ordered = [...months].reverse();
    const selected = params.get("month");

    const dfltCount = window.matchMedia("(max-width: 899px)").matches ? 1 : 2;
    setPage(`
      <h1 class="hide-mobile">History</h1>
      <div class="panel">
        <label class="fl">Jump to month
          <select id="hist-month">
            <option value="">— all months —</option>
            ${ordered.map((m) => `<option value="${m.month}" ${m.month === selected ? "selected" : ""}>${monthLabel(m.month)}</option>`).join("")}
          </select>
        </label>
        ${months.length ? monthList(ordered) : "<p>Nothing recorded yet.</p>"}
      </div>
      <div id="history-detail"></div>
    `);

    const detail = document.getElementById("history-detail");
    const show = async (list) => {
      detail.innerHTML = "<p>Loading…</p>";
      const parts = [];
      for (const mo of list) parts.push(await monthDetailHtml(mo));
      detail.innerHTML = parts.join("") || "<p class='hint'>No month selected.</p>";
    };
    document.getElementById("hist-month").addEventListener("change", (e) => {
      const v = e.target.value;
      location.hash = v ? `#/history?month=${v}` : "#/history";
      show(v ? [v] : ordered.slice(0, dfltCount).map((m) => m.month));
    });
    await show(selected ? [selected] : ordered.slice(0, dfltCount).map((m) => m.month));
  } catch (e) {
    setPage(`<h1>History</h1><p class="warning-box">${esc(e.message)}</p>`);
  }
}

async function monthDetailHtml(month) {
  try {
    const data = await api(`/api/months/${month}`);
    const s = data.summary;
    const rows = data.readings;
    return `
      <div class="panel">
        <h3>${monthLabel(month)} <span class="pill ${s.status === "finalized" ? "pill-green" : "pill-brass"}">${esc(s.status || "draft")}</span></h3>
        <div class="stat-row">
          <div class="stat"><div class="k">Utility bill</div><div class="v">${fmtMoney(s.main_bill_amount)}</div></div>
          <div class="stat"><div class="k">Unit price</div><div class="v">${fmtMoney(s.unit_price)}</div></div>
          <div class="stat"><div class="k">Water (${esc(s.water_mode || "even")})</div><div class="v">${fmtMoney(s.water_pool_amount)}</div></div>
          <div class="stat"><div class="k">Collected</div><div class="v">${fmtMoney(s.total_collected)}</div></div>
        </div>
        <div class="rowcards">
          ${rows.map((r) => `
            <div class="rowcard">
              <div class="rowcard-top">
                <div class="rowcard-name">${esc(meterLabel(r.meter_id))}
                  <span class="rowcard-sub">${r.reading === "" ? "no meter" : "read " + r.reading} · ${r.units} units @ ${cur()}${r.unit_price}</span></div>
                <div class="month-row-amt mono">${fmtMoney(r.total_amount)}</div>
              </div>
              <div class="kv"><span class="k">Electricity</span><span class="v">${fmtMoney(r.electricity_amount)}</span></div>
              <div class="kv"><span class="k">Water</span><span class="v">${r.water_share_amount === "" ? "—" : fmtMoney(r.water_share_amount)}</span></div>
              ${r.notes ? `<div class="kv"><span class="k warn-text">${esc(r.notes)}</span><span></span></div>` : ""}
            </div>`).join("")}
        </div>
      </div>`;
  } catch (e) {
    return `<div class="panel warn-text">${esc(month)}: ${esc(e.message)}</div>`;
  }
}
function meterLabel(id) {
  const m = state.meters.find((x) => x.meter_id == id);
  return m ? m.label : id;
}

/* =====================================================================
   SETTINGS
   ===================================================================== */
async function renderSettings() {
  setPage(`<h1 class="hide-mobile">Settings</h1><p>Loading…</p>`);
  try {
    const [settingsDict, backtest] = await Promise.all([
      api("/api/settings"), api("/api/pricing/backtest").catch(() => null),
    ]);
    state.settings = settingsDict;
    setPage(`
      <h1 class="hide-mobile">Settings</h1>
      <div class="panel">
        <form id="settings-form">
          <div class="grid-2">
            <label class="fl">Building name<input name="building_name" value="${esc(settingsDict.building_name || "")}"></label>
            <label class="fl">Currency symbol<input name="currency_symbol" value="${esc(settingsDict.currency_symbol || "Rs")}"></label>
          </div>
          <div class="grid-2">
            <label class="fl">Default margin %<input name="default_margin_pct" type="number" step="any" value="${esc(settingsDict.default_margin_pct || 7)}"></label>
            <label class="fl">Default water split
              <select name="default_water_mode">
                <option value="even" ${settingsDict.default_water_mode !== "proportional" ? "selected" : ""}>Equal</option>
                <option value="proportional" ${settingsDict.default_water_mode === "proportional" ? "selected" : ""}>By units</option>
              </select>
            </label>
          </div>
          <label class="fl">WhatsApp template name (fallback path)<input name="whatsapp_template_name" value="${esc(settingsDict.whatsapp_template_name || "monthly_bill")}"></label>
          <button class="btn btn-brass btn-inline" type="submit">Save</button>
        </form>
      </div>
      <div class="panel">
        <h3>Pricing model backtest</h3>
        ${backtest && backtest.months_tested ? `
          <p class="hint">Over <span class="tabular">${backtest.months_tested}</span> months ·
            never a loss: <strong>${backtest.never_a_loss ? "yes" : "no"}</strong> ·
            avg margin ${backtest.average_margin_pct}% · worst ${backtest.worst_margin_pct}%</p>` :
          `<p class="hint">Not enough history yet to backtest.</p>`}
      </div>
    `);
    document.getElementById("settings-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try {
        for (const [key, value] of fd.entries()) await api("/api/settings", { method: "POST", body: { key, value } });
        toast("Settings saved.");
        state.settings = await api("/api/settings");
      } catch (err) { toast(err.message, { error: true }); }
    });
  } catch (e) {
    setPage(`<h1 class="hide-mobile">Settings</h1><p class="warning-box">${esc(e.message)}</p>`);
  }
}

/* ------------------------------------------------------------------ boot -- */
(async function boot() {
  if (state.token) {
    try {
      await api("/api/auth/verify", { method: "POST" });
      showApp();
      route();
      return;
    } catch (e) { /* fall through */ }
  }
  showLogin();
})();
