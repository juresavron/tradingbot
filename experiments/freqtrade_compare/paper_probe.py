"""Bounded, credentialless native Freqtrade dry-run connectivity probe.

The probe never reports a sustained trading success. It stores raw subprocess
output only beneath the repository's ignored data/freqtrade_compare/online/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import ssl
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONFIG = HERE / "config.paper.json"
PYTHON = HERE / ".venv" / "bin" / "python"
STRATEGIES = HERE / "strategies"
RAW_LOG_ROOT = ROOT / "data" / "freqtrade_compare" / "online"
_CREDENTIAL_FIELDS = {"key", "apikey", "secret", "password", "privatekey", "walletaddress", "uid", "accountid", "token"}


def _reject_credentials(value) -> None:
    if isinstance(value, dict):
        for field, item in value.items():
            if field.replace("_", "").lower() in _CREDENTIAL_FIELDS and item not in (None, ""):
                raise ValueError("Exchange credentials must be absent or empty")
            _reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _reject_credentials(item)


def _validated_config() -> tuple[dict, str]:
    contents = CONFIG.read_bytes()
    config = json.loads(contents)
    if not isinstance(config, dict) or config.get("dry_run") is not True:
        raise ValueError("The probe requires dry_run to be exactly true")
    if config.get("trading_mode") != "spot" or config.get("margin_mode", "") != "":
        raise ValueError("Only non-margin spot simulation is permitted")
    if "add_config_files" in config:
        raise ValueError("Additional configuration files are not permitted")
    exchange = config.get("exchange")
    if not isinstance(exchange, dict) or exchange.get("name") != "binance":
        raise ValueError("The probe requires the Binance public-data configuration")
    _reject_credentials(exchange)
    for service in ("api_server", "telegram", "webhook"):
        settings = config.get(service, {})
        if not isinstance(settings, dict) or settings.get("enabled", False):
            raise ValueError("Remote-control and notification services must be disabled")
    for name in ("ccxt_config", "ccxt_async_config"):
        client = exchange.get(name, {})
        if not isinstance(client, dict):
            raise ValueError("Invalid CCXT configuration")
        if client.get("verify") is False or client.get("validateServerSsl") is False:
            raise ValueError("TLS certificate verification must remain enabled")
        if any(key in client for key in ("urls", "hostname", "httpProxy", "httpsProxy", "proxyUrl", "aiohttp_proxy")):
            raise ValueError("Endpoint and proxy overrides are not permitted in the source config")
    return config, hashlib.sha256(contents).hexdigest()


def _child_environment() -> tuple[dict[str, str], int]:
    # Preserve inherited proxy/CA settings. Freqtrade's own environment-overlay
    # namespace must not override the validated trial configuration.
    removed = [key for key in os.environ if key.upper().startswith("FREQTRADE__")]
    env = {key: value for key, value in os.environ.items() if key not in removed}
    for key in tuple(env):
        if key.upper() in {"BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_SECRET_KEY", "BINANCE_SECRET"}:
            env.pop(key)
    env["PYTHONUNBUFFERED"] = "1"
    return env, len(removed)


def _runtime_config(config: dict) -> tuple[dict, bool]:
    """Make CCXT honor the supplied environment without changing endpoints/TLS."""
    config["exchange"]["enable_ws"] = False
    ca_bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
    if ca_bundle and not Path(ca_bundle).is_file():
        raise ValueError("The inherited CA bundle is unavailable")
    if not ca_bundle:
        ca_bundle = ssl.get_default_verify_paths().cafile
    for name in ("ccxt_config", "ccxt_async_config"):
        client = config["exchange"].setdefault(name, {})
        client["trust_env"] = True
        # CCXT derives these flags before applying generic trust_env from its
        # constructor config. Set each supported client flag explicitly too.
        client["aiohttp_trust_env"] = True
        client["requests_trust_env"] = True
        client["verify"] = True
        if ca_bundle:
            client["cafile"] = ca_bundle
    return config, bool(ca_bundle)


def _stop(process: subprocess.Popen) -> str:
    """Stop only the process group created by this probe."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return "already_exited"
    try:
        process.wait(timeout=5)
        return "sigterm"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        return "sigkill"


def _classify(log: str, timed_out: bool, returncode: int) -> tuple[str, str, str]:
    text = log.lower()
    if "403" in text and any(marker in text for marker in ("clienthttpproxyerror", "tunnel connection failed", "proxyerror", "proxy error", "proxy connect")):
        return "blocked", "proxy_connect_403", "The proxy rejected Binance public market access with HTTP 403. No sustained paper run was verified."
    if any(marker in text for marker in ("certificate_verify_failed", "certificate verify failed", "sslcertverificationerror")):
        return "blocked", "tls_verification_failed", "Native market access failed TLS verification; verification was not disabled."
    if "restricted location" in text or ("451" in text and "binance" in text):
        return "blocked", "exchange_access_restricted", "Binance reported a location/access restriction; no alternate endpoint was attempted."
    if "403" in text and "forbidden" in text:
        return "blocked", "http_403", "Native Binance public-data access returned HTTP 403; the request did not establish market readiness."
    if "clientconnectordnserror" in text or "name or service not known" in text:
        return "blocked", "dns_resolution_failed", "The native public-market client could not resolve its destination. No market access was established."
    if "exchangenotavailable" in text:
        return "blocked", "public_market_unavailable", "The native exchange client reported public market data unavailable. No sustained paper run was verified."
    if timed_out:
        return "started_unverified", "bounded_observation_ended", "The native process was still present at the observation deadline and was stopped. Sustained market data and paper execution remain unverified."
    if returncode:
        return "failed", "native_process_failed", "The native dry-run process exited before a sustained paper run could be verified. Inspect the private local log."
    return "started_unverified", "native_process_exited", "The native process exited without a sustained market-data and paper-execution verification."


def run_probe(output_dir: str | Path, timeout_seconds: float = 25) -> dict:
    """Attempt native public-data paper startup once; return a safe JSON result.

    ``output_dir`` holds this attempt's isolated user data and paper.sqlite.
    Refuse an existing ledger rather than resuming or overwriting prior state.
    The observation bound is at most 25 seconds, plus a 5-second stop grace.
    """
    if isinstance(timeout_seconds, bool) or not math.isfinite(float(timeout_seconds)) or not 0 < float(timeout_seconds) <= 25:
        raise ValueError("timeout_seconds must be greater than zero and at most 25")
    output = Path(output_dir).resolve()
    result = {
        "attempted": False,
        "status": "configuration_rejected",
        "execution": "simulation",
        "dry_run": True,
        "exchange": "binance",
        "public_market_access_verified": False,
        "sustained_paper_trading_verified": False,
        "timeout_seconds": float(timeout_seconds),
    }
    try:
        config, digest = _validated_config()
        if not PYTHON.is_file():
            raise ValueError("The isolated Freqtrade Python environment is missing")
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (output / "paper.sqlite").exists():
            raise ValueError("The output directory already contains a paper ledger; choose a new directory")
        config, ca_configured = _runtime_config(config)
        env, overrides_removed = _child_environment()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Do not echo untrusted JSON/parser errors or credential values.
        result.update(reason="unsafe_or_unavailable_trial_configuration", summary="The probe rejected its configuration or isolated runtime prerequisites. No process was launched.")
        return result

    RAW_LOG_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    log_path = RAW_LOG_ROOT / (run_id + ".log")
    runtime_config = output / "paper.runtime.json"
    with runtime_config.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)
        file.write("\n")
    runtime_config.chmod(0o600)
    command = [
        str(PYTHON), "-m", "freqtrade", "trade",
        "--config", str(runtime_config),
        "--strategy", "DonchianBaseline", "--strategy-path", str(STRATEGIES),
        "--userdir", str(output), "--db-url", "sqlite:///" + str(output / "paper.sqlite"),
        "--dry-run", "--dry-run-wallet", "300", "--no-color",
    ]
    started = time.monotonic()
    timed_out = False
    termination = None
    process = None
    try:
        with log_path.open("x", encoding="utf-8") as logfile:
            log_path.chmod(0o600)
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.wait(timeout=float(timeout_seconds))
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                if process.poll() is None:
                    termination = _stop(process)
    except OSError:
        result.update(status="unavailable", reason="native_launch_failed", summary="The isolated native dry-run process could not be launched.")
        return result
    elapsed = round(time.monotonic() - started, 3)
    # Keep raw output local. Only fixed diagnostic categories enter the result.
    with log_path.open("rb") as logfile:
        logfile.seek(max(0, log_path.stat().st_size - 2_000_000))
        log = logfile.read().decode("utf-8", errors="replace")
    status, reason, summary = _classify(log, timed_out, process.returncode)
    result.update(
        attempted=True, status=status, reason=reason, summary=summary,
        config_sha256=digest, elapsed_seconds=elapsed, timed_out=timed_out,
        returncode=process.returncode, termination=termination,
        removed_freqtrade_environment_overrides=overrides_removed,
        inherited_proxy_environment=any(env.get(key) for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")),
        tls_verification_enabled=True, inherited_ca_configured=ca_configured,
        websockets_enabled=False,
        transport_note="Installed CCXT requires explicit requests_trust_env and aiohttp_trust_env; generic trust_env alone does not enable its derived client flags.",
        runtime_config_changes=["CCXT trust_env/client-specific flags=true", "TLS verify=true", "inherited CA bundle when available", "WebSockets disabled; REST public data only"],
        raw_log=str(log_path.relative_to(ROOT)), user_data_dir=str(output),
    )
    result_path = output / "paper_probe.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Fresh isolated user-data output directory")
    args = parser.parse_args()
    print(json.dumps(run_probe(args.out), indent=2))


if __name__ == "__main__":
    main()
