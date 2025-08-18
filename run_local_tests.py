import asyncio
import time
from typing import Tuple

import httpx
from uvicorn import Config, Server

import limited_mock
import access_api


async def wait_http_ready(url: str, timeout: float = 10.0) -> None:
    start = time.monotonic()
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=1.0) as c:
        while time.monotonic() - start < timeout:
            try:
                r = await c.get(url)
                if r.status_code < 500:
                    return
            except Exception as e:  # noqa: BLE001
                last_err = e
            await asyncio.sleep(0.1)
    raise RuntimeError(f"Service not ready at {url}: {last_err}")


async def start_uvicorn_server(app, host: str, port: int) -> Tuple[Server, asyncio.Task]:
    cfg = Config(app=app, host=host, port=port, log_level="info")
    server = Server(cfg)
    task = asyncio.create_task(server.serve(), name=f"uvicorn:{host}:{port}")
    return server, task


async def run_load(n_reqs: int = 500) -> list[str]:
    min_limit = 200
    n_clients = 2
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:5812",
        http2=True,
        timeout=60 * (n_reqs / min_limit) * n_clients * 2,
        limits=httpx.Limits(max_connections=n_reqs, max_keepalive_connections=n_reqs),
    ) as aclient:
        async def get_it(path: str):
            return await aclient.get(path)

        results: list[str] = []
        for t in asyncio.as_completed([get_it("/hello") for _ in range(n_reqs)]):
            resp = await t
            remaining = resp.headers.get("ratelimit-remaining", "not_found")
            results.append(f"{resp.status_code} - {remaining}")
        return results


async def run_load_multi_callers(
    n_callers: int = 2,
    per_caller: int = 60,
    fairness_tolerance_pct: float = 0.25,
    fairness_abs_slack_s: float = 0.2,
) -> dict:
    """Dispara carga com múltiplos Callers (via header x-connection-id).

    Retorna um resumo com contagem por status e por caller.
    """
    total = n_callers * per_caller
    min_limit = 200
    n_clients = max(2, n_callers)
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:5812",
        http2=True,
        timeout=60 * (total / min_limit) * n_clients * 2,
        limits=httpx.Limits(max_connections=total, max_keepalive_connections=total),
    ) as aclient:
        async def get_it(path: str, caller_id: str):
            start = time.monotonic()
            resp = await aclient.get(path, headers={"x-connection-id": caller_id})
            elapsed = time.monotonic() - start
            return resp, elapsed

        caller_ids = [f"caller-{i+1}" for i in range(n_callers)]

        status_counts: dict[str, int] = {}
        by_caller_counts: dict[str, int] = {cid: 0 for cid in caller_ids}

        # Create tasks that return (cid, response) tuples to keep mapping stable
        tasks = []
        for cid in caller_ids:
            for _ in range(per_caller):
                async def req(caller_id=cid):
                    resp, elapsed = await get_it("/hello", caller_id)
                    return caller_id, resp, elapsed
                tasks.append(asyncio.create_task(req()))

        for t in asyncio.as_completed(tasks):
            cid, resp, elapsed = await t
            by_caller_counts[cid] = by_caller_counts.get(cid, 0) + 1
            code = str(resp.status_code)
            status_counts[code] = status_counts.get(code, 0) + 1

        # Compute average response time per caller
        # Re-run quickly to gather timings per caller without another HTTP pass by reusing above? We already captured per-task elapsed.
        # We maintained only aggregates for counts; reconstruct averages by iterating tasks results would require storing them.
        # Instead, gather again from the finished tasks stored in 'tasks' (their result is cached).
        per_caller_durations: dict[str, list[float]] = {cid: [] for cid in caller_ids}
        for t in tasks:
            cid, _resp, elapsed = await t
            per_caller_durations[cid].append(elapsed)

        avg_by_caller: dict[str, float] = {}
        for cid, arr in per_caller_durations.items():
            avg_by_caller[cid] = (sum(arr) / len(arr)) if arr else 0.0

        # Fairness check on mean latency across callers
        if avg_by_caller:
            max_avg = max(avg_by_caller.values())
            min_avg = min(avg_by_caller.values())
            rel_gap = (max_avg - min_avg) / max(min_avg, 1e-6)
            abs_gap = max_avg - min_avg
            fair_ok = abs_gap <= max(fairness_abs_slack_s, fairness_tolerance_pct * min_avg)
        else:
            rel_gap = 0.0
            abs_gap = 0.0
            fair_ok = True

        return {
            "total": total,
            "status_counts": status_counts,
            "by_caller_counts": by_caller_counts,
            "avg_latency_s_by_caller": avg_by_caller,
            "fair_latency_ok": fair_ok,
            "fair_latency_rel_gap": rel_gap,
            "fair_latency_abs_gap_s": abs_gap,
            "fair_params": {
                "tolerance_pct": fairness_tolerance_pct,
                "abs_slack_s": fairness_abs_slack_s,
            },
        }


async def main() -> None:
    # Start upstream mock and proxy servers
    mock_server, mock_task = await start_uvicorn_server(
        limited_mock.app, host="127.0.0.1", port=8001
    )
    proxy_server, proxy_task = await start_uvicorn_server(
        access_api.app, host="127.0.0.1", port=5812
    )

    try:
        # Wait for readiness
        await wait_http_ready("http://127.0.0.1:8001/healthz?ping=1", timeout=5.0)
    except Exception:
        # limited_mock has no healthz; just try root
        await wait_http_ready("http://127.0.0.1:8001/", timeout=5.0)

    await wait_http_ready("http://127.0.0.1:5812/__status", timeout=10.0)

    # Test 1: single caller load (baseline)
    started = time.monotonic()
    results = await run_load(n_reqs=200)  # keep test brief
    elapsed = time.monotonic() - started
    status_counts: dict[str, int] = {}
    for row in results:
        code = row.split(" ")[0]
        status_counts[code] = status_counts.get(code, 0) + 1
    print({"test": "single_caller", "elapsed_sec": round(elapsed, 2), "status_counts": status_counts})
    if any(k in ("429", "503") for k in status_counts):
        raise AssertionError(f"Unexpected upstream errors surfaced: {status_counts}")

    # Test 2: multi-caller fairness
    started = time.monotonic()
    multi = await run_load_multi_callers(n_callers=2, per_caller=60)
    elapsed2 = time.monotonic() - started
    print({"test": "multi_caller", "elapsed_sec": round(elapsed2, 2), **multi})
    if any(k in ("429", "503") for k in multi["status_counts"]):
        raise AssertionError(f"Unexpected upstream errors surfaced (multi): {multi}")
    if not multi["fair_latency_ok"]:
        raise AssertionError(
            "Unfair average latency across callers: "
            f"rel_gap={multi['fair_latency_rel_gap']:.2%}, "
            f"abs_gap_s={multi['fair_latency_abs_gap_s']:.3f}, "
            f"avg_by_caller={multi['avg_latency_s_by_caller']}"
        )

    # Done
    proxy_server.should_exit = True
    mock_server.should_exit = True
    await asyncio.gather(proxy_task, mock_task)


if __name__ == "__main__":
    asyncio.run(main())
