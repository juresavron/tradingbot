"use strict";

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const STRATEGIES = {
  donchian_breakout: { name: "Donchian breakout", params: { entry_period: 120, exit_period: 60 } },
  sma_cross: { name: "SMA crossover", params: { fast: 20, slow: 50 } },
  rsi_revert: { name: "RSI reversion", params: { period: 14, buy_below: 30, exit_above: 55 } },
};
const TERMINAL = new Set(["FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"]);
const REASONS = {
  paused: "Entries paused.",
  started: "Entry permission requested. Waiting for the next cycle.",
  flatten_requested: "Close requested; new entries are paused.",
  entries_paused: "Entry signal observed; new entries are paused.",
  strategy_long: "Strategy targets a long position, including an ongoing signal.",
  strategy_flat: "Strategy targets cash.",
  already_flat: "No position is open.",
  blocked_no_fresh_news: "Entry blocked: no fresh news is available.",
  paused_before_submission: "Entry paused before order submission.",
  position_or_budget_below_minimum_order: "Position size or budget is below the minimum order.",
  kill_switch: "Drawdown limit reached. A position close was requested.",
  demo_replay_complete: "Demo replay finished. Create a new simulation to replay again.",
  market_not_observed: "Waiting for the first market observation.",
  market_data_stale: "Market data is stale.",
  market_stale: "Market data is stale.",
  market_data_unavailable: "Market data is unavailable.",
  worker_not_running: "The trading worker is not running.",
  worker_stopped: "The trading worker is not running.",
  worker_disabled: "The trading worker is disabled.",
  worker_unavailable: "The trading worker is unavailable.",
  heartbeat_stale: "The trading worker heartbeat is stale.",
  storage_unavailable: "Account storage is unavailable.",
  automation_halted: "Simulation stopped. A new simulation is required.",
  worker_error: "The worker reported an error.",
  pipeline_error: "The execution pipeline is blocked.",
  execution_unresolved: "An order outcome is unresolved; new entries are blocked.",
  news_unavailable: "Fresh news is required but unavailable.",
  price_event_unknown: "The exchange price timestamp is unavailable.",
  market_quote_stale: "The last trade or its receipt is too old; waiting for a fresh observation.",
  market_quote_unknown_time: "The exchange price timestamp is unavailable.",
  market_quote_clock_skew: "The price timestamp disagrees with the worker clock.",
  market_quote_out_of_order: "An older price observation was rejected.",
  market_request_timeout: "Market request timed out; waiting for a fresh observation.",
  market_request_pending: "A timed-out market request is still finishing; new requests are bounded.",
  market_cycle_deadline: "The market observation budget expired.",
  market_quote_expired_before_submission: "The price observation expired before submission; entry was canceled.",
};
let snapshot = null;
let signedInEmail = null;
let pollTimer = null;
let refreshRequest = null;
let requestSequence = 0;
let appliedSequence = 0;
let sessionEpoch = 0;
const pendingRequests = new Set();
let toastTimer = null;

function element(tag, className = "", text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}
function svgElement(tag, attributes = {}) {
  const result = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attributes)) result.setAttribute(key, String(value));
  return result;
}
function finite(value) { return value !== null && value !== undefined && Number.isFinite(Number(value)); }
function number(value, digits = 8) {
  return finite(value) ? new Intl.NumberFormat("en-GB", { maximumFractionDigits: digits }).format(Number(value)) : "—";
}
function money(value, digits = 2) {
  if (!finite(value)) return "—";
  const currency = snapshot?.portfolio?.quote_asset || "EUR";
  try { return new Intl.NumberFormat("en-IE", { style: "currency", currency, minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(value)); }
  catch { return Number(value).toFixed(digits) + " " + currency; }
}
function percent(value, signed = false) {
  if (!finite(value)) return "—";
  const n = Number(value);
  return (signed && n > 0 ? "+" : "") + n.toFixed(2) + "%";
}
function timestamp(value, date = false) {
  if (!finite(value) || Number(value) <= 0) return "Not observed";
  return new Intl.DateTimeFormat("en-GB", { ...(date ? { day: "2-digit", month: "short" } : {}), hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(Number(value)));
}
function age(value) {
  if (!finite(value) || Number(value) <= 0) return "not observed";
  const seconds = Math.max(0, Math.floor((Date.now() - Number(value)) / 1000));
  if (seconds < 60) return seconds + "s ago";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
  return Math.floor(seconds / 86400) + "d ago";
}
function reason(value) {
  if (Object.hasOwn(REASONS, value)) return REASONS[value];
  if (typeof value === "string" && /^[a-z][a-z0-9_]+$/.test(value)) return value[0].toUpperCase() + value.slice(1).replaceAll("_", " ") + ".";
  return value || "No reason recorded.";
}
function empty(message, note) {
  const result = element("div", "empty-state");
  result.append(element("p", "", message));
  if (note) result.append(element("span", "", note));
  return result;
}
function safeLink(value) {
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : null; }
  catch { return null; }
}

async function api(path, body) {
  const epoch = sessionEpoch;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 45000);
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", signal: controller.signal,
      ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Server response timed out. Check the latest account state before repeating an action.");
    throw new Error("The server could not be reached.");
  } finally { clearTimeout(timeout); }
  let result;
  try { result = await response.json(); } catch { result = null; }
  if (response.status === 401 && path !== "/api/login") {
    if (epoch === sessionEpoch) showLogin();
    throw new Error("Session expired. Sign in again.");
  }
  if (!response.ok) {
    const detail = result?.detail || result?.error;
    throw new Error(typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((item) => item.msg || "Invalid input").join("; ") : "Request failed (" + response.status + ").");
  }
  return result;
}
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }
function showLogin() {
  stopPolling();
  sessionEpoch += 1;
  snapshot = null;
  signedInEmail = null;
  appliedSequence = 0;
  pendingRequests.clear();
  $("boot-screen").hidden = true;
  $("app-shell").hidden = true;
  $("login-screen").hidden = false;
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  $("login-email").focus();
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").className = "toast" + (error ? " error" : "");
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 9000 : 5000);
}
function connection(online, message = "") {
  $("connection-label").textContent = online ? "API reachable" : "API unavailable";
  $("connection-label").className = online ? "" : "offline";
  $("connection-error").hidden = online;
  $("connection-error").textContent = online ? "" : message + " Displayed values may be stale. Reconnecting automatically.";
  if (!online) {
    $("engine-state").textContent = "Status unavailable";
    $("state-dot").className = "state-dot warning";
    $("worker-health").textContent = "Worker not confirmed";
    $("worker-health").className = "health-warning";
  }
}
function syncButtons() {
  const busy = pendingRequests.size > 0;
  const protectivePending = Array.from(pendingRequests).some((request) => ["pause", "flatten"].includes(request.kind));
  const entries = Boolean(snapshot?.readiness?.entries_requested ?? snapshot?.status?.desired_running ?? snapshot?.status?.running);
  const closePending = Boolean(snapshot?.status?.flatten_requested);
  for (const id of ["step-bot", "config-submit", "new-simulation", "settings-edit", "refresh-news"]) $(id).disabled = busy;
  // Slow market/news requests must never lock the operator out of a stop.
  $("toggle-bot").disabled = closePending || (entries ? protectivePending : busy || snapshot?.status?.state === "halted");
  $("flatten-bot").disabled = protectivePending || closePending;
  $("confirm-flatten").disabled = protectivePending || closePending;
  $("app-shell").setAttribute("aria-busy", String(busy));
}
function requestContext(kind) {
  return { kind, sequence: ++requestSequence, epoch: sessionEpoch };
}
function beginRequest(kind) {
  const protective = ["pause", "flatten"].includes(kind);
  if (protective) {
    if (snapshot?.status?.flatten_requested || Array.from(pendingRequests).some((request) => ["pause", "flatten"].includes(request.kind))) return null;
  } else if (pendingRequests.size || (kind === "start" && (snapshot?.status?.flatten_requested || snapshot?.status?.state === "halted"))) return null;
  const request = requestContext(kind);
  pendingRequests.add(request);
  syncButtons();
  return request;
}
function finishRequest(request) {
  pendingRequests.delete(request);
  syncButtons();
}
function currentResponse(request) {
  return request.epoch === sessionEpoch && request.sequence >= appliedSequence;
}
function acceptSnapshot(data, request) {
  if (!data || request.epoch !== sessionEpoch) return false;
  const revision = data.revision;
  const previousRevision = snapshot?.revision;
  const sameRevision = revision === previousRevision;
  if (finite(revision) && finite(previousRevision) && Number(revision) < Number(previousRevision)) return false;
  if (sameRevision) {
    const generation = data.status?.control_generation;
    const previousGeneration = snapshot?.status?.control_generation;
    if (finite(generation) && finite(previousGeneration)) {
      if (Number(generation) < Number(previousGeneration)) return false;
      if (Number(generation) === Number(previousGeneration) && request.sequence < appliedSequence) return false;
    } else if (request.sequence < appliedSequence) return false;
  } else if (!(finite(revision) && finite(previousRevision) && Number(revision) > Number(previousRevision)) && request.sequence < appliedSequence) return false;
  // Control generations are authoritative even when a later-started poll
  // delivered an earlier generation before this control acknowledgement.
  appliedSequence = Math.max(appliedSequence, request.sequence);
  render(data);
  connection(true);
  return true;
}
async function refresh() {
  if (refreshRequest?.epoch === sessionEpoch) return;
  const request = requestContext("poll");
  refreshRequest = request;
  try { acceptSnapshot(await api("/api/state"), request); }
  catch (error) { if (currentResponse(request)) connection(false, error.message); }
  finally { if (refreshRequest === request) refreshRequest = null; }
}
async function enterApp(session) {
  signedInEmail = session?.email || null;
  $("boot-screen").hidden = true;
  $("login-screen").hidden = true;
  $("app-shell").hidden = false;
  await refresh();
  stopPolling();
  if (!$("app-shell").hidden) pollTimer = setInterval(refresh, 3000);
}
async function bootstrap() {
  $("retry-connection").hidden = true;
  try { const session = await api("/api/session"); if (session.authenticated) await enterApp(session); else showLogin(); }
  catch (error) { $("boot-screen").querySelector("p").textContent = error.message; $("retry-connection").hidden = false; }
}

function renderHealth(status, readiness) {
  const entries = Boolean(readiness?.entries_requested ?? status.desired_running ?? status.running);
  const closePending = Boolean(status.flatten_requested);
  const noObservation = !finite(status.price) && !entries && status.state !== "error" && status.state !== "halted";
  const workerStopped = readiness?.worker_alive === false || status.worker_alive === false;
  const workerDisabled = status.worker_enabled === false;
  const staleHeartbeat = Boolean(status.heartbeat_stale);
  let worker = workerDisabled ? "Worker disabled" : workerStopped ? "Worker stopped" : staleHeartbeat ? "Heartbeat stale" : (status.worker_alive === true || readiness?.worker_alive === true) ? "Worker ready" : "Worker status unknown";
  let warning = "";
  if (workerDisabled || workerStopped) warning = "The worker is unavailable. Orders and risk exits cannot be processed automatically.";
  else if (status.worker_error) warning = reason(status.worker_error);
  else if (staleHeartbeat) warning = "The worker heartbeat is stale. Automatic execution has not been confirmed.";
  else if (status.error) warning = reason(status.error);
  else if (readiness && !readiness.ready) {
    const blockers = (readiness.blocking_reasons || []).filter((item) => !(noObservation && item === "market_not_observed"));
    if (blockers.length) warning = blockers.map(reason).join(" ");
  }
  let label = closePending ? "Close requested" + (warning ? " · waiting" : "") : status.state === "halted" ? "Stopped" : noObservation ? "Paused · awaiting first observation" : status.state === "error" ? "Needs attention" : entries ? (warning ? "Entries requested · waiting" : "Entries enabled") : "Entries paused";
  $("engine-state").textContent = label;
  $("state-dot").className = "state-dot" + (warning || status.state === "error" ? " warning" : status.state === "halted" ? " error" : entries ? " running" : "");
  $("worker-health").textContent = worker;
  $("worker-health").className = warning ? "health-warning" : "";
  $("worker-warning").hidden = !warning;
  $("worker-warning").textContent = warning;
  $("toggle-label").textContent = closePending ? "Close pending" : entries ? "Pause entries" : "Start bot";
  $("toggle-bot").dataset.action = entries ? "pause" : "start";
  $("flatten-bot").textContent = closePending ? "Close requested" : "Flatten & pause";
  $("engine-reason").textContent = closePending ? "Close pending. Remaining position: " + number(snapshot.portfolio?.quantity) + " " + (snapshot.portfolio?.base_asset || "") + ". Awaiting usable prices and confirmed fills." : status.last_reason ? "Last action: " + reason(status.last_reason) : "No cycle recorded yet.";
  syncButtons();
}

function render(data) {
  snapshot = data;
  const config = data.config || {}, portfolio = data.portfolio || {}, status = data.status || {};
  const source = status.source || config.source;
  $("source-badge").textContent = source === "demo" ? "Demo replay" : source === "binance" ? "Binance public" : "No data source";
  $("source-notice").textContent = source === "demo" ? "Demo candles. All orders and funds are simulated." : source === "binance" ? (status.error || !finite(status.price) ? "Binance selected; waiting for valid market data. Orders remain simulated." : "Binance public prices. All orders and funds are simulated.") : "No market source connected. Orders and funds are simulated.";
  $("account-identity").textContent = signedInEmail || (data.revision ? "Simulation " + data.revision : "Simulation account");
  $("metric-equity").textContent = money(portfolio.equity);
  $("metric-return").textContent = percent(portfolio.return_pct, true);
  $("metric-return").className = Number(portfolio.return_pct) > 0 ? "positive" : Number(portfolio.return_pct) < 0 ? "negative" : "neutral";
  $("metric-cash").textContent = money(portfolio.cash);
  $("position-quantity").textContent = number(portfolio.quantity) + (portfolio.base_asset ? " " + portfolio.base_asset : "");
  $("allocation-asset").textContent = money(portfolio.position_value);
  $("metric-drawdown").textContent = percent(portfolio.drawdown_pct);
  $("risk-limit").textContent = "of " + percent(Number(config.max_drawdown) * 100) + " limit";
  $("engine-strategy").textContent = (STRATEGIES[config.strategy]?.name || config.strategy || "No strategy") + " · " + (config.symbol || "—") + " · " + (config.timeframe || "—");
  $("market-age").textContent = source === "demo" && status.market_time_ms ? "Replay · " + timestamp(status.market_time_ms, true) : status.price_event_ms ? "Last exchange trade " + age(status.price_event_ms) : "Exchange trade age unknown";
  $("market-age").title = status.quote_received_ms ? "Received " + new Date(Number(status.quote_received_ms)).toISOString() : "No timestamped quote received";
  $("last-updated").textContent = "Updated " + timestamp(Date.now());
  renderHealth(status, data.readiness);
  renderChart(data.equity || []);
  renderOrders(data.orders || [], data.metrics || {});
  renderDecisions(data.decisions || []);
  renderEvents(data.events || []);
  renderNews(data.news || [], data.news_source || {}, config.news_policy);
  renderConfig(config, data.readiness, status);
}

function renderChart(records) {
  const points = records.filter((item) => finite(item.at_ms) && finite(item.equity)).slice().sort((a, b) => a.at_ms - b.at_ms);
  $("chart-points").textContent = points.length + " recent observation" + (points.length === 1 ? "" : "s");
  $("chart-coverage").textContent = points.length > 1 ? timestamp(points[0].at_ms, true) + " — " + timestamp(points[points.length - 1].at_ms, true) : "Recent observations";
  if (points.length < 2) { $("equity-chart").replaceChildren(empty("No equity history yet", "Recorded account values will appear after a few cycles.")); return; }
  const W = Math.max(280, $("equity-chart").clientWidth), H = $("equity-chart").clientHeight || 225;
  const L = 3, R = 62, T = 8, B = 27;
  const xs = points.map((item) => Number(item.at_ms)), ys = points.map((item) => Number(item.equity));
  const min = Math.min(...ys), max = Math.max(...ys), padding = Math.max((max - min) * .2, Math.abs(max) * .002, .02);
  const low = min - padding, high = max + padding;
  const x = (v) => L + (v - xs[0]) / (xs[xs.length - 1] - xs[0] || 1) * (W - L - R);
  const y = (v) => T + (high - v) / (high - low) * (H - T - B);
  const svg = svgElement("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${points.length} recent recorded equity values, from ${money(ys[0])} to ${money(ys[ys.length - 1])}` });
  for (let i = 0; i < 3; i++) {
    const vertical = T + i / 2 * (H - T - B);
    svg.append(svgElement("line", { x1: L, y1: vertical, x2: W - R, y2: vertical, stroke: "#edf0ed", "stroke-width": "1" }));
    const label = svgElement("text", { x: W - R + 12, y: vertical + 3 });
    label.textContent = money(high - i / 2 * (high - low), high - low < 10 ? 2 : 0); svg.append(label);
  }
  const path = points.map((item, i) => (i ? "L" : "M") + x(Number(item.at_ms)).toFixed(2) + " " + y(Number(item.equity)).toFixed(2)).join(" ");
  svg.append(svgElement("path", { d: path, fill: "none", stroke: "#397860", "stroke-width": "1.7", "stroke-linejoin": "round", "stroke-linecap": "round" }));
  svg.append(svgElement("circle", { cx: x(xs[xs.length - 1]), cy: y(ys[ys.length - 1]), r: "2.6", fill: "#397860" }));
  const labels = W < 500 ? 2 : 3;
  for (let i = 0; i < labels; i++) {
    const index = Math.round(i / (labels - 1) * (points.length - 1));
    const label = svgElement("text", { x: x(xs[index]), y: H - 5, "text-anchor": i === 0 ? "start" : i === labels - 1 ? "end" : "middle" });
    label.textContent = timestamp(xs[index], xs[xs.length - 1] - xs[0] > 86400000); svg.append(label);
  }
  $("equity-chart").replaceChildren(svg);
}

function renderOrders(items, metrics) {
  const orders = items.slice().sort((a, b) => b.created_at_ms - a.created_at_ms);
  const fills = orders.filter((item) => item.status === "FILLED").slice(0, 5);
  $("orders-empty").hidden = fills.length > 0;
  $("orders-body").replaceChildren(...fills.map((item) => {
    const intent = item.intent || {}, fill = item.snapshot || {};
    const quantity = finite(fill.executed_quantity) ? Number(fill.executed_quantity) : null;
    const total = finite(fill.cumulative_quote_quantity) ? Number(fill.cumulative_quote_quantity) : null;
    const average = quantity > 0 && total !== null ? total / quantity : null;
    const row = element("tr"), trade = element("td");
    trade.append(element("span", "trade-side " + String(intent.side).toLowerCase(), intent.side === "BUY" ? "Buy" : "Sell"), element("span", "trade-asset", snapshot.portfolio?.base_asset || intent.symbol || ""));
    row.append(element("td", "", timestamp(fill.updated_at_ms || item.created_at_ms, true)), trade, element("td", "numeric", number(quantity)), element("td", "numeric", money(average)), element("td", "numeric", money(total)));
    return row;
  }));
  const unresolved = orders.filter((item) => !TERMINAL.has(item.status));
  $("pending-orders").hidden = unresolved.length === 0;
  $("pending-orders").textContent = unresolved.length + " order" + (unresolved.length === 1 ? " has" : "s have") + " an unresolved outcome. See order history in Details.";
  $("order-count").textContent = number(metrics.n_orders ?? orders.length, 0) + " total orders · latest " + Math.min(orders.length, 50) + " shown";
  $("ledger-empty").hidden = orders.length > 0;
  $("ledger-body").replaceChildren(...orders.slice(0, 50).map((item) => {
    const row = element("tr"), intent = item.intent || {}, status = String(item.status || "UNKNOWN"), stateCell = element("td");
    const style = ["REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"].includes(status) ? " error" : !TERMINAL.has(status) ? " warning" : "";
    stateCell.append(element("span", "status-tag" + style, status.replaceAll("_", " ")));
    row.append(element("td", "", timestamp(item.created_at_ms, true)), element("td", "", (intent.side || "—") + " · " + (intent.symbol || "—")), element("td", "numeric", number(intent.quantity)), element("td", "numeric", number(item.snapshot?.executed_quantity)), element("td", "numeric", money(intent.limit_price)), stateCell);
    row.title = "Order " + (item.client_order_id || item.request_id || "identifier unavailable");
    return row;
  }));
}

function renderDecisions(items) {
  if (!items.length) { $("decision-feed").replaceChildren(element("p", "detail-empty", "No decisions recorded.")); return; }
  $("decision-feed").replaceChildren(...items.slice().sort((a, b) => b.at_ms - a.at_ms).slice(0, 15).map((item) => {
    const row = element("article", "decision-item"), date = element("time", "", timestamp(item.at_ms, true)), action = String(item.action || "HOLD");
    if (finite(item.at_ms)) date.dateTime = new Date(Number(item.at_ms)).toISOString();
    const copy = element("div"); copy.append(element("p", "", reason(item.reason)));
    if (finite(item.price)) copy.append(element("span", "decision-meta", money(item.price) + (item.news_ids?.length ? " · " + item.news_ids.length + " news item(s)" : "")));
    row.append(date, element("span", "decision-action " + action.toLowerCase(), action), copy); return row;
  }));
}

function renderEvents(items) {
  $("event-feed").replaceChildren(...(items.length ? items.slice(0, 20).map((item) => {
    const row = element("article", "decision-item");
    row.append(element("time", "", timestamp(item.at_ms, true)),
      element("span", "decision-action", item.kind === "recovered" ? "RESOLVED" : "ERROR"),
      element("p", "", reason(item.detail?.code || item.detail?.previous_error || item.kind)));
    return row;
  }) : [element("p", "detail-empty", "No operational errors recorded.")]));
}

function renderNews(items, source, policy) {
  $("news-source-status").textContent = !source.configured ? "No RSS or Apify source connected." : source.error ? "Feed error: " + source.error : source.last_success_ms ? "Last successful refresh " + age(source.last_success_ms) + "." : "Feed configured; not refreshed yet.";
  $("news-policy-label").textContent = policy === "require-fresh" ? "Fresh news is required before an entry. The strategy uses rules, not an AI decision model." : "News is context only. The strategy uses rules, not an AI decision model.";
  if (!items.length) { $("news-grid").replaceChildren(element("p", "detail-empty", "No articles received.")); return; }
  $("news-grid").replaceChildren(...items.slice().sort((a, b) => b.published_at_ms - a.published_at_ms).slice(0, 6).map((item) => {
    const row = element("article", "news-item"), url = safeLink(item.url);
    const title = element(url ? "a" : "span", "", item.title || "Untitled article");
    if (url) { title.href = url; title.target = "_blank"; title.rel = "noopener noreferrer"; }
    row.append(title, element("p", "", (item.source || "Source unavailable") + " · " + age(item.published_at_ms))); return row;
  }));
}

function renderConfig(config, readiness, status) {
  const rows = [
    ["Account", snapshot.account_id || "—"],
    ["Strategy parameters", JSON.stringify(config.params || {})],
    ["Entry policy", "Join the current target signal, including one that began before startup"],
    ["Quote source", status.quote_source || (config.source === "demo" ? "Historical demo fixture" : "Not observed")],
    ["Quote received", status.quote_received_ms ? age(status.quote_received_ms) : "Not recorded"],
    ["Starting cash", money(config.starting_cash)],
    ["Maximum order", money(config.max_order_notional)],
    ["Cash per entry", percent(Number(config.position_fraction) * 100)],
    ["Drawdown limit", percent(Number(config.max_drawdown) * 100)],
    ["Simulated fee per fill", percent(Number(config.fee ?? .001) * 100)],
    ["Assumed slippage per fill", percent(Number(config.slippage ?? 0) * 100)],
    ["Fees charged", money(snapshot.metrics?.fees_paid ?? 0)],
    ["Modeled slippage cost", money(snapshot.metrics?.slippage_paid ?? 0)],
    ["Last worker heartbeat", status.heartbeat_ms ? age(status.heartbeat_ms) : "Not recorded"],
    ["Pipeline", readiness ? readiness.ready ? "Ready · simulation only" : (readiness.blocking_reasons || []).map(reason).join(" ") || "Not ready" : "Readiness not reported"],
  ];
  $("config-summary").replaceChildren(...rows.map(([label, value]) => {
    const row = element("div", "config-item"); row.append(element("dt", "", label), element("dd", "", value)); return row;
  }));
}

async function control(action) {
  const request = beginRequest(action);
  if (!request) return;
  try {
    const result = await api("/api/control", { action });
    if (!acceptSnapshot(result, request)) return;
    if (action === "flatten") toast("Close requested; entries paused. Remaining position: " + number(result.portfolio?.quantity) + " " + (result.portfolio?.base_asset || "") + ".");
    else if (action === "pause") toast("Entries paused.");
    else if (action === "start") toast("Entry permission requested. Check the bot status for any blockers.");
    else toast(result.status?.error ? reason(result.status.error) : "Cycle checked. " + reason(result.status?.last_reason), Boolean(result.status?.error));
  } catch (error) { if (currentResponse(request)) toast(error.message, true); }
  finally { finishRequest(request); }
}
function openSettings() {
  if (pendingRequests.size) return;
  const form = $("config-form"), config = snapshot?.config || {};
  const defaults = { source: "demo", symbol: "BTC/EUR", timeframe: "4h", strategy: "donchian_breakout", starting_cash: 300, max_order_notional: 75, position_fraction: .25, max_drawdown: .1, news_policy: "observe", fee: .001, slippage: 0 };
  for (const [key, fallback] of Object.entries(defaults)) {
    const input = form.elements[key], value = config[key] ?? fallback;
    if (input.tagName === "SELECT" && !Array.from(input.options).some((item) => item.value === String(value))) {
      const option = element("option", "", String(value)); option.value = String(value); input.append(option);
    }
    input.value = ["position_fraction", "max_drawdown", "fee", "slippage"].includes(key) ? Math.round(Number(value) * 10000) / 100 : value;
  }
  form.elements.params.value = JSON.stringify(config.params || STRATEGIES[form.elements.strategy.value]?.params || {}, null, 2);
  $("config-error").textContent = "";
  $("config-dialog").showModal();
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("login-submit").disabled = true; $("login-error").textContent = "";
  try {
    const session = await api("/api/login", { email: $("login-email").value.trim(), password: $("login-password").value });
    $("login-password").value = "";
    await enterApp(session);
  }
  catch (error) { $("login-error").textContent = error.message; }
  finally { $("login-submit").disabled = false; }
});
$("logout").addEventListener("click", async () => {
  try { await api("/api/logout", {}); snapshot = null; showLogin(); }
  catch (error) { toast(error.message, true); }
});
$("retry-connection").addEventListener("click", bootstrap);
$("toggle-bot").addEventListener("click", () => control($("toggle-bot").dataset.action));
$("step-bot").addEventListener("click", () => control("step"));
$("flatten-bot").addEventListener("click", () => $("flatten-dialog").showModal());
$("confirm-flatten").addEventListener("click", () => { $("flatten-dialog").close(); control("flatten"); });
$("new-simulation").addEventListener("click", openSettings);
$("settings-edit").addEventListener("click", openSettings);
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => $(button.dataset.close).close());
$("config-form").elements.strategy.addEventListener("change", (event) => { $("config-form").elements.params.value = JSON.stringify(STRATEGIES[event.target.value]?.params || {}, null, 2); });
$("config-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (pendingRequests.size) return;
  const form = event.currentTarget; $("config-error").textContent = "";
  let params;
  try { params = JSON.parse(form.elements.params.value); if (!params || typeof params !== "object" || Array.isArray(params)) throw new Error("Use a JSON object."); }
  catch (error) { $("config-error").textContent = "Invalid strategy parameters. " + error.message; return; }
  const body = { params };
  for (const key of ["source", "symbol", "timeframe", "strategy", "news_policy"]) body[key] = form.elements[key].value;
  for (const key of ["starting_cash", "max_order_notional", "position_fraction", "max_drawdown", "fee", "slippage"]) body[key] = Number(form.elements[key].value) / (["position_fraction", "max_drawdown", "fee", "slippage"].includes(key) ? 100 : 1);
  const request = beginRequest("config");
  if (!request) return;
  // Release the modal's focus trap while the server is busy so the operator
  // can still pause entries or request a close from the main controls.
  $("config-dialog").close();
  try {
    const accepted = acceptSnapshot(await api("/api/config", body), request);
    if (request.epoch !== sessionEpoch) return;
    if (accepted) toast("New simulation created. Previous account records were retained.");
  } catch (error) { if (currentResponse(request)) toast(error.message, true); }
  finally { finishRequest(request); }
});
$("refresh-news").addEventListener("click", async () => {
  const request = beginRequest("news");
  if (!request) return;
  try {
    const result = await api("/api/news/sync", {});
    if (result.state ? !acceptSnapshot(result.state, request) : !currentResponse(request)) return;
    toast(result.news_source?.error || "News refreshed.", Boolean(result.news_source?.error));
  } catch (error) { if (currentResponse(request)) toast(error.message, true); }
  finally { finishRequest(request); }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("app-shell").hidden) refresh(); });
window.addEventListener("pagehide", stopPolling);
window.addEventListener("resize", () => { if (snapshot && !$("app-shell").hidden) renderChart(snapshot.equity || []); });
bootstrap();
