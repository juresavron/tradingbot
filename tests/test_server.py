import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from tradingbot.server import COOKIE, create_app
except ImportError:
    TestClient = None


class FakeRuntime:
    def __init__(self):
        self.actions = []
        self.closed = False

    def snapshot(self):
        return {"config": {"source": "demo", "symbol": "BTC/EUR"},
                "status": {"state": "paused", "mode": "simulation"},
                "portfolio": {"equity": 300, "base_asset": "BTC"}, "orders": []}

    def control(self, action):
        self.actions.append(action)
        return self.snapshot()

    def configure(self, config):
        if config.get("mode") == "live":
            raise ValueError("Production execution is unavailable")
        return self.snapshot()

    def shutdown(self):
        self.closed = True


@unittest.skipIf(TestClient is None, "Optional server dependencies are not installed")
class TestControlServer(unittest.TestCase):
    TOKEN = "test-dashboard-token-32-characters-long"

    def app(self, runtime=None, origin="http://testserver"):
        return create_app(token=self.TOKEN, public_origin=origin, runtime=runtime or FakeRuntime(),
                          db_path="/tmp/test-tradingbot-server-unused.sqlite3", start_worker=False)

    def login(self, client, origin="http://testserver"):
        return client.post("/api/login", json={"token": self.TOKEN}, headers={"Origin": origin})

    def test_protected_state_requires_session_and_logout_revokes_it(self):
        runtime = FakeRuntime()
        with TestClient(self.app(runtime)) as client:
            self.assertEqual(client.get("/api/state").status_code, 401)
            self.assertFalse(client.get("/api/session").json()["authenticated"])
            response = self.login(client)
            self.assertEqual(response.status_code, 200)
            cookie = response.headers["set-cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=strict", cookie)
            self.assertEqual(client.get("/api/state").json()["portfolio"]["equity"], 300)
            client.post("/api/logout", json={}, headers={"Origin": "http://testserver"})
            self.assertEqual(client.get("/api/state").status_code, 401)
        self.assertTrue(runtime.closed)

    def test_mutations_enforce_origin_json_and_session(self):
        runtime = FakeRuntime()
        with TestClient(self.app(runtime)) as client:
            self.login(client)
            self.assertEqual(client.post("/api/control", json={"action": "start"}).status_code, 403)
            self.assertEqual(client.post("/api/control", json={"action": "start"},
                                         headers={"Origin": "https://evil.example"}).status_code, 403)
            self.assertEqual(client.post("/api/control", content="start",
                                         headers={"Origin": "http://testserver", "Content-Type": "text/plain"}).status_code, 415)
            self.assertEqual(runtime.actions, [])
            response = client.post("/api/control", json={"action": "start"}, headers={"Origin": "http://testserver"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(runtime.actions, ["start"])
            self.assertEqual(client.post("/api/control", json={"action": "live"},
                                         headers={"Origin": "http://testserver"}).status_code, 400)

    def test_login_rate_limit_body_limits_host_check_and_security_headers(self):
        with TestClient(self.app()) as client:
            for _ in range(5):
                self.assertEqual(client.post("/api/login", json={"token": "wrong"},
                                             headers={"Origin": "http://testserver"}).status_code, 401)
            self.assertEqual(self.login(client).status_code, 429)
            response = client.post("/api/login", content="x" * 20000,
                                   headers={"Origin": "http://testserver", "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 413)
            self.assertEqual(client.get("/healthz", headers={"Host": "evil.example"}).status_code, 400)
            response = client.get("/healthz")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_public_cookie_is_secure_and_configuration_errors_are_bounded(self):
        with TestClient(self.app(origin="https://bot.example"), base_url="https://bot.example") as client:
            response = self.login(client, "https://bot.example")
            self.assertIn("Secure", response.headers["set-cookie"])
            self.assertEqual(client.get("/api/state").status_code, 200)
            response = client.post("/api/config", json={"mode": "live"}, headers={"Origin": "https://bot.example"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("unavailable", response.json()["detail"])
        for token, origin in [("short", None), (self.TOKEN, "https://user:secret@bot.example"),
                              (self.TOKEN, "http://public.example"), (self.TOKEN, "https://bot.example/path")]:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                create_app(token=token, public_origin=origin, runtime=FakeRuntime())

    def test_failed_worker_readiness_and_news_startup_cleanup(self):
        runtime = FakeRuntime()
        runtime.worker_enabled = True
        runtime.worker_alive = False
        with TestClient(self.app(runtime)) as client:
            self.assertEqual(client.get("/healthz").status_code, 503)
        runtime = FakeRuntime()
        with mock.patch("tradingbot.news_collector.NewsCollector", side_effect=ValueError("bad news configuration")):
            with self.assertRaises(ValueError):
                with TestClient(self.app(runtime)):
                    pass
        self.assertTrue(runtime.closed)

    def ready_runtime(self, now_ms=1_800_000_000_000):
        runtime = FakeRuntime()
        runtime.worker_enabled = True
        runtime.worker_alive = True
        runtime.poll_seconds = 5
        snapshot = {
            "config": {"source": "demo", "symbol": "BTC/EUR", "news_policy": "require-fresh"},
            "status": {"state": "paused", "desired_running": False, "heartbeat_ms": now_ms,
                       "market_time_ms": 1_000, "price": 100, "heartbeat_stale": False,
                       "worker_error": None, "error": None},
            "portfolio": {"equity": 300, "cash": 300, "base_asset": "BTC"},
            "orders": [], "news": [],
        }
        runtime.snapshot = mock.Mock(return_value=snapshot)
        return runtime, snapshot

    def test_liveness_stays_healthy_but_readiness_reports_storage_failure_safely(self):
        runtime, _ = self.ready_runtime()
        runtime.snapshot.side_effect = RuntimeError("private provider URL with secret-token")
        with TestClient(self.app(runtime)) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            response = client.get("/readyz")
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.json()["storage_available"])
            self.assertIn("storage_unavailable", response.json()["blocking_reasons"])
            self.assertNotIn("private provider", response.text)
            self.assertNotIn("secret-token", response.text)
            self.login(client)
            state = client.get("/api/state")
            self.assertEqual(state.status_code, 503)
            self.assertFalse(state.json()["readiness"]["ready"])
            self.assertNotIn("secret-token", state.text)

    def test_paused_fresh_demo_is_ready_without_news_and_api_reuses_its_state_read(self):
        runtime, _ = self.ready_runtime()
        with mock.patch("tradingbot.server.time.time", return_value=1_800_000_000), TestClient(self.app(runtime)) as client:
            response = client.get("/readyz")
            self.assertEqual(response.status_code, 200)
            readiness = response.json()
            self.assertTrue(readiness["ready"])
            self.assertFalse(readiness["entries_requested"])
            self.assertTrue(readiness["market_fresh"])
            self.assertEqual(set(readiness), {"ready", "execution", "worker_alive", "storage_available",
                                               "market_fresh", "entries_requested", "blocking_reasons"})
            self.assertNotIn("cash", response.text)
            self.login(client)
            runtime.snapshot.reset_mock()
            state = client.get("/api/state")
            self.assertEqual(state.status_code, 200)
            self.assertEqual(state.json()["readiness"], readiness)
            self.assertEqual(runtime.snapshot.call_count, 1)

    def test_readiness_distinguishes_stale_data_worker_errors_and_unresolved_execution(self):
        now = 1_800_000_000_000
        cases = [
            ({"heartbeat_ms": now - 60_001}, {}, [], "heartbeat_stale"),
            ({"heartbeat_stale": True}, {}, [], "heartbeat_stale"),
            ({"market_time_ms": now - 60_001}, {"source": "binance"}, [], "market_data_stale"),
            ({"price": None, "heartbeat_ms": None}, {}, [], "market_not_observed"),
            ({"worker_error": "private worker error"}, {}, [], "worker_error"),
            ({"error": "private pipeline error"}, {}, [], "pipeline_error"),
            ({"state": "halted"}, {}, [], "automation_halted"),
            ({}, {}, [{"status": "UNKNOWN"}], "execution_unresolved"),
        ]
        for status_changes, config_changes, orders, expected in cases:
            with self.subTest(reason=expected):
                runtime, snapshot = self.ready_runtime(now)
                snapshot["status"].update(status_changes)
                snapshot["config"].update(config_changes)
                snapshot["orders"] = orders
                with mock.patch("tradingbot.server.time.time", return_value=now / 1000), TestClient(self.app(runtime)) as client:
                    self.assertEqual(client.get("/healthz").status_code, 200)
                    response = client.get("/readyz")
                    self.assertEqual(response.status_code, 503)
                    self.assertIn(expected, response.json()["blocking_reasons"])
                    self.assertNotIn("private", response.text)

    def test_readiness_news_requires_fresh_relevant_observed_article_when_entries_requested(self):
        now = 1_800_000_000_000
        for assets, published, observed, expected in [
            (["BTC"], now - 1000, now - 500, True),
            (["ETH"], now - 1000, now - 500, False),
            (["BTC"], now - 86_400_001, now - 500, False),
            (["BTC"], now - 1000, now + 500, False),
        ]:
            with self.subTest(assets=assets, expected=expected):
                runtime, snapshot = self.ready_runtime(now)
                snapshot["status"].update(desired_running=True, state="running")
                snapshot["config"]["source"] = "binance"
                snapshot["status"]["market_time_ms"] = now
                snapshot["news"] = [{"id": "news-id", "assets": assets,
                                     "published_at_ms": published, "observed_at_ms": observed,
                                     "body": "private article body"}]
                with mock.patch("tradingbot.server.time.time", return_value=now / 1000), TestClient(self.app(runtime)) as client:
                    response = client.get("/readyz")
                    self.assertEqual(response.status_code, 200 if expected else 503)
                    self.assertEqual(response.json()["ready"], expected)
                    self.assertEqual("news_unavailable" in response.json()["blocking_reasons"], not expected)
                    self.assertNotIn("private article body", response.text)
                    self.assertNotIn("news-id", response.text)

    def test_demo_does_not_treat_todays_news_as_available_at_a_historical_fixture_bar(self):
        now = 1_800_000_000_000
        runtime, snapshot = self.ready_runtime(now)
        snapshot["status"].update(desired_running=True, state="running", market_time_ms=now - 10 * 86_400_000)
        snapshot["news"] = [{"id": "today", "assets": ["BTC"], "published_at_ms": now - 1000,
                             "observed_at_ms": now - 500}]
        with mock.patch("tradingbot.server.time.time", return_value=now / 1000), TestClient(self.app(runtime)) as client:
            response = client.get("/readyz")
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.json()["market_fresh"])
            self.assertEqual(response.json()["blocking_reasons"], ["news_unavailable"])

    def test_readiness_observation_deadline_respects_worker_poll_interval(self):
        now = 1_800_000_000_000
        runtime, snapshot = self.ready_runtime(now)
        runtime.poll_seconds = 30
        snapshot["config"]["source"] = "binance"
        snapshot["status"].update(heartbeat_ms=now - 90_000, market_time_ms=now - 90_000)
        with mock.patch("tradingbot.server.time.time", return_value=now / 1000), TestClient(self.app(runtime)) as client:
            self.assertEqual(client.get("/readyz").status_code, 200)
            snapshot["status"]["market_time_ms"] = now - 120_001
            self.assertEqual(client.get("/readyz").status_code, 503)

    def test_http_pause_does_not_wait_for_market_io_and_fences_the_blocked_cycle(self):
        from tradingbot.automation import AutomationRuntime
        from tradingbot.candles import Candle, TIMEFRAME_MS

        entered_market = threading.Event()
        release_market = threading.Event()
        now_ms = int(time.time() * 1000)
        interval = TIMEFRAME_MS["4h"]
        latest_close = now_ms // interval * interval
        bars = [Candle(latest_close - (300 - i) * interval, 100, 100, 100, 100, 1)
                for i in range(300)]

        def blocked_candles(*args, **kwargs):
            entered_market.set()
            if not release_market.wait(timeout=10):
                raise RuntimeError("Test did not release the market request")
            return bars

        with tempfile.TemporaryDirectory() as tmp:
            runtime = AutomationRuntime(Path(tmp) / "control.sqlite3", start_worker=False)
            try:
                runtime.configure({"source": "binance", "strategy": "buy_hold", "news_policy": "observe"})
                runtime.control("start")
                with TestClient(self.app(runtime)) as client, \
                        mock.patch("tradingbot.automation.MarketData.get_candles", side_effect=blocked_candles), \
                        mock.patch("tradingbot.automation.MarketData.get_price", return_value=100):
                    self.assertEqual(self.login(client).status_code, 200)
                    with ThreadPoolExecutor(max_workers=2) as workers:
                        cycle = workers.submit(runtime.tick_once, now_ms=now_ms)
                        pause = None
                        try:
                            self.assertTrue(entered_market.wait(timeout=2), "Worker did not enter market retrieval")
                            pause = workers.submit(client.post, "/api/control", json={"action": "pause"},
                                                   headers={"Origin": "http://testserver"})
                            response = pause.result(timeout=2)
                            self.assertEqual(response.status_code, 200)
                            self.assertFalse(response.json()["status"]["desired_running"])
                            # The market request is still blocked: the control
                            # response cannot be explained by a finished poll.
                            self.assertFalse(release_market.is_set())
                            self.assertFalse(cycle.done())
                            state = workers.submit(client.get, "/api/state").result(timeout=2)
                            self.assertEqual(state.status_code, 200)
                            self.assertFalse(state.json()["status"]["desired_running"])
                        finally:
                            release_market.set()
                            cycle.result(timeout=5)
                            if pause is not None:
                                pause.result(timeout=5)
                    after = client.get("/api/state").json()
                    self.assertFalse(after["status"]["desired_running"])
                    self.assertEqual(after["metrics"]["n_orders"], 0)
                    self.assertEqual(after["portfolio"]["quantity"], 0)
            finally:
                release_market.set()
                runtime.shutdown()


@unittest.skipIf(TestClient is None, "Optional server dependencies are not installed")
class TestOwnerLogin(unittest.TestCase):
    EMAIL = "owner@example.com"
    PASSWORD = "Only-a-test-password-123"
    TOKEN = "test-dashboard-token-32-characters-long"

    @classmethod
    def setUpClass(cls):
        from tradingbot.auth import hash_password
        cls.PASSWORD_HASH = hash_password(cls.PASSWORD)

    def app(self, **kwargs):
        return create_app(token=self.TOKEN, public_origin="https://bot.example", runtime=FakeRuntime(),
                          owner_email=kwargs.get("owner_email", self.EMAIL),
                          owner_password_hash=kwargs.get("owner_password_hash", self.PASSWORD_HASH),
                          db_path="/tmp/test-tradingbot-owner-unused.sqlite3", start_worker=False)

    def login(self, client, **kwargs):
        return client.post("/api/login", json={"email": kwargs.get("email", self.EMAIL),
                                              "password": kwargs.get("password", self.PASSWORD)},
                           headers={"Origin": "https://bot.example"})

    def test_email_login_identifies_owner_and_logout_revokes_copied_session(self):
        with TestClient(self.app(), base_url="https://bot.example") as client:
            self.assertEqual(client.get("/api/session").json(), {"authenticated": False, "email": None})
            response = self.login(client, email=" OWNER@Example.com ")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"authenticated": True, "email": self.EMAIL})
            self.assertIn("Secure", response.headers["set-cookie"])
            self.assertIn("HttpOnly", response.headers["set-cookie"])
            self.assertEqual(client.get("/api/session").json()["email"], self.EMAIL)
            copied_cookie = client.cookies.get(COOKIE)
            self.assertEqual(client.get("/api/state").status_code, 200)
            client.post("/api/logout", json={}, headers={"Origin": "https://bot.example"})
            self.assertEqual(client.get("/api/state", headers={"Cookie": COOKIE + "=" + copied_cookie}).status_code, 401)

    def test_wrong_email_and_password_get_same_error_and_cannot_control_bot(self):
        with TestClient(self.app(), base_url="https://bot.example") as client:
            wrong_email = self.login(client, email="someone-else@example.com")
            wrong_password = self.login(client, password="wrong-password")
            self.assertEqual(wrong_email.status_code, 401)
            self.assertEqual(wrong_password.status_code, 401)
            self.assertEqual(wrong_email.json(), wrong_password.json())
            self.assertNotIn(self.EMAIL, wrong_email.text)
            self.assertEqual(client.post("/api/control", json={"action": "start"},
                                         headers={"Origin": "https://bot.example"}).status_code, 401)

    def test_owner_login_survives_new_application_but_old_sessions_do_not(self):
        with TestClient(self.app(), base_url="https://bot.example") as first:
            self.assertEqual(self.login(first).status_code, 200)
            old_cookie = first.cookies.get(COOKIE)
        with TestClient(self.app(), base_url="https://bot.example") as second:
            self.assertEqual(second.get("/api/state", headers={"Cookie": COOKIE + "=" + old_cookie}).status_code, 401)
            self.assertEqual(self.login(second).status_code, 200)

    def test_rate_limit_and_input_limits_also_apply_to_password_logins(self):
        with TestClient(self.app(), base_url="https://bot.example") as client:
            self.assertEqual(self.login(client, password="x" * 257).status_code, 422)
            for _ in range(5):
                self.assertEqual(self.login(client, password="wrong").status_code, 401)
            self.assertEqual(self.login(client).status_code, 429)

    def test_partial_or_invalid_account_configuration_fails_at_startup(self):
        for config in ({"owner_email": ""}, {"owner_password_hash": ""},
                       {"owner_password_hash": "plaintext-password"}, {"owner_email": "invalid"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.app(**config)
        with TestClient(self.app(owner_email="", owner_password_hash=""), base_url="https://bot.example") as client:
            self.assertEqual(self.login(client).status_code, 401)


if __name__ == "__main__":
    unittest.main()
