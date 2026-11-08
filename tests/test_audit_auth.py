"""Identity and availability regression tests for the deployed HTTP boundary."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from tradingbot.server import create_app
except ImportError:
    TestClient = None

from tradingbot.auth import hash_password
from tests.test_server import FakeRuntime


@unittest.skipIf(TestClient is None, "Optional server dependencies are not installed")
class TestAuditAuthentication(unittest.TestCase):
    TOKEN = "test-dashboard-token-32-characters-long"
    ORIGIN = "https://bot.example"

    def app(self, **kwargs):
        return create_app(token=self.TOKEN, public_origin=self.ORIGIN, runtime=FakeRuntime(),
                          start_worker=False, db_path="/tmp/tradingbot-audit-auth.sqlite3", **kwargs)

    def post(self, client, ip, token="wrong"):
        return client.post("/api/login", json={"token": token},
                           headers={"Origin": self.ORIGIN, "Fly-Client-IP": ip})

    def test_fly_ingress_clients_have_independent_login_buckets(self):
        with patch.dict(os.environ, {"TRADINGBOT_FLY_PROXY": "1", "FLY_APP_NAME": "audit"}), \
                TestClient(self.app(), base_url=self.ORIGIN, client=("172.19.0.1", 1234)) as client:
            for _ in range(5):
                self.assertEqual(self.post(client, "198.51.100.10").status_code, 401)
            self.assertEqual(self.post(client, "198.51.100.10", self.TOKEN).status_code, 429)
            self.assertEqual(self.post(client, "198.51.100.20", self.TOKEN).status_code, 200)

    def test_untrusted_and_invalid_forwarding_cannot_reset_a_bucket(self):
        cases = [("0", "172.19.0.1"), ("1", "198.51.100.1"), ("1", "127.0.0.1"),
                 ("1", "fdaa::2"), ("1", "172.19.0.1")]
        for enabled, peer in cases:
            with self.subTest(enabled=enabled, peer=peer), \
                    patch.dict(os.environ, {"TRADINGBOT_FLY_PROXY": enabled, "FLY_APP_NAME": "audit"}), \
                    TestClient(self.app(), base_url=self.ORIGIN, client=(peer, 1234)) as client:
                for n in range(5):
                    ip = f"invalid-{n}" if peer == "172.19.0.1" and enabled == "1" else f"198.51.100.{n+10}"
                    self.assertEqual(self.post(client, ip).status_code, 401)
                response = client.post("/api/login", json={"token": self.TOKEN},
                                       headers={"Origin": self.ORIGIN, "Fly-Client-IP": "not-an-ip",
                                                "X-Forwarded-For": "203.0.113.50"})
                self.assertEqual(response.status_code, 429)

    def test_password_work_does_not_hold_session_lock_and_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        checks, count_lock = [], threading.Lock()

        def slow_password(*args):
            with count_lock:
                checks.append(1)
                if len(checks) == 2:
                    entered.set()
            if not release.wait(5):
                raise RuntimeError("Test password check was not released")
            return False

        with TestClient(self.app(owner_email="owner@example.com", owner_password_hash=hash_password("audit-password-123")),
                        base_url=self.ORIGIN) as client:
            self.assertEqual(self.post(client, "198.51.100.10", self.TOKEN).status_code, 200)
            with patch("tradingbot.server.verify_password", side_effect=slow_password), ThreadPoolExecutor(3) as pool:
                logins = [pool.submit(client.post, "/api/login", json={"email": "owner@example.com", "password": "wrong"},
                                      headers={"Origin": self.ORIGIN}) for _ in range(2)]
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertEqual(pool.submit(client.get, "/api/state").result(timeout=1).status_code, 200)
                    self.assertTrue(all(not login.done() for login in logins))
                    self.assertEqual(self.post(client, "198.51.100.10", self.TOKEN).status_code, 429)
                    self.assertEqual(len(checks), 2)
                finally:
                    release.set()
                self.assertEqual([login.result(timeout=1).status_code for login in logins], [401, 401])

    def test_fly_identity_requires_an_explicit_fly_environment(self):
        with patch.dict(os.environ, {"TRADINGBOT_FLY_PROXY": "1", "FLY_APP_NAME": ""}):
            with self.assertRaisesRegex(ValueError, "Fly machine"):
                self.app()


if __name__ == "__main__":
    unittest.main()
