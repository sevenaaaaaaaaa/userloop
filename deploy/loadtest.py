#!/usr/bin/env python
"""UserLoop 轻量压测：ingest 写入 + overview 读取，输出 RPS 与 p50/p95/p99.

用法（在服务器上跑，避免走 Cloudflare）：
    .venv/bin/python deploy/loadtest.py --base http://127.0.0.1:8600/userloop \\
        --token <api_token> --n 2000 --concurrency 20
    .venv/bin/python deploy/loadtest.py --base http://127.0.0.1:8600/userloop \\
        --token <api_token> --mode read --n 500 --concurrency 10

注意：会向目标写入压测事件（distinct_id 前缀 `loadtest_`），生产环境请自行评估或事后清理。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time

import httpx


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
    return s[k]


async def worker(client: httpx.AsyncClient, args: argparse.Namespace, idx: int,
                 lat: list[float], errs: list[str]) -> None:
    headers = {"X-UserLoop-Token": args.token} if args.token else {}
    for i in range(args.n // args.concurrency + (1 if idx < args.n % args.concurrency else 0)):
        t0 = time.perf_counter()
        try:
            if args.mode == "read":
                r = await client.get(f"{args.base}/api/v1/overview", headers=headers)
            else:
                uid = f"loadtest_{random.randint(0, max(1, args.users) - 1)}"
                r = await client.post(f"{args.base}/api/v1/ingest", headers=headers, json={
                    "distinct_id": uid, "event": "loadtest_event",
                    "props": {"i": i, "ts": time.time()}, "source": "loadtest"})
            if r.status_code >= 400:
                errs.append(f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            errs.append(type(exc).__name__)
        lat.append((time.perf_counter() - t0) * 1000)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8600/userloop")
    ap.add_argument("--token", default="")
    ap.add_argument("--mode", choices=("write", "read"), default="write")
    ap.add_argument("--n", type=int, default=1000, help="总请求数")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--users", type=int, default=100, help="写入模式的用户基数")
    args = ap.parse_args()

    lat: list[float] = []
    errs: list[str] = []
    limits = httpx.Limits(max_connections=args.concurrency * 2,
                          max_keepalive_connections=args.concurrency)
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=30, limits=limits) as client:
        await asyncio.gather(*(worker(client, args, i, lat, errs) for i in range(args.concurrency)))
    dur = time.perf_counter() - t0

    total = len(lat)
    print(f"模式={args.mode} 并发={args.concurrency} 请求={total} 用时={dur:.2f}s")
    print(f"RPS={total / dur:.1f}  错误={len(errs)}")
    if lat:
        print(f"p50={pct(lat, 50):.1f}ms  p95={pct(lat, 95):.1f}ms  p99={pct(lat, 99):.1f}ms  "
              f"max={max(lat):.1f}ms  mean={statistics.fmean(lat):.1f}ms")
    if errs:
        kinds: dict[str, int] = {}
        for e in errs:
            kinds[e] = kinds.get(e, 0) + 1
        print("错误分布:", kinds)


if __name__ == "__main__":
    asyncio.run(main())
