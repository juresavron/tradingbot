"""Authenticated single-process control API and dashboard for the bot worker."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import os
import secrets
import time
from threading import BoundedSemaphore, RLock
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import normalize_email, validate_password_hash, verify_password
from .orders import TERMINAL_STATUSES
from .market_io import MAX_CLOCK_SKEW_MS, MAX_QUOTE_AGE_MS

COOKIE = "tradingbot_session"
SESSION_SECONDS = 8 * 60 * 60
MAX_BODY_BYTES = 16 * 1024
DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
_SNAPSHOT_UNAVAILABLE = object()
_PRIVATE_IPV4 = tuple(ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def operational_readiness(service, snapshot=None) -> dict:
    """Describe pipeline health without exposing account or provider data.

    Ready means that observations and local processing are usable. It does
    not authorize an entry, promise an execution, or assess profitability.
    A supplied snapshot is reused so dashboard reads do not query twice.
    """
    enabled = bool(getattr(service, "worker_enabled", False))
    alive = bool(getattr(service, "worker_alive", False))
    if snapshot is None:
        try:
            snapshot = service.snapshot()
        except Exception:
            snapshot = _SNAPSHOT_UNAVAILABLE
    available = isinstance(snapshot, dict)
    result = {
        "ready": False, "execution": "simulation", "worker_alive": alive,
        "storage_available": available, "market_fresh": False,
        "price_event_fresh": None, "quote_received_fresh": False,
        "entries_requested": False, "blocking_reasons": [],
    }
    reasons = result["blocking_reasons"]
    if not enabled:
        reasons.append("worker_disabled")
    elif not alive:
        reasons.append("worker_unavailable")
    if not available:
        reasons.append("storage_unavailable")
        return result

    status = snapshot.get("status") or {}
    config = snapshot.get("config") or {}
    result["entries_requested"] = bool(status.get("desired_running", status.get("running", False)))
    if status.get("worker_error"):
        reasons.append("worker_error")
    if status.get("error"):
        reasons.append("pipeline_error")
    if status.get("state") == "halted":
        reasons.append("automation_halted")

    poll = getattr(service, "poll_seconds", 5.0)
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not math.isfinite(poll) or poll <= 0:
        poll = 5.0
    deadline = max(60_000, int(poll * 4000))
    now_ms = int(time.time() * 1000)

    def fresh_timestamp(value):
        return type(value) is int and value > 0 and 0 <= now_ms - value <= deadline

    heartbeat = status.get("heartbeat_ms")
    price = status.get("price")
    valid_price = (isinstance(price, (int, float)) and not isinstance(price, bool)
                   and math.isfinite(price) and price > 0)
    observed = valid_price and type(heartbeat) is int and heartbeat > 0
    source = config.get("source", status.get("source"))
    heartbeat_fresh = fresh_timestamp(heartbeat) and not status.get("heartbeat_stale", False)
    # Demo fixtures deliberately have historical market times. Their worker
    # heartbeat is wall time; never declare them stale because of fixture time.
    event_ms, receipt_ms = status.get("price_event_ms"), status.get("quote_received_ms")
    receipt_fresh = (type(receipt_ms) is int and receipt_ms > 0
                     and -MAX_CLOCK_SKEW_MS <= now_ms - receipt_ms <= deadline)
    event_known = type(event_ms) is int and event_ms > 0
    event_fresh = (event_known and -MAX_CLOCK_SKEW_MS <= now_ms - event_ms <= MAX_QUOTE_AGE_MS
                   and type(receipt_ms) is int and event_ms <= receipt_ms + MAX_CLOCK_SKEW_MS)
    result["quote_received_fresh"] = bool(receipt_fresh)
    if source == "binance" and event_known:
        result["price_event_fresh"] = bool(event_fresh)
    market_fresh = bool(observed and heartbeat_fresh and (
        source == "demo" or source == "binance" and receipt_fresh and event_fresh
    ))
    result["market_fresh"] = market_fresh
    if not observed:
        reasons.append("market_not_observed")
    elif not heartbeat_fresh:
        reasons.append("heartbeat_stale")
    elif not market_fresh:
        reasons.append("price_event_unknown" if source == "binance" and not event_known else "market_data_stale")

    orders = snapshot.get("orders", [])
    if not isinstance(orders, list) or any(
        not isinstance(order, dict) or order.get("status") not in TERMINAL_STATUSES for order in orders
    ):
        reasons.append("execution_unresolved")
    if config.get("news_policy") == "require-fresh" and result["entries_requested"]:
        base = (snapshot.get("portfolio") or {}).get("base_asset")
        if not base and isinstance(config.get("symbol"), str):
            base = config["symbol"].upper().replace("-", "/").partition("/")[0]
        eligible = False
        news_at = status.get("market_time_ms")
        for article in snapshot.get("news") or []:
            if not isinstance(article, dict) or base not in article.get("assets", []):
                continue
            times = (article.get("published_at_ms"), article.get("observed_at_ms"))
            if (type(news_at) is int and news_at > 0
                    and all(type(value) is int and 0 <= news_at - value <= 86_400_000 for value in times)):
                eligible = True
                break
        if not eligible:
            reasons.append("news_unavailable")
    result["ready"] = not reasons
    return result


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope.get("headers", [])).get(b"content-length")
        try:
            oversized = length is not None and int(length) > MAX_BODY_BYTES
        except ValueError:
            oversized = True
        if oversized:
            await JSONResponse({"detail": "Request body is too large"}, status_code=413)(scope, receive, send)
            return
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise _BodyTooLarge
            return message

        async def track_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, track_send)
        except _BodyTooLarge:
            if not started:
                await JSONResponse({"detail": "Request body is too large"}, status_code=413)(scope, receive, send)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str | None = Field(default=None, min_length=1, max_length=512)
    email: str | None = Field(default=None, min_length=3, max_length=254)
    password: str | None = Field(default=None, min_length=1, max_length=256)


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=1, max_length=30)


def create_app(*, db_path=None, token=None, public_origin=None, start_worker=True, runtime=None,
               owner_email=None, owner_password_hash=None) -> FastAPI:
    db_path = db_path or os.environ.get("TRADINGBOT_DB", "data/server.sqlite3")
    token = token if token is not None else os.environ.get("TRADINGBOT_DASHBOARD_TOKEN", "")
    if not isinstance(token, str) or not 32 <= len(token) <= 512:
        raise ValueError("Set TRADINGBOT_DASHBOARD_TOKEN to a private value of 32-512 characters")
    token_hash = hashlib.sha256(token.encode()).digest()
    owner_email = os.environ.get("TRADINGBOT_OWNER_EMAIL", "") if owner_email is None else owner_email
    owner_password_hash = (os.environ.get("TRADINGBOT_OWNER_PASSWORD_HASH", "")
                           if owner_password_hash is None else owner_password_hash)
    if bool(owner_email) != bool(owner_password_hash):
        raise ValueError("Configure both the owner email and password hash")
    if owner_email:
        owner_email = normalize_email(owner_email)
        validate_password_hash(owner_password_hash)
    origin = public_origin if public_origin is not None else os.environ.get("PUBLIC_ORIGIN")
    if origin:
        parsed = urlsplit(origin)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("PUBLIC_ORIGIN must be an HTTP(S) origin without a path, credentials, or query")
        origin = origin.rstrip("/")
        if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1", "testserver"):
            raise ValueError("A public dashboard requires HTTPS in PUBLIC_ORIGIN")
    secure_cookie = bool(origin and origin.startswith("https://"))
    fly_proxy = os.environ.get("TRADINGBOT_FLY_PROXY", "0")
    if fly_proxy not in ("0", "1"):
        raise ValueError("TRADINGBOT_FLY_PROXY must be 0 or 1")
    if fly_proxy == "1" and (not os.environ.get("FLY_APP_NAME") or not secure_cookie):
        raise ValueError("Fly proxy identity requires a Fly machine and an HTTPS origin")
    allowed_hosts = ["localhost", "127.0.0.1", "[::1]"]
    if origin:
        allowed_hosts.append(urlsplit(origin).hostname)
    sessions: dict[str, tuple[float, str | None]] = {}
    failed_logins: dict[str, deque] = defaultdict(deque)
    auth_lock = RLock()
    password_slots = BoundedSemaphore(2)

    def login_address(request):
        peer = request.client.host if request.client else "unknown"
        # Opt in only on Fly's IPv4-only HTTP listener (enforced by cmd_serve).
        # Public HTTP traffic passes Fly Proxy, which overwrites this header.
        # Direct 6PN, loopback and non-Fly requests cannot supply identity.
        # X-Forwarded-For is intentionally never trusted.
        if fly_proxy == "1":
            try:
                address = ipaddress.ip_address(peer)
                values = request.headers.getlist("fly-client-ip")
                if (address.version == 4 and any(address in network for network in _PRIVATE_IPV4)
                        and len(values) == 1 and "%" not in values[0]):
                    return str(ipaddress.ip_address(values[0]))
            except ValueError:
                pass
        return peer

    @asynccontextmanager
    async def lifespan(app):
        service = runtime
        if service is None:
            from .automation import AutomationRuntime
            service = AutomationRuntime(db_path, start_worker=start_worker)
        app.state.runtime = service
        collector = None
        try:
            from .news_collector import NewsCollector
            collector = NewsCollector(
                db_path, asset_provider=lambda: service.snapshot().get("portfolio", {}).get("base_asset", "BTC"),
                start=start_worker,
            )
            app.state.news_collector = collector
            yield
        finally:
            try:
                if collector is not None:
                    collector.shutdown()
            finally:
                service.shutdown()

    app = FastAPI(title="Tradingbot control room", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(set(allowed_hosts)))

    def authenticated(request: Request) -> bool:
        cookie = request.cookies.get(COOKIE, "")
        if not cookie or len(cookie) > 256:
            return False
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        with auth_lock:
            expiry, _email = sessions.get(digest, (0, None))
            if expiry <= time.monotonic():
                sessions.pop(digest, None)
                return False
            return True

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            if request.method not in ("GET", "POST"):
                return JSONResponse({"detail": "Method not allowed"}, status_code=405)
            if request.method == "POST":
                expected = origin or str(request.base_url).rstrip("/")
                if request.headers.get("origin") != expected:
                    return JSONResponse({"detail": "Request origin is not allowed"}, status_code=403)
                if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                    return JSONResponse({"detail": "Use application/json"}, status_code=415)
            if request.url.path not in {"/api/session", "/api/login", "/api/logout"} and not authenticated(request):
                return JSONResponse({"detail": "Sign in to the control room"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(ValueError)
    async def value_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def runtime_error(_request, _exc):
        return JSONResponse({"detail": "The worker could not complete this action; inspect its status"}, status_code=409)

    @app.get("/healthz")
    def health(request: Request):
        # Process liveness intentionally does not restart the machine because
        # an external feed is down. /readyz reports operational blockers.
        service = request.app.state.runtime
        enabled = getattr(service, "worker_enabled", False)
        alive = getattr(service, "worker_alive", False)
        if enabled and not alive:
            return JSONResponse({"status": "worker_unavailable", "execution": "simulation"}, status_code=503)
        return {"status": "ok", "execution": "simulation", "worker_enabled": enabled, "worker_alive": alive}

    @app.get("/readyz")
    def readiness(request: Request):
        result = operational_readiness(request.app.state.runtime)
        return JSONResponse(result, status_code=200 if result["ready"] else 503)

    @app.get("/api/session")
    def session(request: Request):
        signed_in = authenticated(request)
        digest = hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()
        with auth_lock:
            email = sessions.get(digest, (0, None))[1] if signed_in else None
        return {"authenticated": signed_in, "email": email}

    @app.post("/api/login")
    def login(body: Login, request: Request):
        now = time.monotonic()
        address = login_address(request)
        ticket = (now, object())
        with auth_lock:
            for key, bucket in list(failed_logins.items()):
                while bucket and bucket[0][0] <= now - 60:
                    bucket.popleft()
                if not bucket:
                    del failed_logins[key]
            if address not in failed_logins and len(failed_logins) >= 1000:
                raise HTTPException(429, "Too many attempts; wait a minute")
            attempts = failed_logins[address]
            if len(attempts) >= 5:
                raise HTTPException(429, "Too many attempts; wait a minute")
            # Reserve before expensive work so simultaneous attempts cannot
            # bypass the bucket. Success removes only its own reservation.
            attempts.append(ticket)
        if not password_slots.acquire(blocking=False):
            raise HTTPException(429, "Sign-in is busy; retry shortly")
        try:
            valid = False
            if body.token is not None and body.email is None and body.password is None:
                # Retain the existing private operator token for recovery/API use.
                valid = hmac.compare_digest(hashlib.sha256(body.token.encode()).digest(), token_hash)
            elif body.email is not None and body.password is not None and body.token is None and owner_email:
                # Always derive the password hash, including for unknown addresses.
                password_matches = verify_password(body.password, owner_password_hash)
                email_matches = hmac.compare_digest(body.email.strip().casefold().encode(), owner_email.encode())
                valid = password_matches and email_matches
            if not valid:
                raise HTTPException(401, "Invalid email or password")
        finally:
            password_slots.release()
        now = time.monotonic()
        with auth_lock:
            if ticket in attempts:
                attempts.remove(ticket)
            for digest, (expiry, _email) in list(sessions.items()):
                if expiry <= now:
                    sessions.pop(digest, None)
            if len(sessions) >= 100:
                sessions.pop(next(iter(sessions)))
            cookie = secrets.token_urlsafe(32)
            sessions[hashlib.sha256(cookie.encode()).hexdigest()] = (now + SESSION_SECONDS, owner_email or None)
        response = JSONResponse({"authenticated": True, "email": owner_email or None})
        response.set_cookie(COOKIE, cookie, httponly=True, secure=secure_cookie,
                            samesite="strict", max_age=SESSION_SECONDS, path="/")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        cookie = request.cookies.get(COOKIE, "")
        with auth_lock:
            sessions.pop(hashlib.sha256(cookie.encode()).hexdigest(), None)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/", httponly=True, secure=secure_cookie, samesite="strict")
        return response

    @app.get("/api/state")
    def state(request: Request):
        service = request.app.state.runtime
        try:
            snapshot = service.snapshot()
        except Exception:
            return JSONResponse({"detail": "Trading state is unavailable", "readiness":
                                 operational_readiness(service, snapshot=_SNAPSHOT_UNAVAILABLE)}, status_code=503)
        snapshot["readiness"] = operational_readiness(service, snapshot=snapshot)
        snapshot["news_source"] = request.app.state.news_collector.snapshot()
        return snapshot

    @app.post("/api/control")
    def control(body: Control, request: Request):
        if body.action not in {"start", "pause", "flatten", "step"}:
            raise HTTPException(400, "Unknown control action")
        request.app.state.runtime.control(body.action)
        return state(request)

    @app.post("/api/config")
    def config(body: dict, request: Request):
        request.app.state.runtime.configure(body)
        return state(request)

    @app.post("/api/news/sync")
    def sync_news(request: Request):
        collector = request.app.state.news_collector
        if not collector.snapshot()["configured"]:
            raise HTTPException(400, "Configure TRADINGBOT_RSS_URL or APIFY_DATASET_ID on the server first")
        result = collector.sync_once()
        if result.get("error"):
            return JSONResponse({"detail": "News feed is unavailable; existing records remain unchanged",
                                 "news_source": result}, status_code=503)
        return {"news_source": result, "state": state(request)}

    @app.get("/")
    def index():
        return FileResponse(DASHBOARD / "index.html")

    @app.get("/app.js")
    def javascript():
        return FileResponse(DASHBOARD / "app.js", media_type="application/javascript")

    @app.get("/style.css")
    def css():
        return FileResponse(DASHBOARD / "style.css", media_type="text/css")

    return app
