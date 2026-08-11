/* tradingbot dashboard — plain JS + supabase-js (CDN). No build step. */
"use strict";

const cfg = window.TRADINGBOT_CONFIG || {};
const configured =
  cfg.SUPABASE_URL && cfg.SUPABASE_URL.startsWith("https://") &&
  cfg.SUPABASE_ANON_KEY && !cfg.SUPABASE_ANON_KEY.startsWith("YOUR_");

const $ = (id) => document.getElementById(id);
const show = (el, on) => el.classList.toggle("hidden", !on);

if (!configured) {
  show($("view-setup"), true);
} else {
  main();
}

function main() {
  const db = supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
  let runs = [];
  let selectedId = null;
  let pollTimer = null;

  /* ---------------- auth ---------------- */

  db.auth.onAuthStateChange((_event, session) => {
    show($("view-auth"), !session);
    show($("view-app"), !!session);
    if (session) {
      $("user-email").textContent = session.user.email;
      refreshRuns();
      startPolling();
    } else {
      stopPolling();
    }
  });

  $("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("auth-message").textContent = "";
    const { error } = await db.auth.signInWithPassword({
      email: $("auth-email").value,
      password: $("auth-password").value,
    });
    if (error) $("auth-message").textContent = error.message;
  });

  $("btn-signup").addEventListener("click", async () => {
    $("auth-message").textContent = "";
    const email = $("auth-email").value;
    const password = $("auth-password").value;
    if (!email || password.length < 6) {
      $("auth-message").textContent = "Enter an email and a password of at least 6 characters.";
      return;
    }
    const { error } = await db.auth.signUp({ email, password });
    $("auth-message").textContent = error
      ? error.message
      : "Account created. If a confirmation email arrives, click it first — then sign in.";
  });

  $("btn-signout").addEventListener("click", () => db.auth.signOut());

  /* ---------------- runs list ---------------- */

  async function refreshRuns() {
    const { data, error } = await db
      .from("runs")
      .select("*")
      .order("created_at", { ascending: false });
    if (error) return console.error(error);
    runs = data;
    renderRunsList();
    if (selectedId && !runs.some((r) => r.id === selectedId)) selectRun(null);
    if (selectedId) refreshDetail();
  }

  function renderRunsList() {
    const ul = $("runs-list");
    ul.innerHTML = "";
    show($("runs-empty"), runs.length === 0);
    for (const run of runs) {
      const li = document.createElement("li");
      li.className = run.id === selectedId ? "active" : "";
      const meta = [run.symbol, run.timeframe, statusOf(run).label]
        .filter(Boolean)
        .join(" · ");
      li.innerHTML = `<span class="run-name"></span><span class="run-meta"></span>`;
      li.querySelector(".run-name").textContent = run.name;
      li.querySelector(".run-meta").textContent = meta;
      li.addEventListener("click", () => selectRun(run.id));
      ul.appendChild(li);
    }
  }

  function statusOf(run) {
    if (run.killed) return { label: "KILLED", cls: "killed" };
    if (!run.last_seen_at) return { label: "WAITING FOR BOT", cls: "stale" };
    const ageMin = (Date.now() - new Date(run.last_seen_at).getTime()) / 60000;
    return ageMin <= 10
      ? { label: "LIVE", cls: "live" }
      : { label: "STALE", cls: "stale" };
  }

  function selectRun(id) {
    selectedId = id;
    renderRunsList();
    show($("run-detail"), !!id);
    show($("run-none"), !id);
    if (id) refreshDetail();
  }

  /* ---------------- run detail ---------------- */

  async function refreshDetail() {
    const run = runs.find((r) => r.id === selectedId);
    if (!run) return;

    const [equityRes, tradesRes] = await Promise.all([
      db.from("equity_points").select("ts,equity,price")
        .eq("run_id", run.id).order("ts", { ascending: false }).limit(2000),
      db.from("trades").select("*")
        .eq("run_id", run.id).order("seq", { ascending: false }).limit(200),
    ]);
    if (equityRes.error || tradesRes.error) {
      return console.error(equityRes.error || tradesRes.error);
    }
    const points = equityRes.data.slice().reverse();
    const trades = tradesRes.data;

    $("run-title").textContent = run.name;
    $("run-sub").textContent = [run.symbol, run.timeframe, run.strategy]
      .filter(Boolean).join(" · ");
    const st = statusOf(run);
    const badge = $("run-badge");
    badge.textContent = st.label;
    badge.className = `badge ${st.cls}`;

    renderStats(run, points, trades);
    renderChart(points, Number(run.starting_cash));
    renderTrades(trades);
  }

  function renderStats(run, points, trades) {
    const start = Number(run.starting_cash) || 100;
    const last = points.length ? Number(points[points.length - 1].equity) : start;
    const ret = last / start - 1;

    const priced = points.filter((p) => p.price != null);
    let holdText = "—";
    if (priced.length >= 2) {
      const hold = Number(priced[priced.length - 1].price) / Number(priced[0].price) - 1;
      holdText = fmtPct(hold);
    }

    const sells = trades.filter((t) => t.side === "SELL" && t.pnl != null);
    const wins = sells.filter((t) => Number(t.pnl) > 0).length;
    const winRate = sells.length ? ` · win ${Math.round((wins / sells.length) * 100)}%` : "";

    const seen = run.last_seen_at
      ? relTime(new Date(run.last_seen_at))
      : "never — start the bot";

    $("stats").innerHTML = "";
    addStat("Equity", `$${last.toFixed(2)}`, "", `started $${start.toFixed(2)}`);
    addStat("Return", fmtPct(ret), ret >= 0 ? "pos" : "neg", "after simulated costs");
    addStat("Buy & hold", holdText, "", "same period, before costs");
    addStat("Trades", String(trades.length), "", `${sells.length} closed${winRate}`);
    addStat("Last heartbeat", seen, "", run.killed ? "kill switch fired" : "");
  }

  function addStat(label, value, valueClass, sub) {
    const div = document.createElement("div");
    div.className = "stat";
    div.innerHTML = `<div class="label"></div><div class="value ${valueClass}"></div><div class="sub"></div>`;
    div.querySelector(".label").textContent = label;
    div.querySelector(".value").textContent = value;
    div.querySelector(".sub").textContent = sub;
    $("stats").appendChild(div);
  }

  /* ---------------- chart (hand-rolled SVG) ---------------- */

  function renderChart(points, startingCash) {
    const box = $("chart");
    if (points.length < 2) {
      box.innerHTML = `<p class="muted">Not enough data yet — the bot logs a point at every candle close.</p>`;
      $("chart-span").textContent = "";
      return;
    }
    const W = 800, H = 260, padL = 56, padR = 12, padT = 12, padB = 24;
    const xs = points.map((p) => Number(p.ts));
    const ys = points.map((p) => Number(p.equity));
    const xMin = xs[0], xMax = xs[xs.length - 1];
    const yMin = Math.min(...ys, startingCash) * 0.995;
    const yMax = Math.max(...ys, startingCash) * 1.005;
    const X = (t) => padL + ((t - xMin) / (xMax - xMin || 1)) * (W - padL - padR);
    const Y = (v) => padT + (1 - (v - yMin) / (yMax - yMin || 1)) * (H - padT - padB);

    let line = "";
    for (let i = 0; i < points.length; i++) {
      line += `${i ? "L" : "M"}${X(xs[i]).toFixed(1)},${Y(ys[i]).toFixed(1)}`;
    }
    const area = `${line}L${X(xMax).toFixed(1)},${Y(yMin).toFixed(1)}L${X(xMin).toFixed(1)},${Y(yMin).toFixed(1)}Z`;
    const up = ys[ys.length - 1] >= startingCash;
    const color = up ? "var(--green)" : "var(--red)";
    const base = Y(startingCash).toFixed(1);

    box.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="equity curve">
        <path d="${area}" fill="${color}" opacity="0.12"></path>
        <path d="${line}" fill="none" stroke="${color}" stroke-width="1.8"></path>
        <line x1="${padL}" y1="${base}" x2="${W - padR}" y2="${base}"
              stroke="var(--muted)" stroke-dasharray="4 4" stroke-width="1"></line>
        <text x="4" y="${Y(yMax).toFixed(1)}" fill="var(--muted)" font-size="11" dominant-baseline="hanging">$${yMax.toFixed(2)}</text>
        <text x="4" y="${Y(yMin).toFixed(1)}" fill="var(--muted)" font-size="11">$${yMin.toFixed(2)}</text>
        <text x="4" y="${base}" fill="var(--muted)" font-size="11" dominant-baseline="middle">$${startingCash.toFixed(0)}</text>
        <text x="${padL}" y="${H - 6}" fill="var(--muted)" font-size="11">${fmtDate(xMin)}</text>
        <text x="${W - padR}" y="${H - 6}" fill="var(--muted)" font-size="11" text-anchor="end">${fmtDate(xMax)}</text>
      </svg>`;
    $("chart-span").textContent = `— ${points.length} points`;
  }

  /* ---------------- trades table ---------------- */

  function renderTrades(trades) {
    const tbody = $("trades-table").querySelector("tbody");
    tbody.innerHTML = "";
    show($("trades-empty"), trades.length === 0);
    $("trades-count").textContent = trades.length ? `— latest ${trades.length}` : "";
    for (const t of trades) {
      const tr = document.createElement("tr");
      const pnl = t.pnl == null ? "" : Number(t.pnl).toFixed(2);
      const pnlCls = t.pnl == null ? "" : Number(t.pnl) >= 0 ? "pos" : "neg";
      tr.innerHTML = `
        <td>${new Date(Number(t.ts)).toLocaleString()}</td>
        <td class="${t.side === "BUY" ? "buy" : "sell"}">${t.side}</td>
        <td>${Number(t.price).toFixed(2)}</td>
        <td>${Number(t.qty).toFixed(6)}</td>
        <td class="${pnlCls}">${pnl}</td>
        <td>${t.reason}</td>`;
      tbody.appendChild(tr);
    }
  }

  /* ---------------- new run + delete ---------------- */

  $("btn-new-run").addEventListener("click", () => {
    $("nr-message").textContent = "";
    $("dlg-new-run").showModal();
  });

  $("new-run-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("nr-message").textContent = "";
    const { data, error } = await db.rpc("create_run", {
      p_name: $("nr-name").value,
      p_symbol: $("nr-symbol").value,
      p_timeframe: $("nr-timeframe").value,
      p_strategy: $("nr-strategy").value,
      p_starting_cash: Number($("nr-cash").value) || 100,
    });
    if (error) {
      $("nr-message").textContent = error.message;
      return;
    }
    const row = Array.isArray(data) ? data[0] : data;
    $("dlg-new-run").close();
    $("key-block").textContent = [
      `export TRADINGBOT_WEB_URL="${cfg.SUPABASE_URL}"`,
      `export TRADINGBOT_WEB_ANON_KEY="${cfg.SUPABASE_ANON_KEY}"`,
      `export TRADINGBOT_WEB_RUN_ID="${row.run_id}"`,
      `export TRADINGBOT_WEB_RUN_KEY="${row.ingest_key}"`,
    ].join("\n");
    $("dlg-key").showModal();
    await refreshRuns();
    selectRun(row.run_id);
  });

  $("btn-copy-key").addEventListener("click", () => {
    navigator.clipboard.writeText($("key-block").textContent);
    $("btn-copy-key").textContent = "Copied ✓";
    setTimeout(() => ($("btn-copy-key").textContent = "Copy"), 1500);
  });

  $("btn-delete-run").addEventListener("click", async () => {
    const run = runs.find((r) => r.id === selectedId);
    if (!run) return;
    if (!confirm(`Delete "${run.name}" and all its data? This cannot be undone.`)) return;
    const { error } = await db.from("runs").delete().eq("id", run.id);
    if (error) return alert(error.message);
    selectRun(null);
    refreshRuns();
  });

  /* ---------------- polling + helpers ---------------- */

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(refreshRuns, 30_000);
  }
  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  function fmtPct(x) {
    return `${x >= 0 ? "+" : ""}${(x * 100).toFixed(2)}%`;
  }
  function fmtDate(ts) {
    return new Date(ts).toLocaleDateString();
  }
  function relTime(date) {
    const s = Math.round((Date.now() - date.getTime()) / 1000);
    if (s < 90) return `${s}s ago`;
    if (s < 5400) return `${Math.round(s / 60)}min ago`;
    if (s < 129600) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }
}
