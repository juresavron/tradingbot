"""Durable news, account state, and decision/execution history in SQLite.

Writes require an explicit transaction, allowing an account update, decision,
and its fills to succeed or roll back together. News keeps its first observed
timestamp: reimporting an old story cannot rewrite what the bot knew earlier.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class TradingMemory:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS news (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                source TEXT NOT NULL,
                published_at_ms INTEGER NOT NULL,
                observed_at_ms INTEGER NOT NULL,
                assets_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS news_observed ON news(observed_at_ms);
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                at_ms INTEGER NOT NULL,
                candle_ts INTEGER NOT NULL,
                target INTEGER NOT NULL,
                reason TEXT NOT NULL,
                news_ids_json TEXT NOT NULL,
                state_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS decisions_account ON decisions(account_id, id);
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                decision_id INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                fill_json TEXT NOT NULL,
                UNIQUE(account_id, seq)
            );
        """)

    def __enter__(self) -> TradingMemory:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self):
        """Take the SQLite write lock and commit all changes atomically."""
        if self.connection.in_transaction:
            raise ValueError("Nested memory transactions are not supported")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.connection.rollback()
            raise
        else:
            try:
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def _require_transaction(self) -> None:
        if not self.connection.in_transaction:
            raise ValueError("Memory writes require an explicit transaction()")

    @staticmethod
    def _json(value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _integer(value: int, name: str) -> int:
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
        return value

    @staticmethod
    def _identifier(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string")
        return value

    def insert_news(self, article: dict) -> bool:
        self._require_transaction()
        article_id = self._identifier(article["id"], "news id")
        fields = [article[key] for key in ("url", "title", "body", "source")]
        if any(not isinstance(value, str) for value in fields):
            raise ValueError("News URL, title, body, and source must be strings")
        published = self._integer(article["published_at_ms"], "published_at_ms")
        observed = self._integer(article["observed_at_ms"], "observed_at_ms")
        assets = article["assets"]
        if not isinstance(assets, list) or any(not isinstance(asset, str) or not asset.strip() for asset in assets):
            raise ValueError("News assets must be a list of nonempty strings")
        assets_json = self._json(sorted({asset.upper() for asset in assets}))
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO news VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (article_id, *fields, published, observed, assets_json),
        )
        return cursor.rowcount == 1

    def recent_news(self, at_ms: int, asset: str, max_age_ms: int, *,
                    limit: int | None = None, include_body: bool = True) -> list[dict]:
        self._integer(at_ms, "at_ms")
        self._integer(max_age_ms, "max_age_ms")
        self._identifier(asset, "asset")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
            raise ValueError("News limit must be an integer in [1, 1000] or None")
        if type(include_body) is not bool:
            raise ValueError("include_body must be a boolean")
        cutoff = at_ms - max_age_ms
        fields = "*" if include_body else "id,url,title,source,published_at_ms,observed_at_ms,assets_json"
        rows = self.connection.execute(
            f"""SELECT {fields} FROM news
               WHERE observed_at_ms BETWEEN ? AND ?
                 AND published_at_ms BETWEEN ? AND ?
               ORDER BY published_at_ms DESC, id ASC""",
            (cutoff, at_ms, cutoff, at_ms),
        )
        result = []
        for row in rows:
            article = dict(row)
            article["assets"] = json.loads(article.pop("assets_json"))
            if asset.upper() in article["assets"]:
                result.append(article)
                if limit is not None and len(result) >= limit:
                    break
        return result

    def get_news(self, ids: list[str]) -> list[dict]:
        """Retrieve known articles in requested order, omitting duplicate IDs.

        This audit lookup intentionally has no freshness filter: older news
        referenced by a past decision must remain inspectable.
        """
        if not isinstance(ids, list) or len(ids) > 1000:
            raise ValueError("News lookup requires a list of at most 1000 IDs")
        for article_id in ids:
            self._identifier(article_id, "news id")
        ordered_ids = list(dict.fromkeys(ids))
        articles = {}
        # Stay below SQLite's historical 999-variable limit.
        for start in range(0, len(ordered_ids), 900):
            batch = ordered_ids[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            for row in self.connection.execute(
                f"SELECT * FROM news WHERE id IN ({placeholders})", batch,
            ):
                article = dict(row)
                article["assets"] = json.loads(article.pop("assets_json"))
                articles[article["id"]] = article
        return [articles[article_id] for article_id in ordered_ids if article_id in articles]

    def load_account(self, account_id: str) -> dict | None:
        self._identifier(account_id, "account_id")
        row = self.connection.execute(
            "SELECT state_json FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        return None if row is None else json.loads(row["state_json"])

    def save_account(self, account_id: str, state: dict) -> None:
        self._require_transaction()
        self._identifier(account_id, "account_id")
        if not isinstance(state, dict):
            raise ValueError("Account state must be a dictionary")
        self.connection.execute(
            """INSERT INTO accounts(account_id, state_json) VALUES (?, ?)
               ON CONFLICT(account_id) DO UPDATE SET state_json = excluded.state_json""",
            (account_id, self._json(state)),
        )

    def record_decision(self, account_id: str, at_ms: int, candle_ts: int,
                        target: int, reason: str, news_ids: list[str], state: dict) -> int:
        self._require_transaction()
        self._identifier(account_id, "account_id")
        self._integer(at_ms, "at_ms")
        self._integer(candle_ts, "candle_ts")
        if target not in (0, 1):
            raise ValueError("Decision target must be 0 or 1")
        if not isinstance(reason, str):
            raise ValueError("Decision reason must be a string")
        if not isinstance(news_ids, list) or any(not isinstance(item, str) for item in news_ids):
            raise ValueError("Decision news_ids must be a list of strings")
        if not isinstance(state, dict):
            raise ValueError("Decision state must be a dictionary")
        cursor = self.connection.execute(
            """INSERT INTO decisions(account_id, at_ms, candle_ts, target, reason,
                                     news_ids_json, state_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account_id, at_ms, candle_ts, target, reason, self._json(news_ids), self._json(state)),
        )
        return cursor.lastrowid

    def record_fill(self, account_id: str, decision_id: int, seq: int, fill: dict) -> None:
        self._require_transaction()
        self._identifier(account_id, "account_id")
        self._integer(decision_id, "decision_id")
        self._integer(seq, "seq")
        if not isinstance(fill, dict):
            raise ValueError("Fill must be a dictionary")
        encoded = self._json(fill)
        prior = self.connection.execute(
            "SELECT decision_id, fill_json FROM fills WHERE account_id = ? AND seq = ?",
            (account_id, seq),
        ).fetchone()
        if prior is not None:
            if prior["decision_id"] != decision_id or prior["fill_json"] != encoded:
                raise ValueError("Conflicting duplicate execution sequence")
            return
        self.connection.execute(
            "INSERT INTO fills(account_id, decision_id, seq, fill_json) VALUES (?, ?, ?, ?)",
            (account_id, decision_id, seq, encoded),
        )

    def history(self, account_id: str, limit: int = 20) -> dict:
        self._identifier(account_id, "account_id")
        if type(limit) is not int or limit < 1:
            raise ValueError("History limit must be a positive integer")
        decisions = []
        for row in self.connection.execute(
            "SELECT * FROM decisions WHERE account_id = ? ORDER BY id DESC LIMIT ?",
            (account_id, limit),
        ):
            entry = dict(row)
            entry["news_ids"] = json.loads(entry.pop("news_ids_json"))
            entry["state"] = json.loads(entry.pop("state_json"))
            decisions.append(entry)
        fills = []
        for row in self.connection.execute(
            "SELECT * FROM fills WHERE account_id = ? ORDER BY seq DESC LIMIT ?",
            (account_id, limit),
        ):
            entry = dict(row)
            entry["fill"] = json.loads(entry.pop("fill_json"))
            fills.append(entry)
        return {"decisions": decisions, "fills": fills}
