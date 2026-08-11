# Web dashboard — watch your bot from anywhere

A live, login-protected website showing your paper bot's equity curve,
trades, and heartbeat. The bot still runs on your machine (a static site
can't run a 24/7 Python process); the site is a **read-only window** into it.

```
your machine                        cloud
┌──────────────────┐   pushes    ┌──────────────┐   reads    ┌─────────────┐
│ python3 -m       │ ──────────► │  Supabase    │ ◄───────── │   Netlify   │
│ tradingbot paper │  (run key)  │  Postgres +  │  (login)   │ static site │
└──────────────────┘             │  Auth (RLS)  │            │   web/      │
                                 └──────────────┘            └─────────────┘
```

Security model, in one breath: the browser can only **read**, and only
*your* rows (Supabase Auth + row-level security). The bot can only
**write**, and only to its own run, using a per-run secret key — it never
holds your login. Nothing here ever touches exchange API keys.

## One-time setup

### 1. Database (Supabase)

The whole schema lives in [`supabase/migrations/001_dashboard.sql`](supabase/migrations/001_dashboard.sql).
On your Supabase project: **SQL Editor → New query → paste the file → Run.**
(If the project was provisioned for you, this is already done — skip ahead.)

Then in **Project settings → API**, copy two values into `web/config.js`:
the Project URL and the publishable/anon key. Both are public by design.

Optional quality-of-life: **Authentication → Sign In / Providers → Email →**
turn off "Confirm email" so you can sign in immediately after sign-up.
(Leave it on if others might ever see the URL — it's your call.)

### 2. Website (Netlify)

1. Push this repo to GitHub (already done if you're reading this there).
2. [app.netlify.com](https://app.netlify.com) → **Add new site → Import an
   existing project → GitHub** → pick `juresavron/tradingbot`.
3. Branch: this one. Build command: *(leave empty)*. Publish directory: `web`.
   (Netlify reads `netlify.toml`, so the defaults should already be right.)
4. Deploy. You'll get `https://<something>.netlify.app` — open it, click
   **Create account**, sign in.

Every `git push` redeploys the site automatically.

To preview locally instead: `cd web && python3 -m http.server 8000` →
http://localhost:8000 (works with the same config).

### 3. Connect the bot

1. On the dashboard, click **+ New run** and fill in what you're about to
   run (name, symbol, timeframe, strategy — labels only, shown on the site).
2. It shows a block of four `export TRADINGBOT_WEB_*` lines **exactly once**
   — the secret run key is stored only as a hash. Copy it.
3. Paste the block into the terminal on the machine that runs the bot (or
   into a `.env` file you `source` — it's already gitignored), then start
   the bot exactly as in [FIRST_TRADE.md](FIRST_TRADE.md):

```bash
export TRADINGBOT_WEB_URL="https://....supabase.co"
export TRADINGBOT_WEB_ANON_KEY="...public key..."
export TRADINGBOT_WEB_RUN_ID="...uuid..."
export TRADINGBOT_WEB_RUN_KEY="...secret..."

python3 -m tradingbot paper --strategy sma_cross --params fast=10,slow=30 --timeframe 4h --fee 0.004 --cash 100
```

The startup banner will say `Web dashboard reporting: ON`. The bot pushes
at every candle close and every trade; the dashboard shows LIVE / STALE
(no heartbeat for 10+ minutes — bot or machine is down) / KILLED.

Reporting failures never stop trading — if the dashboard is unreachable,
the bot logs a warning and carries on; the state file on your machine
remains the source of truth.

## Costs

Netlify free tier and a Supabase free-tier project are both €0 for this
workload (a few kilobytes per hour). No credit card should ever be needed
for the dashboard.

## Troubleshooting

- **Site shows "Not configured yet"** — `web/config.js` still has
  placeholders; fill it and push again.
- **Sign-up succeeds but sign-in fails** — email confirmation is on and the
  link wasn't clicked; check spam, or disable confirmation (step 1).
- **Run stays "WAITING FOR BOT"** — the four env vars aren't set in the
  shell that started the bot (`echo $TRADINGBOT_WEB_RUN_ID` to check), or
  the bot hasn't hit a candle close yet — the first push comes then.
- **`[dashboard] report failed` in the bot log** — the bot can't reach
  Supabase (network, typo in URL, wrong/rotated run key). Trading is
  unaffected; create a new run to mint a fresh key if needed.
- **STALE on the dashboard** — the bot process or its machine is down; see
  FIRST_TRADE.md Part 4 for the aliveness check and resume command.
