# app.py — Proxy FastAPI com rate limit por execução, retries com Prefer: wait,
# suporte a 429/503 (e 5xx transitórios), logs detalhados (Stream + File) e lifespan moderno.

import os
import asyncio
import time
import json
import uuid
import random
import logging
import contextvars
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Tuple, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from logging.handlers import RotatingFileHandler

# ==============================
# Config
# ==============================
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE_URL", "https://example.com")

LIMIT_PER_MIN = float(os.getenv("LIMIT_PER_MINUTE", "200"))  # L (req/min)
RATE_G = LIMIT_PER_MIN / 60.0                                # tokens/s (global)
CAP_G = float(os.getenv("GLOBAL_CAP", str(int(LIMIT_PER_MIN))))
ACTIVE_WINDOW = float(os.getenv("ACTIVE_WINDOW_SECONDS", "5"))    # conexões "ativas" (s)
BURST_WINDOW  = float(os.getenv("BURST_WINDOW_SECONDS",  "30"))   # cap_i = rate_i * BURST_WINDOW
PREFER_WAIT_DEFAULT = float(os.getenv("PREFER_WAIT_DEFAULT", "0")) # s
OUT_MAX_CONN = int(os.getenv("OUTBOUND_MAX_CONNECTIONS", "30"))
OUT_MAX_KEEPALIVE = int(os.getenv("OUTBOUND_MAX_KEEPALIVE", "20"))

# Rate-limit e falhas transitórias
RATE_LIMIT_STATUSES = {429, 503}                  # 503 tratado como rate-limit (compat com seu cliente)
TRANSIENT_5XX = {500, 502, 504}
FALLBACK_RA = {429: 1.0, 503: 60.0}               # fallback de Retry-After (503=60s como no cliente)

# Logs
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL = 'DEBUG'
LOG_FILE = os.getenv("LOG_FILE", "./access_api.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MiB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
LOG_FORMAT = os.getenv("LOG_FORMAT", "json").lower()  # json|text

# ==============================
# Logger
# ==============================
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
actives_start_ctx = contextvars.ContextVar("actives_start", default=None)
rate_start_ctx    = contextvars.ContextVar("rate_start", default=None)

class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get("-")
        return True

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for k in (
            "method","path","status","latency_ms","client_ip",
            "active_conns","active_conns_start","rate_per_conn","rate_per_conn_start","global_tokens",
            "local_wait_s","local_waits","upstream_wait_s","upstream_retries",
            "deadline_s","wait_required_s","attempts","upstream_status"
        ):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f'{datetime.now(timezone.utc).isoformat()} [{record.levelname}] {record.name} req={getattr(record,"request_id","-")} {record.getMessage()}'
        extras = []
        for k in (
            "method","path","status","latency_ms","client_ip",
            "active_conns","active_conns_start","rate_per_conn","rate_per_conn_start","global_tokens",
            "local_wait_s","local_waits","upstream_wait_s","upstream_retries",
            "deadline_s","wait_required_s","attempts","upstream_status"
        ):
            v = getattr(record, k, None)
            if v is not None:
                extras.append(f"{k}={v}")
        if extras:
            base += " " + " ".join(extras)
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base

def build_logger() -> logging.Logger:
    logger = logging.getLogger("access_api")
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    fmt = JSONFormatter() if LOG_FORMAT == "json" else TextFormatter()
    flt = RequestIdFilter()

    sh = logging.StreamHandler()
    sh.setLevel(logger.level)
    sh.setFormatter(fmt)
    sh.addFilter(flt)
    logger.addHandler(sh)

    os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setLevel(logger.level)
    fh.setFormatter(fmt)
    fh.addFilter(flt)
    logger.addHandler(fh)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("h11").setLevel(logging.WARNING)

    return logger

log = build_logger()

# ==============================
# Rate limit (token-bucket)
# ==============================
class TokenBucket:
    __slots__ = ("rate","cap","tokens","last","paused_until")
    def __init__(self, rate: float, cap: float):
        self.rate = rate
        self.cap = cap
        self.tokens = cap
        self.last = time.monotonic()
        self.paused_until = 0.0

    def _refill(self, now: float):
        if now < self.paused_until:
            self.last = now
            return
        elapsed = now - self.last
        if elapsed > 0:
            self.tokens = min(self.cap, self.tokens + self.rate * elapsed)
            self.last = now

    def take(self, cost: float = 1.0) -> Tuple[bool, float]:
        now = time.monotonic()
        self._refill(now)
        if now < self.paused_until:
            return False, self.paused_until - now
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        wait = (cost - self.tokens) / self.rate if self.rate > 0 else 1e9
        return False, wait

    def set_rate_cap(self, rate: float, cap: Optional[float] = None):
        now = time.monotonic()
        self._refill(now)
        self.rate = max(1e-9, rate)
        if cap is not None:
            self.cap = max(1.0, cap)

    def pause(self, seconds: float):
        self.paused_until = max(self.paused_until, time.monotonic() + max(0.0, seconds))

class ConnManager:
    def __init__(self):
        self.clients: Dict[str, TokenBucket] = {}
        self.last_seen: Dict[str, float] = {}
        self.global_bucket = TokenBucket(RATE_G, CAP_G)

    def mark_active(self, cid: str, now: float):
        self.last_seen[cid] = now
        if cid not in self.clients:
            self.clients[cid] = TokenBucket(rate=1e-6, cap=1.0)

    def prune_and_compute_rates(self):
        now = time.monotonic()
        actives = [cid for cid, ts in self.last_seen.items() if now - ts <= ACTIVE_WINDOW]
        n = max(1, len(actives))
        rate_each = (LIMIT_PER_MIN / 60.0) / n
        cap_each = max(1.0, rate_each * BURST_WINDOW)
        for cid in actives:
            self.clients[cid].set_rate_cap(rate_each, cap_each)
        for cid in list(self.clients.keys()):
            if cid not in actives:
                self.clients[cid].set_rate_cap(1e-6, 1.0)
                if now - self.last_seen.get(cid, 0) > 300:
                    self.clients.pop(cid, None)
                    self.last_seen.pop(cid, None)
        return actives, rate_each

    def get_bucket(self, cid: str) -> TokenBucket:
        return self.clients[cid]

RL = ConnManager()

# ==============================
# App lifecycle + httpx hooks
# ==============================
STRIP_HEADERS = {
    "connection","keep-alive","proxy-authenticate","proxy-authorization",
    "te","trailer","transfer-encoding","upgrade",
    "content-length","content-encoding"
}

async def _httpx_request_hook(request: httpx.Request):
    log.debug("upstream_request", extra={"method": str(request.method), "path": str(request.url)})

async def _httpx_response_hook(response: httpx.Response):
    log.debug("upstream_response", extra={"upstream_status": response.status_code, "path": str(response.request.url)})

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        base_url=UPSTREAM_BASE,
        timeout=httpx.Timeout(60.0, connect=10.0, read=60.0, write=60.0),
        limits=httpx.Limits(max_connections=OUT_MAX_CONN, max_keepalive_connections=OUT_MAX_KEEPALIVE),
        trust_env=True,
        event_hooks={"request":[_httpx_request_hook], "response":[_httpx_response_hook]},
    )
    try:
        yield
    finally:
        await app.state.http.aclose()

app = FastAPI(lifespan=lifespan)

# ==============================
# Middleware: request-id + access
# ==============================
@app.middleware("http")
async def add_request_id_and_access_log(request: Request, call_next):
    rid = request.headers.get("x-request-id") or str(uuid.uuid4())
    token = request_id_ctx.set(rid)
    start = time.perf_counter()
    client_ip = request.client.host if request.client else "-"
    response: Response = None  # type: ignore
    try:
        response = await call_next(request)
    except Exception:
        log.error("unhandled_exception", exc_info=True, extra={
            "method": request.method, "path": request.url.path, "client_ip": client_ip
        })
        response = JSONResponse({"detail": "internal_error"}, status_code=500)
    finally:
        dur_ms = round((time.perf_counter() - start) * 1000, 2)
        actives_now = len([1 for ts in RL.last_seen.values() if time.monotonic()-ts <= ACTIVE_WINDOW])
        rate_now = (LIMIT_PER_MIN/60.0)/max(1, actives_now)

        # snapshots do início do request
        actives_start = actives_start_ctx.get(None)
        rate_start = rate_start_ctx.get(None)

        log.info(
            "access",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": getattr(response, "status_code", 0),
                "latency_ms": dur_ms,
                "client_ip": client_ip,
                "global_tokens": round(RL.global_bucket.tokens, 2),
                "active_conns": actives_now,
                "rate_per_conn": round(rate_now, 6),
                "active_conns_start": actives_start,
                "rate_per_conn_start": round(rate_start, 6) if rate_start is not None else None,
            },
        )
        response.headers["X-Request-ID"] = rid
        request_id_ctx.reset(token)
    return response

# ==============================
# Helpers
# ==============================
def _conn_id(req: Request) -> str:
    cid = req.headers.get("x-connection-id")
    if cid:
        return cid
    ip = req.client.host if req.client else "na"
    ua = req.headers.get("user-agent","na")
    return f"{ip}|{ua}"

def _prefer_wait(req: Request) -> float:
    pref = req.headers.get("prefer","").lower().strip()
    if pref.startswith("wait="):
        try:
            return float(pref.split("=",1)[1])
        except Exception:
            return PREFER_WAIT_DEFAULT
    return PREFER_WAIT_DEFAULT

def _retry_after_seconds(h: dict) -> Optional[float]:
    # numérico simples ou HTTP-date; aceita variações comuns de header
    for k in ("retry-after","ratelimit-reset","x-ratelimit-reset","x-rate-limit-reset"):
        v = h.get(k)
        if not v:
            continue
        # numérico
        try:
            return float(v)
        except Exception:
            pass
        # HTTP-date
        try:
            dt = parsedate_to_datetime(v)
            base = datetime.now(dt.tzinfo or timezone.utc)
            return max(0.0, (dt - base).total_seconds())
        except Exception:
            continue
    return None

async def _call_upstream_raw(req: Request) -> httpx.Response:
    method = req.method.upper()
    path = req.url.path
    query = str(req.url.query)
    headers = {k: v for k, v in req.headers.items() if k.lower() not in STRIP_HEADERS}
    headers.pop("host", None)
    headers["X-Request-ID"] = request_id_ctx.get("-")
    body = await req.body()
    url = path + (("?" + query) if query else "")
    return await req.app.state.http.request(method, url, headers=headers, content=body)

def _build_response_from_httpx(r: httpx.Response) -> Response:
    resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in STRIP_HEADERS}
    return Response(content=r.content, status_code=r.status_code, headers=resp_headers, media_type=r.headers.get("content-type"))

# ==============================
# Rota catch-all com fairness por conexão e retries
# ==============================
@app.api_route("/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"])
async def proxy(request: Request, path: str):
    # identificação e cálculo de cotas
    cid = _conn_id(request)
    RL.mark_active(cid, time.monotonic())
    actives, rate_each = RL.prune_and_compute_rates()

    # snapshot para logs e headers
    actives_start_ctx.set(len(actives))
    rate_start_ctx.set(rate_each)

    prefer_wait = _prefer_wait(request)
    deadline_ts = time.monotonic() + prefer_wait if prefer_wait > 0 else None

    local_waits = 0
    local_wait_total = 0.0

    # Gate local: aguarda tokens global/cliente se necessário
    attempts = 0
    while True:
        attempts += 1
        ok_g, wg = RL.global_bucket.take()
        ok_c, wc = RL.get_bucket(cid).take()
        if ok_g and ok_c:
            break
        wait = max(wg, wc)
        log.debug(
            "local_rate_wait",
            extra={
                "wait_required_s": round(wait, 3),
                "attempts": attempts,
                "active_conns": len(actives),
                "rate_per_conn": round(rate_each, 6),
                "global_tokens": round(RL.global_bucket.tokens, 3),
            },
        )
        if deadline_ts is not None and wait > 0 and (time.monotonic() + wait) <= deadline_ts:
            local_waits += 1
            local_wait_total += wait
            await asyncio.sleep(wait)
            continue
        retry_after = max(0, int(round(wait)))
        return JSONResponse(
            {"detail":"rate_limited_local","wait_required_s":wait,"attempts":attempts},
            status_code=429,
            headers={
                "Retry-After": str(retry_after),
                "X-Wait-Required": f"{wait:.3f}",
                "X-Active-Connections": str(max(1,len(actives))),
                "X-Rate-Per-Connection": f"{rate_each:.6f}",
            },
        )

    # Loop de upstream: respeita 429/503 com Retry-After e 5xx transitórios
    upstream_retries = 0
    upstream_wait_total = 0.0
    backoff = 1.0  # para 5xx transitórios

    while True:
        r = await _call_upstream_raw(request)
        sc = r.status_code

        # 429/503 → tratar como rate-limit do provedor
        if sc in RATE_LIMIT_STATUSES:
            ra = _retry_after_seconds(r.headers) or FALLBACK_RA.get(sc, 1.0)
            RL.global_bucket.pause(ra)  # backpressure global
            log.debug(
                "upstream_rate_limited",
                extra={"upstream_status": sc, "wait_required_s": round(ra,3),
                       "active_conns": len(actives), "rate_per_conn": round(rate_each, 6)},
            )
            if deadline_ts is not None and (time.monotonic() + ra) <= deadline_ts:
                upstream_retries += 1
                upstream_wait_total += ra
                await asyncio.sleep(ra)
                continue
            # prazo esgotado → devolve 429/503 do upstream
            resp = _build_response_from_httpx(r)
            resp.headers["X-Upstream-Retry-After"] = f"{ra:.3f}"
            resp.headers["X-Retry-Attempts"] = str(upstream_retries)
            resp.headers["X-Active-Connections"] = str(max(1,len(actives)))
            resp.headers["X-Rate-Per-Connection"] = f"{rate_each:.6f}"
            resp.headers["X-RateLimit-Remaining-Global"] = f"{RL.global_bucket.tokens:.2f}"
            resp.headers["X-RateLimit-Rate-Global"] = f"{RL.global_bucket.rate:.4f}"

            log.info(
                "proxy_done_upstream_rl",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": sc,
                    "local_wait_s": round(local_wait_total, 3),
                    "local_waits": local_waits,
                    "upstream_wait_s": round(upstream_wait_total, 3),
                    "upstream_retries": upstream_retries,
                    "deadline_s": prefer_wait if prefer_wait > 0 else 0.0,
                },
            )
            return resp

        # 5xx transitório → backoff exponencial + jitter
        if sc in TRANSIENT_5XX:
            jitter = random.uniform(0.1, 0.5)
            wait = min(backoff + jitter, 8.0)  # teto curto
            log.debug("upstream_5xx_backoff", extra={"upstream_status": sc, "wait_required_s": round(wait,3)})
            if deadline_ts is not None and (time.monotonic() + wait) <= deadline_ts:
                upstream_retries += 1
                upstream_wait_total += wait
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 8.0)
                continue
            # prazo esgotado → devolve como veio
            resp = _build_response_from_httpx(r)
            resp.headers["X-Retry-Attempts"] = str(upstream_retries)
            log.info(
                "proxy_done_upstream_5xx",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": sc,
                    "local_wait_s": round(local_wait_total, 3),
                    "local_waits": local_waits,
                    "upstream_wait_s": round(upstream_wait_total, 3),
                    "upstream_retries": upstream_retries,
                    "deadline_s": prefer_wait if prefer_wait > 0 else 0.0,
                },
            )
            return resp

        # sucesso ou código definitivo → responde
        resp = _build_response_from_httpx(r)
        resp.headers["X-Retry-Attempts"] = str(upstream_retries)
        resp.headers["X-Active-Connections"] = str(max(1,len(actives)))
        resp.headers["X-Rate-Per-Connection"] = f"{rate_each:.6f}"
        resp.headers["X-RateLimit-Remaining-Global"] = f"{RL.global_bucket.tokens:.2f}"
        resp.headers["X-RateLimit-Rate-Global"] = f"{RL.global_bucket.rate:.4f}"

        log.info(
            "proxy_done",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": sc,
                "local_wait_s": round(local_wait_total, 3),
                "local_waits": local_waits,
                "upstream_wait_s": round(upstream_wait_total, 3),
                "upstream_retries": upstream_retries,
                "deadline_s": prefer_wait if prefer_wait > 0 else 0.0,
            },
        )
        return resp

# Execução (foi feito para rodar com apenas um worker):
# uvicorn access_api:app --host 0.0.0.0 --port 8000 --workers 1
