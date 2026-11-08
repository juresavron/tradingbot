# Deploy the simulation dashboard to Fly.io

This deployment serves the dashboard, stores durable bot memory on a Fly
Volume, and runs one background worker in the same server process. On the first
boot of an empty database, automatic execution starts **paused** in simulation.
Existing configuration and the desired running state survive restarts, so pause
automation explicitly before deployment or recovery. The default demo source uses
fake prices; the news panel stays empty unless a feed is configured. The dashboard
works without Binance, Apify, or API keys. This is not evidence of real-market profitability.

The repository contains deployable files, but creating an app, provisioning
a paid machine/volume, authenticating with Fly, and verifying the deployed
service are separate steps. Do not interpret local tests as a completed Fly
deployment.

Current preparation (8 November 2026): the container builds and passes authenticated
desktop/mobile, background simulated buy/sell, non-root UID, and persistent restart
checks. Fly CLI 0.5.5 is installed from a checksum-verified official release.
`fly.toml` proposes `juresavron-tradingbot` and its matching HTTPS origin; the
name's availability and ownership have not been checked with Fly. No app,
machine or volume has been created from this environment.

The running cloud policy rejects `api.fly.io` with proxy CONNECT 403, and the
CLI has no account credential. The saved network draft includes the required
Fly endpoints, but is not applied. Provider-side validation and a public health
check remain pending. Do not describe this state as a deployed application.

## Prerequisites

- A Fly.io account with permission and billing enabled for the desired
  organization. Machine and persistent-volume charges apply while running.
- `flyctl`, authenticated using `fly auth login`.
- A globally unique app name chosen by the operator.

The proposed name is `juresavron-tradingbot`. Deployment must inspect the
authenticated account and provider state before creating or modifying it.
Changing the name also requires changing `PUBLIC_ORIGIN` and the exact public
hostname allowed for verification.

On macOS, `brew install flyctl` installs the packaged CLI with Homebrew's
artifact-checksum verification. For another platform, use a release from
[the official Fly CLI repository](https://github.com/superfly/flyctl/releases),
verify the downloaded artifact against that release's published checksum
file, and install it into a user-owned binary directory. Do not install an
unverified third-party binary or disable HTTPS verification.

### Connect this managed cloud environment

The required public destinations are `api.fly.io` (control API),
`api.machines.dev` (Machines/apps/volumes API), `registry.fly.io` (image registry),
`fly.io` (browser login), and `juresavron-tradingbot.fly.dev` (planned app checks).
The current draft preserves the existing Binance/Apify domains and adds the
missing Machines, browser-login and planned-app domains. Review/save it in
Environment Settings, then publish as requested by the saved-draft result.
Recheck actual runtime access after applying it.

Use interactive browser login for Fly. The older, unbound `FLY_API_TOKEN`
network-secret requirement should be removed in settings for this workflow;
no token value needs to be entered in chat. Fly CLI parses token types and
Docker authentication encodes credentials, so an opaque proxy placeholder is
not a verified substitute for its local credential. Browser login stores its
returned credential in Fly's local, mode-0600 config without printing it.

In this workspace, use a persistent interactive terminal and the verified CLI:

```bash
mkdir -p /tmp/tradingbot-fly-config
chmod 700 /tmp/tradingbot-fly-config
env -u FLY_API_TOKEN -u FLY_ACCESS_TOKEN FLY_CONFIG_DIR=/tmp/tradingbot-fly-config /tmp/tradingbot-flyctl/flyctl auth login
```

The operator opens the approval URL printed by Fly. Preserve this credential
directory for subsequent commands and leave conflicting proxy-token environment
overrides unset in that command context. Never print/copy its access token into
chat, source, logs or deployment arguments.

Build locally to avoid remote-builder/WireGuard dependencies. Keep the configured
proxy/CA trust and use the local Docker daemon. Do not disable TLS, add network
fallbacks, or spin up a remote builder simply to work around blocked access.

## Prepare the app and one persistent volume

Run these commands from the repository checkout. They intentionally require
an app name rather than claiming an available global name:

```bash
export TRADINGBOT_FLY_APP=your-chosen-unique-app-name
fly auth login
fly launch --name "$TRADINGBOT_FLY_APP" --region fra --copy-config --no-deploy
```

Review the resulting `fly.toml` before proceeding. Keep the `/data` mount,
region `fra`, internal port `8080`, HTTPS, health check, and the single `app`
process. Confirm `PUBLIC_ORIGIN` matches the selected app/domain. If the launch wizard proposes a database service or extra services,
decline those additions: this application uses its local SQLite volume.

Inspect existing volumes before creating anything:

```bash
fly volumes list --app "$TRADINGBOT_FLY_APP"
```

For a new app with no suitable volume, create one:

```bash
fly volumes create tradingbot_data --app "$TRADINGBOT_FLY_APP" --region fra --size 1
```

The volume and machine must stay in the same region. A one-gigabyte volume is
an initial allocation, not a retention guarantee: candle history, news, and
audit records grow. Monitor usage and define a retention/archive policy before
collecting a large feed.

## Configure authentication securely

Choose a random, URL-safe dashboard token of at least 32 characters in your
password manager and retain it there for dashboard access. Never place it in
`fly.toml`, a Docker build argument, source files, a commit, or chat.

The following Bash commands read the value without echoing it or placing it
in command arguments. `PUBLIC_ORIGIN` is not a secret; it is included in the
same import to set the exact allowed browser origin.

```bash
set -o pipefail
read -r -s -p 'Dashboard token (32+ URL-safe characters): ' TRADINGBOT_DEPLOY_TOKEN
printf '\n'
export TRADINGBOT_DEPLOY_TOKEN
python3 - <<'PY' | fly secrets import --app "$TRADINGBOT_FLY_APP"
import os
import re
token = os.environ["TRADINGBOT_DEPLOY_TOKEN"]
if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
    raise SystemExit("Use a random URL-safe token containing at least 32 characters")
app = os.environ["TRADINGBOT_FLY_APP"]
if not re.fullmatch(r"[a-z0-9-]+", app):
    raise SystemExit("Set TRADINGBOT_FLY_APP to the chosen Fly app name")
print("TRADINGBOT_DASHBOARD_TOKEN=" + token)
print("PUBLIC_ORIGIN=https://" + app + ".fly.dev")
PY
unset TRADINGBOT_DEPLOY_TOKEN
```

The server refuses startup without a sufficiently long token. Use the same
token when signing in to the dashboard. A custom domain needs a matching
`PUBLIC_ORIGIN`, its Fly certificate/DNS configuration, and subsequent browser
validation. Do not loosen the origin check to `*` to resolve a mismatch.

## Deploy one machine and verify

```bash
fly config validate --app "$TRADINGBOT_FLY_APP"
fly deploy --app "$TRADINGBOT_FLY_APP" --local-only --strategy immediate --ha=false
fly status --app "$TRADINGBOT_FLY_APP"
fly checks list --app "$TRADINGBOT_FLY_APP"
fly logs --app "$TRADINGBOT_FLY_APP"
```

Verify that there is exactly **one** running machine for the `app` process,
with one shared CPU, 512 MB memory, and `tradingbot_data` mounted at `/data`.
`--ha=false` prevents the new-app deployment from creating a spare machine.
If an existing app already has extra machines, review and explicitly reduce
that app to one; do not silently delete machines or volumes.

The configured `immediate` deployment strategy deliberately accepts downtime
while replacing the single machine. Keep it when deploying this single-volume
worker; do not use blue/green, canary, or rolling deployments that require a
second active worker or another machine's volume. Pause automation and inspect
the stored state before a deploy or controlled restart. The Fly health probe
sends `Host: localhost` so the internal check passes the application's trusted
host middleware; the public browser origin remains the configured HTTPS domain.

The supplied configuration keeps autostop off and at least one machine
running. Do not scale SQLite or the background worker across machines, regions,
or multiple Uvicorn workers. A Fly Volume is local to one machine; additional
machines would not share the account, lock, or order journal. The server command
must remain a single process, not `uvicorn --workers N` or a process-manager
cluster.

Verify the public health endpoint:

```bash
curl --fail --show-error "https://$TRADINGBOT_FLY_APP.fly.dev/healthz"
```

Then open the HTTPS app, sign in, confirm simulation/demo labeling and paused
automation, start a short demo run, inspect decisions and simulated fills,
pause it, and verify a controlled machine restart preserves account memory.
Confirm unauthenticated access cannot read or change the protected dashboard
API. Health alone establishes service readiness, not trading profitability,
market-data access, or a successful exchange order.

No paid news actor is launched by deployment. Real publisher feeds, Apify
dataset access, and Binance Testnet require their own configuration and live
checks. They must not be represented as working until those checks succeed.

## Container behavior and local builds

The image uses Python 3.12 and pins only the optional HTTP server dependencies
in `requirements-server.txt`. The trading core remains usable without those
packages. Data, credentials, local SQLite files, `.env` files, and Git metadata
are excluded from the image; `/data` is the only persistent runtime location.

The entrypoint initially runs as root solely to create and set ownership of
the mount directory `/data`, then drops to UID/GID `10001` before executing the
server. It never recursively changes ownership of volume contents. Existing
database/WAL/SHM files must already be readable and writable by that application
user; investigate and repair only the affected paths if restoring older files.
Do not fix a permission error by running the web server as root or applying a
recursive ownership change to an unknown volume.

Ordinary Docker builds use normal system CA trust:

```bash
docker build -t tradingbot-dashboard .
```

In the managed cloud environment, preserve its proxy settings and supply the
combined CA bundle as an ephemeral BuildKit secret if the build needs it:

```bash
docker build --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt \
  -t tradingbot-dashboard .
```

The Dockerfile gives pip that secret through `PIP_CERT` only during package
installation. It is not copied into the image, and TLS verification stays on.
Runtime connections through a managed intercepting proxy need their separate
read-only CA mount and client trust settings; a build-only secret does not
configure runtime trust. Fly's ordinary outbound HTTPS uses normal system CAs.

## Backups and recovery

Keep both Fly Volume snapshots and a tested off-volume backup. Use SQLite's
online backup API or a consistent backup while the application is stopped;
copying only an actively written database file can omit its WAL transactions.
An off-volume backup must be protected because it contains account history,
news, and audit records. Never commit it to the repository.

Before a schema-changing deploy, create a consistent backup and verify that it
can be restored into a separate, private test environment. During recovery,
stop the original worker, attach/restore one volume to one machine, check file
ownership and the required token/origin settings, and verify the restored state
before resuming automation. Never run the restored copy alongside the original
as a second active execution worker.
