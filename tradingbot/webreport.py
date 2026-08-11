"""Optional reporting to the web dashboard (Supabase).

The bot runs on your machine; the dashboard is a read-only window into it.
Reporting turns on automatically when four environment variables are set
(the dashboard's "New run" dialog prints them ready to paste — see WEB.md):

    TRADINGBOT_WEB_URL       e.g. https://abcdefgh.supabase.co
    TRADINGBOT_WEB_ANON_KEY  the project's public (anon/publishable) key
    TRADINGBOT_WEB_RUN_ID    identifies the run on the dashboard
    TRADINGBOT_WEB_RUN_KEY   the run's secret write key -- treat like a password

Reporting must never break trading: every failure is caught, counted, and
reduced to an occasional console warning while the bot carries on.
"""

from __future__ import annotations

import json
import os
import urllib.request

EQUITY_TAIL = 200  # points per report; server upserts make re-sending harmless


class WebReporter:
    def __init__(self, url: str, anon_key: str, run_id: str, run_key: str, timeout: float = 10.0):
        self.endpoint = url.rstrip("/") + "/rest/v1/rpc/ingest"
        self.anon_key = anon_key
        self.run_id = run_id
        self.run_key = run_key
        self.timeout = timeout
        self.consecutive_failures = 0

    @classmethod
    def from_env(cls) -> WebReporter | None:
        values = [
            os.environ.get("TRADINGBOT_WEB_URL"),
            os.environ.get("TRADINGBOT_WEB_ANON_KEY"),
            os.environ.get("TRADINGBOT_WEB_RUN_ID"),
            os.environ.get("TRADINGBOT_WEB_RUN_KEY"),
        ]
        if not all(values):
            return None
        return cls(*values)

    def build_payload(self, state: dict) -> dict:
        equity = []
        for entry in state.get("equity_log", [])[-EQUITY_TAIL:]:
            # Old state files log [ts, equity]; new ones [ts, equity, price].
            price = float(entry[2]) if len(entry) > 2 and entry[2] is not None else None
            equity.append([int(entry[0]), float(entry[1]), price])
        trades = [
            {
                "seq": i,
                "ts": t.get("ts"),
                "side": t.get("side"),
                "price": t.get("price"),
                "qty": t.get("qty"),
                "pnl": t.get("pnl"),
                "reason": t.get("reason", "signal"),
            }
            for i, t in enumerate(state.get("trades", []))
        ]
        return {
            "p_run_id": self.run_id,
            "p_key": self.run_key,
            "p_equity": equity,
            "p_trades": trades,
            "p_killed": bool(state.get("killed", False)),
            "p_meta": {
                "symbol": state.get("symbol"),
                "timeframe": state.get("timeframe"),
                "strategy": state.get("strategy"),
                "starting_cash": state.get("starting_cash"),
            },
        }

    def _post(self, payload: dict) -> None:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "apikey": self.anon_key,
                "Authorization": f"Bearer {self.anon_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout):
            pass

    def push(self, state: dict) -> bool:
        try:
            self._post(self.build_payload(state))
        except Exception as e:  # reporting must never break trading
            self.consecutive_failures += 1
            if self.consecutive_failures in (1, 10, 100):
                print(f"[dashboard] report failed ({e}) — trading continues", flush=True)
            return False
        self.consecutive_failures = 0
        return True
