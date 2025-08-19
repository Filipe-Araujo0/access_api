# Access API Proxy

A small, practical proxy that coordinates access to an upstream API that enforces rate limits. It is early-stage but already functional.

- Acts as a reverse proxy to your upstream.
- Identifies callers via the `x-connection-id` header (or derives one automatically).
- Splits the available rate budget fairly across active callers.
- Retries on upstream rate-limit responses (429) and temporary unavailability (503), waiting as required between attempts.
- Intended primarily for short windows (per-minute style limits) but conceptually works for longer windows as well.
- All state is in-memory; run with a single worker.

## How It Works

- The service exposes the same paths as your upstream and forwards requests transparently.
- Each client is associated with a connection id:
  - Preferred: set the `x-connection-id` header yourself.
  - Otherwise: the service derives an id from `ip|user-agent`.
- The global limit window is split evenly across active callers; their allowances are recalculated as callers come and go.
- When the upstream responds with 429 or 503 (or indicates reset via standard headers), the proxy waits before retrying, coordinating a global pause so all callers back off together.
- Clients should set a sufficiently large timeout to keep the connection open while the proxy waits for the upstream window to reset.

A lightweight status endpoint is available at `GET /__status` showing the current callers and their budgets.

## Quick Start

Requirements: Python 3.10+ (recommended), a shell, and the packages in `requirements.txt`.

1) Install dependencies

```
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

2) Configure the upstream base URL

- For now, set the upstream directly in `access_api.py` by editing the `UPSTREAM_BASE` constant.
- Example: `UPSTREAM_BASE = "https://your-upstream.example.com"`

3) Run locally

```
python access_api.py
```

- The service starts on `0.0.0.0:5812` with `workers=1` and auto-reload enabled for development.

4) Call through the proxy

```
# Optional but recommended: set a stable connection id
curl -H 'x-connection-id: alice' http://localhost:5812/your/upstream/path
```

Make sure your client timeout accommodates potential waiting periods when the upstream throttles (e.g., 30–90 seconds, depending on the upstream policy).

## Systemd (Linux) Setup

- The repository includes `run_access_api.sh` to (re)install the service in a clean, reproducible way.
- It copies `access_api.py`, installs `requirements.txt` into a venv under `/opt/access_api`, sets up `/etc/access_api.env`, and installs `access_api.service`.
- After running, the service listens on port `5812` as the `access_api` user.

Typical usage (run from the repo root):

```
./run_access_api.sh
```

Note: the script expects `.env`, `access_api.service`, `requirements.txt`, and `access_api.py` to be present in the repo.

## Configuration

Core behavior is configured in code for clarity at this early stage. A few knobs are exposed via environment variables (when set by systemd or your shell):

- `ACCESS_API_UPSTREAM_HARD_TIMEOUT_SECONDS`: Optional global hard timeout (seconds) for a single upstream attempt. If `<= 0`, disabled.
- `ACCESS_API_HTTP2`: `1/true/on` to request HTTP/2 (requires the `h2` package). If unset, HTTP/2 is enabled only when `h2` is installed; set `0/false/off` to force HTTP/1.1.
- `FALLBACK_429_SECONDS`: Base backoff for 429 when no reliable retry header is present. Default `1.0`.
- `FALLBACK_503_SECONDS`: Base backoff for 503 (grows exponentially per attempt). Default `5.0`.
- `RETRY_JITTER_PCT`: Jitter percentage applied to fallback backoff, default `0.2` (±20%).

Upstream base URL:

- Current version uses the `UPSTREAM_BASE` constant in `access_api.py`. Change it to match your upstream.
- The provided `.env` file is installed for systemd use and may be expanded in future versions to configure the upstream via environment variables.

## Client Guidance

- Prefer to provide a stable `x-connection-id` so the proxy can allocate a fair share to your caller across requests.
- Set an application-level timeout generous enough to cover expected wait times when the upstream enforces rate limits.
- You do not need any special client-side retry logic for 429/503; the proxy manages backoff and retries.

## Limitations

- In-memory state: run with a single worker (`--workers 1`). Horizontal scaling would require shared state.
- Best suited to shorter windows (e.g., per minute). Works for longer windows, but you likely need larger client timeouts.
- Early-stage project: interfaces and configuration points may evolve.

## Status Endpoint

- `GET /__status` returns a JSON payload with the number of active callers and each caller’s general/current allowance.

## License

This project is licensed under the terms of the LICENSE file included in the repository.

