from fastapi import FastAPI, Request
from starlette.responses import JSONResponse
from collections import defaultdict, deque
from time import monotonic
from math import ceil
import copy
import uvicorn
from uvicorn.config import LOGGING_CONFIG as UVICORN_LOGGING_CONFIG

app = FastAPI()


class SlidingWindowLimiter:
    """
    Windows = lista de (limite, janela_em_segundos).
    Ex.: (20,60) = 20/minuto; (20000,86400) = 20000/dia.
    """

    def __init__(self, windows=((20, 60), (20000, 86400))):
        self.windows = list(windows)
        self.store = defaultdict(lambda: [deque() for _ in self.windows])

    def _header_limit(self):
        return ", ".join(f"{lim};w={win}" for lim, win in self.windows)

    def check_and_commit(self, key: str):
        now = monotonic()
        deques = self.store[key]

        remaining = []
        resets = []

        # limpa e mede
        for (limit, win_len), q in zip(self.windows, deques):
            cutoff = now - win_len
            while q and q[0] <= cutoff:
                q.popleft()
            used = len(q)
            remaining.append(limit - used)
            resets.append(0 if used == 0 else int(ceil(win_len - (now - q[0]))))

        # bloqueado se alguma janela estourou
        blocked_resets = [resets[i] for i, rem in enumerate(remaining) if rem <= 0]
        if blocked_resets:
            # tempo até liberar todas as janelas que bloquearam
            retry_after = max(blocked_resets)
            hdr = {
                "RateLimit-Limit": self._header_limit(),
                "RateLimit-Remaining": "0",
                "RateLimit-Reset": str(retry_after),
                "Retry-After": str(retry_after),
            }
            return False, hdr

        # consome a cota desta requisição
        for q in deques:
            q.append(now)

        # recalc restante após consumir
        remaining_after = []
        resets_after = []
        for (limit, win_len), q in zip(self.windows, deques):
            remaining_after.append(limit - len(q))
            resets_after.append(0 if not q else int(ceil(win_len - (now - q[0]))))

        # janela mais restritiva define Remaining/Reset
        k = min(range(len(remaining_after)), key=lambda i: remaining_after[i])
        hdr = {
            "RateLimit-Limit": self._header_limit(),
            "RateLimit-Remaining": str(remaining_after[k]),
            "RateLimit-Reset": str(resets_after[k]),
        }
        return True, hdr


limiter = SlidingWindowLimiter(windows=((200, 60),))


def client_id(req: Request) -> str:
    api_key = req.headers.get("X-Mock-Key")
    if api_key:
        return f"key:{api_key}"
    # atrás de proxy, pegue o primeiro IP de X-Forwarded-For
    xff = req.headers.get("X-Forwarded-For")
    if xff:
        return f"ip:{xff.split(',')[0].strip()}"
    return f"ip:{req.client.host}"


@app.middleware("http")
async def ratelimit_middleware(request: Request, call_next):
    ok, hdrs = limiter.check_and_commit(client_id(request))
    if not ok:
        return JSONResponse(
            {"detail": "rate limit exceeded"}, status_code=429, headers=hdrs
        )

    response = await call_next(request)
    for k, v in hdrs.items():
        # no 200 não envie Retry-After
        if k == "Retry-After":
            continue
        response.headers[k] = v

    # opcional: espelhar cabeçalhos legados X-RateLimit-*
    response.headers.setdefault(
        "X-RateLimit-Limit", response.headers["RateLimit-Limit"]
    )
    response.headers.setdefault(
        "X-RateLimit-Remaining", response.headers["RateLimit-Remaining"]
    )
    response.headers.setdefault(
        "X-RateLimit-Reset", response.headers["RateLimit-Reset"]
    )
    return response


@app.api_route(
    "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
)
async def mock():
    return {"ok": True}


if __name__ == "__main__":
    log_config = copy.deepcopy(UVICORN_LOGGING_CONFIG)
    for name in ("default", "access"):
        fmt = log_config["formatters"][name]["fmt"]
        log_config["formatters"][name]["fmt"] = "%(asctime)s " + fmt
        log_config["formatters"][name]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    uvicorn.run(
        "limited_mock:app",
        host="0.0.0.0",
        port=8001,
        log_config=log_config,
        reload=True,
        workers=1,
    )
