from math import ceil
import os
import random
import time
import asyncio
from typing import Optional

from fastapi import FastAPI, Request, Response
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging


GCL = 200  # General call limit
TRLS = 60  # Time to reset limit

n_callers = 0

# Pausa global simples coordenando todas as conexões após 429/503
PAUSED_UNTIL: float = 0.0


def global_pause_remaining() -> float:
    return max(0.0, PAUSED_UNTIL - time.monotonic())


def set_pause(seconds: float) -> None:
    """Estende a pausa global até agora+seconds (se maior que a atual)."""
    if not seconds or seconds <= 0:
        return
    global PAUSED_UNTIL
    now = time.monotonic()
    PAUSED_UNTIL = max(PAUSED_UNTIL, now + seconds)


async def forward_request_to_upstream(req: Request) -> httpx.Response:
    """Encaminha o request bruto ao upstream preservando corpo e cabeçalhos."""
    method = req.method.upper()
    path = req.url.path
    query = str(req.url.query)
    headers = {k: v for k, v in req.headers.items()}
    body = await req.body()
    url = path + (("?" + query) if query else "")
    return await req.app.state.http.request(method, url, headers=headers, content=body)


class Caller:
    def __init__(self):
        self.general_limit = GCL / max(1, n_callers)
        self.current_limit = self.general_limit
        self.last_reset_ts = time.monotonic()
        self.last_response_received_ts = 0
        self.last_call_ts = 0

    def _has_limit_to_call_now(self):
        return self.current_limit >= 1

    async def call(self, req):
        # Loop iterativo (evita recursão e melhora previsibilidade)
        while True:
            if self._has_limit_to_call_now():
                # Respeita pausa global vigente, se houver
                if (gpr := global_pause_remaining()) > 0:
                    await asyncio.sleep(gpr)
                self.current_limit -= 1
                self.last_call_ts = time.monotonic()
                resp = await forward_request_to_upstream(req)
                self.last_response_received_ts = time.monotonic()
                return resp

            time_to_reset = self._reset_limit()
            if time_to_reset is not None:
                await asyncio.sleep(ceil(time_to_reset))

    def _reset_limit(self):
        time_from_last_call = time.monotonic() - self.last_reset_ts
        time_to_reset = TRLS - time_from_last_call
        if time_to_reset <= 0:
            self.current_limit = self.general_limit
            self.last_reset_ts = time.monotonic()
            return
        return time_to_reset


callers_by_id: dict[str, Caller] = dict()


def recalculate_caller_limit(caller: Caller):
    caller.general_limit = max(1, GCL / n_callers)  # TODO: do it better
    if caller.current_limit > caller.general_limit:
        caller.current_limit = caller.general_limit
    return caller


UPSTREAM_BASE = "http://127.0.0.1:8001"
OUTBOUND_MAX_CONNECTIONS = 1000
OUTBOUND_MAX_KEEPALIVE = 1000

# Detect optional HTTP/2 support (requires the 'h2' package)
try:
    import h2  # type: ignore  # noqa: F401
    H2_AVAILABLE = True
except Exception:
    H2_AVAILABLE = False

logger = logging.getLogger("access_api")


def should_enable_http2() -> bool:
    """Return True if HTTP/2 should be enabled.

    Behavior:
    - If ACCESS_API_HTTP2 is undefined: enable HTTP/2 only when 'h2' is installed.
    - If ACCESS_API_HTTP2 in {"1","true","yes","on"}: request HTTP/2; if 'h2' is missing,
      we will log a warning and fall back to HTTP/1.1 to avoid startup errors.
    - If ACCESS_API_HTTP2 in {"0","false","no","off"}: force disable HTTP/2.
    """
    env = os.getenv("ACCESS_API_HTTP2")
    if env is None:
        return H2_AVAILABLE
    val = env.strip().lower()
    if val in {"0", "false", "no", "off"}:
        return False
    if val in {"1", "true", "yes", "on"}:
        if not H2_AVAILABLE:
            logger.warning(
                "ACCESS_API_HTTP2 requested but 'h2' not installed; falling back to HTTP/1.1"
            )
        return H2_AVAILABLE
    # Any other value: be conservative and disable
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Cria httpx.AsyncClient e garante fechamento no shutdown."""
    http2_enabled = should_enable_http2()
    if http2_enabled:
        logger.info("Starting HTTP client with HTTP/2 enabled")
    else:
        logger.info("Starting HTTP client with HTTP/1.1 (HTTP/2 disabled)")

    app.state.http = httpx.AsyncClient(
        base_url=UPSTREAM_BASE,
        http2=http2_enabled,
        timeout=httpx.Timeout(60.0, connect=10.0, read=60.0, write=60.0),
        limits=httpx.Limits(
            max_connections=OUTBOUND_MAX_CONNECTIONS,
            max_keepalive_connections=OUTBOUND_MAX_KEEPALIVE,
        ),
        trust_env=True,
    )
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(lifespan=lifespan)


def derive_connection_id(req: Request) -> str:
    """Deriva um identificador estável para a conexão.

    Preferência: header "x-connection-id". Caso ausente, usa "ip|user-agent".
    """
    cid = req.headers.get("x-connection-id")
    if cid:
        return cid
    ip = req.client.host if req.client else "na"
    ua = req.headers.get("user-agent", "na")
    return f"{ip}|{ua}"


RATE_LIMIT_STATUS_CODES = {429, 503}


def build_fastapi_response(r: httpx.Response) -> Response:
    """Converte httpx.Response em fastapi.Response filtrando cabeçalhos hop-by-hop."""
    resp_headers = {k: v for k, v in r.headers.items()}
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers=resp_headers,
        media_type=r.headers.get("content-type"),
    )


def parse_retry_after_seconds(headers: dict) -> Optional[float]:
    """
    Converte cabeçalhos Retry-After/*reset* em segundos de espera.

    - "Retry-After": aceita segundos numéricos ou data HTTP (RFC 7231).
    - Variantes *-reset: tratadas como delta em segundos.
    Limita retorno a [0, 300] para evitar pausas excessivas.
    """
    # Try Retry-After (segundos numéricos ou data HTTP)
    v = headers.get("retry-after")
    if v:
        s = str(v).strip()
        try:
            x = float(s)
            return max(0.0, min(x, 300.0))
        except Exception:
            try:
                dt = parsedate_to_datetime(s)
                if dt:
                    base = datetime.now(dt.tzinfo or timezone.utc)
                    delta = (dt - base).total_seconds()
                    return max(0.0, min(delta, 300.0))
            except Exception:
                pass

    # Variações comuns de reset (tratadas como delta em segundos)
    for k in ("ratelimit-reset", "x-ratelimit-reset", "x-rate-limit-reset"):
        v = headers.get(k)
        if not v:
            continue
        try:
            s = float(str(v).strip())
            return max(0.0, min(s, 300.0))
        except Exception:
            continue
    return None


FALLBACK_RETRY_BASE_SECONDS = {
    429: float(os.getenv("FALLBACK_429_SECONDS", "1.0")),
    503: float(os.getenv("FALLBACK_503_SECONDS", "5.0")),
}
RETRY_JITTER_PCT = float(os.getenv("RETRY_JITTER_PCT", "0.2"))  # ±20%


def compute_fallback_retry_after(status_code: int, attempt_idx: int) -> float:
    """Backoff conservador com jitter quando não há cabeçalho confiável.

    - 429: crescimento moderado base*(1 + 0.5*attempt).
    - 503: exponencial base*2**attempt (limitado).
    """
    base = FALLBACK_RETRY_BASE_SECONDS.get(status_code, 1.0)
    if status_code == 503:
        backoff = base * (2 ** min(attempt_idx, 6))
    else:
        backoff = base * (1.0 + 0.5 * min(attempt_idx, 10))
    jitter = 1.0 + random.uniform(-RETRY_JITTER_PCT, RETRY_JITTER_PCT)
    backoff *= jitter
    return backoff


MAX_CALLER_IDLE_TIME_SECONDS = 5


@app.get("/__status")
async def status():
    return {
        "n_callers": n_callers,
        "callers": {
            caller_id: (caller.general_limit, caller.current_limit)
            for caller_id, caller in callers_by_id.items()
        },
    }


@app.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
)
async def proxy(path: str, request: Request):
    conn_id = derive_connection_id(request)
    global n_callers
    for caller_id in tuple(callers_by_id.keys()):
        caller = callers_by_id[caller_id]
        time_from_last_interaction = time.monotonic() - max(
            caller.last_response_received_ts, caller.last_call_ts
        )
        if time_from_last_interaction > MAX_CALLER_IDLE_TIME_SECONDS:
            callers_by_id.pop(caller_id)
            n_callers -= 1
    if conn_id in callers_by_id:
        caller = callers_by_id[conn_id]
    else:
        n_callers += 1
        caller = Caller()
        callers_by_id[conn_id] = caller
    for caller_id in tuple(callers_by_id.keys()):
        callers_by_id[caller_id] = recalculate_caller_limit(callers_by_id[caller_id])

    upstream_retries = 0
    upstream_wait_total = 0
    while True:
        r = await caller.call(request)
        sc = r.status_code
        if sc not in RATE_LIMIT_STATUS_CODES:
            return build_fastapi_response(r)
        ra = parse_retry_after_seconds(r.headers)
        if ra is None:
            ra = compute_fallback_retry_after(sc, upstream_retries)
        # Aplica backpressure global para coordenar todas as conexões
        set_pause(ra)
        upstream_retries += 1
        upstream_wait_total += ra
        continue


if __name__ == "__main__":
    import copy
    from uvicorn.config import LOGGING_CONFIG as UVICORN_LOGGING_CONFIG
    import uvicorn

    log_config = copy.deepcopy(UVICORN_LOGGING_CONFIG)
    for name in ("default", "access"):
        fmt = log_config["formatters"][name]["fmt"]
        log_config["formatters"][name]["fmt"] = "%(asctime)s " + fmt
        log_config["formatters"][name]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    uvicorn.run(
        "new:app",
        host="0.0.0.0",
        port=5812,
        log_config=log_config,
        reload=True,
        workers=1,
    )
