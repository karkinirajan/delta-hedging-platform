// The dashboard: reads /api/analysis and /api/hedge/history with the bearer token and shows them.
// The token lives only in this variable. Every piece of text is set with textContent, never innerHTML.
(() => {
  const app = document.getElementById("app");
  if (!app) return;
  const $ = (id) => document.getElementById(id);
  let token = "";
  let analysis = null;
  let settings = null;

  const fmt = (v, digits = 4) => (typeof v === "number" ? v.toFixed(digits) : String(v ?? ""));
  const signed = (v, digits = 4) => (typeof v === "number" ? (v > 0 ? "+" : "") + v.toFixed(digits) : "");
  function el(name, text, className) {
    const node = document.createElement(name);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function say(text, ok = false) {
    const box = $("message");
    box.hidden = !text;
    box.textContent = text || "";
    box.className = "notice" + (ok ? " ok" : "");
  }
  async function api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { Accept: "application/json", Authorization: "Bearer " + token, ...(options.headers || {}) },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || "The request failed (HTTP " + response.status + ").");
      error.status = response.status;
      error.plan = data.plan;
      throw error;
    }
    return data;
  }

  function fillTable(table, head, rows) {
    const thead = table.querySelector("thead"), tbody = table.querySelector("tbody");
    thead.replaceChildren(); tbody.replaceChildren();
    const tr = el("tr");
    for (const h of head) { const th = el("th", h); th.scope = "col"; tr.append(th); }
    thead.append(tr);
    if (!rows.length) {
      const empty = el("tr"); const td = el("td", "None"); td.colSpan = head.length; empty.append(td); tbody.append(empty);
    }
    for (const cells of rows) {
      const row = el("tr");
      cells.forEach((cell, i) => {
        const td = i === 0 ? el("th") : el("td");
        if (i === 0) td.scope = "row";
        if (cell instanceof Node) td.append(cell); else td.textContent = cell;
        row.append(td);
      });
      tbody.append(row);
    }
  }
  const side = (direction) => el("span", direction, "tag " + direction);

  function summary(target, pairs) {
    target.replaceChildren();
    for (const [name, value] of pairs) {
      const box = el("div"); box.append(el("dt", name), el("dd", value)); target.append(box);
    }
  }

  function render() {
    const a = analysis, plan = a.plan;
    summary($("summary"), [
      ["Underlying price", fmt(a.spot, 2)], ["Market status", a.market_status],
      ["Option delta (units)", signed(a.option_delta)], ["Held in the underlying (units)", signed(a.held_units)],
      ["Net delta (units)", signed(plan.net_delta)], ["Updated", a.generated_at.replace("T", " ").slice(0, 19) + " UTC"],
    ]);
    const text = $("planText");
    text.replaceChildren();
    if (plan.action === "hedge") {
      // the reason is shown only when it adds something (an order cut down to the largest allowed size)
      text.append(el("span", "Recommended: ", "plan-hedge"), side(plan.direction), el("span", " " + plan.size + " of the underlying." + (plan.capped ? " " + plan.reason : "")));
    } else {
      text.append(el("span", (plan.action === "blocked" ? "Blocked: " : "No hedge needed: ") + plan.reason, plan.action === "blocked" ? "plan-hedge" : ""));
    }
    const canPlace = settings.hedging_enabled && plan.action === "hedge";
    $("placeBox").hidden = !canPlace;
    $("disabledNote").hidden = settings.hedging_enabled;
    $("confirmBox").checked = false; $("placeButton").disabled = true;
    if (canPlace) $("confirmLabel").textContent = "I confirm: " + plan.direction + " " + plan.size + " on " + settings.underlying_epic + " (" + settings.order_currency + ")";
    const warnings = $("warnings"); warnings.replaceChildren();
    for (const w of a.warnings) warnings.append(el("li", w));

    fillTable($("positions"), ["Position", "Side", "Type", "Strike", "Size", "Expiry", "Years", "Volatility", "Delta", "Position delta"],
      a.positions.map((p) => [p.name + " (" + p.deal_id + ")", side(p.direction === "BUY" ? "BUY" : "SELL"), p.option_type, fmt(p.strike, 2),
        fmt(p.size * p.contract_size, 2), p.expiry, fmt(p.years, 3), fmt(p.volatility, 3) + " " + p.volatility_source, fmt(p.greeks.delta), signed(p.delta_units)]));
    const unreadable = $("unreadable"); unreadable.replaceChildren();
    for (const u of a.unreadable) unreadable.append(el("p", "Cannot be read: " + (u.name || u.deal_id) + " (" + u.deal_id + "): " + u.reason, "notice"));
    for (const e of a.expired) unreadable.append(el("p", "Expired and left out: " + e.name + " (" + e.deal_id + "), " + e.expiry, "muted"));
    fillTable($("held"), ["Position", "Side", "Size", "Contract size", "Units"],
      a.hedge_positions.map((h) => [h.deal_id, side(h.direction), fmt(h.size, 2), fmt(h.contract_size, 2), signed(h.units, 2)]));
    summary($("settings"), [
      ["Environment", settings.environment], ["Hedging enabled", settings.hedging_enabled ? "yes" : "no (dry run)"], ["Underlying", settings.underlying_epic],
      ["Delta threshold", settings.delta_threshold], ["Order size limits", settings.min_hedge_size + " to " + settings.max_hedge_size],
      ["Plan tolerance", settings.plan_tolerance], ["Cooldown (s)", settings.cooldown_seconds], ["Risk-free rate", settings.rate], ["Default volatility", settings.default_volatility],
    ]);
    $("results").hidden = false;
  }

  async function renderHistory() {
    const { history } = await api(app.dataset.historyUrl);
    fillTable($("history"), ["Time", "Side", "Size", "Net delta before", "Outcome", "Reference"],
      history.map((h) => [h.time.replace("T", " ").slice(0, 19), side(h.direction), fmt(h.size, 2), signed(h.net_delta_before), h.outcome + (h.reason ? ": " + h.reason : ""), h.deal_reference]));
  }

  // keepMessage: after an order the result message stays on the page while the data is read again
  async function load(fresh = false, keepMessage = false) {
    try {
      const data = await api(app.dataset.analysisUrl + (fresh ? "?fresh=1" : ""));
      analysis = data.analysis; settings = data.settings;
      if (!keepMessage) say("");
      render();
      await renderHistory();
    } catch (error) {
      $("results").hidden = true;
      say(error.message);
    }
  }

  $("tokenForm").addEventListener("submit", (event) => { event.preventDefault(); token = $("token").value; load(); });
  $("confirmBox").addEventListener("change", () => { $("placeButton").disabled = !$("confirmBox").checked; });
  $("placeButton").addEventListener("click", async () => {
    const plan = analysis.plan;
    $("placeButton").disabled = true;
    try {
      const result = await api(app.dataset.hedgeUrl, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true, expected_direction: plan.direction, expected_size: plan.size }),
      });
      say(result.executed ? "Order " + result.outcome + (result.reason ? ": " + result.reason : "") + " (reference " + result.deal_reference + ")." : "Nothing was sent: " + result.plan.reason, result.outcome === "accepted");
    } catch (error) {
      say(error.message + (error.plan ? " New plan: " + (error.plan.direction || error.plan.action) + " " + error.plan.size + ". Review it and confirm again." : ""));
    }
    await load(true, true);
  });
})();
