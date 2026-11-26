# -*- coding: utf-8 -*-
"""CLI for the single-account high-speed burst.

Examples:
    # 100 simultaneous requests for one account using data/proxies.txt
    python -m core.async_engine.burst_runner \\
        --url https://www.tiktok.com/@tiktok --account tiktok --requests 100

    # Empty proxy file -> pull from a pool endpoint, keep only <300ms exits
    python -m core.async_engine.burst_runner \\
        --url https://httpbin.org/ip --proxy-pool-url https://pool.example/list \\
        --max-latency-ms 300 --requests 200

    # Use a stored session for a real account-bound burst
    python -m core.async_engine.burst_runner \\
        --url https://example/api --session-id funnyfartslolll --requests 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from rich.console import Console
from rich.table import Table

from .burst import BurstConfig, BurstReport, SingleAccountBurst
from .proxy_sources import DEFAULT_PROXY_FILE, MAX_LATENCY_MS, ProxySource
from .session_pool import SessionPool
from .transport import DEFAULT_IMPERSONATE

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.async_engine.burst_runner",
        description="Fire many simultaneous requests at one account through many IPs",
    )
    parser.add_argument("--url", required=True, help="request URL to fan out")
    parser.add_argument("--account", default="", help="account label (for logs/stats)")
    parser.add_argument("--method", default="GET", choices=["GET", "POST"])
    parser.add_argument("--requests", type=int, default=50, help="total simultaneous requests")
    parser.add_argument("--proxy-file", default=DEFAULT_PROXY_FILE)
    parser.add_argument(
        "--proxy-pool-url",
        default="",
        help="proxy list endpoint used when the file has no usable proxy",
    )
    parser.add_argument(
        "--max-latency-ms",
        type=float,
        default=MAX_LATENCY_MS,
        help="reject proxies slower than this (default 300ms)",
    )
    parser.add_argument("--per-proxy-concurrency", type=int, default=8)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument(
        "--rate",
        type=float,
        default=0.0,
        help="optional global requests/minute cap (0 = unthrottled burst)",
    )
    parser.add_argument("--impersonate", default=DEFAULT_IMPERSONATE)
    parser.add_argument(
        "--session-id",
        default="",
        help="identifier/label of the Redis-backed session to use for this account",
    )
    parser.add_argument(
        "--redis-url",
        default="",
        help="Redis URL (defaults to REDIS_URL env or built-in default)",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="run without proxies (local tests / trusted egress only)",
    )
    parser.add_argument("--header", action="append", default=[], metavar="K:V")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    return parser


def _parse_headers(items: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in items:
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        if key.strip():
            headers[key.strip()] = value.strip()
    return headers


async def _resolve_session(redis_url: str, identifier: str):
    """Resolve a stored session from Redis (the only session source)."""
    if not identifier:
        return None
    try:
        from ..redis_manager import RedisSessionManager, get_redis_client
    except ImportError:
        from core.redis_manager import RedisSessionManager, get_redis_client

    mgr = RedisSessionManager(client=get_redis_client(redis_url or None))
    if not mgr.is_connected:
        console.print("[yellow]Redis unavailable; cannot resolve session[/yellow]")
        return None

    await asyncio.to_thread(mgr.refresh_sessions_from_redis)
    pool = SessionPool.from_redis(mgr)
    for config in pool.get_available():
        if identifier in (config.identifier, config.label):
            return config
    return None


def _print_report(report: BurstReport) -> None:
    if report.error:
        console.print(f"[bold red]burst failed:[/bold red] {report.error}")

    table = Table(title=f"Burst — {report.account or 'account'}")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("source", report.source)
    table.add_row("proxies used", str(report.proxies_used))
    table.add_row("rejected proxies", str(len(report.rejected_proxies)))
    table.add_row("requests", str(report.stats.total))
    table.add_row("ok", str(report.stats.ok))
    table.add_row("failed", str(report.stats.failed))
    table.add_row("success rate", f"{report.stats.success_rate:.1%}")
    table.add_row("avg latency", f"{report.stats.avg_latency_ms:.0f} ms")
    table.add_row("duration", f"{report.stats.duration_s:.2f} s")
    table.add_row("throughput", f"{report.requests_per_second:.1f} req/s")
    table.add_row(
        "outcomes",
        ", ".join(f"{k}={v}" for k, v in sorted(report.stats.by_outcome.items())) or "—",
    )
    console.print(table)

    if report.rejected_proxies:
        rejected = Table(title="Rejected proxies", show_lines=False)
        rejected.add_column("proxy")
        rejected.add_column("reason")
        for proxy, reason in report.rejected_proxies[:20]:
            rejected.add_row(proxy, reason)
        console.print(rejected)


async def run(args: argparse.Namespace) -> int:
    session = await _resolve_session(args.redis_url, args.session_id)
    if args.session_id and session is None:
        console.print(f"[yellow]session '{args.session_id}' not found; running without it[/yellow]")

    config = BurstConfig(
        requests=args.requests,
        per_proxy_concurrency=args.per_proxy_concurrency,
        max_latency_ms=args.max_latency_ms,
        max_retries=args.max_retries,
        rate_per_minute=args.rate,
        impersonate=args.impersonate or None,
        allow_direct=args.direct,
    )
    source = ProxySource(
        file_path=args.proxy_file,
        pool_url=args.proxy_pool_url,
        max_latency_ms=args.max_latency_ms,
        impersonate=args.impersonate or None,
    )
    report = await SingleAccountBurst(source, config).run(
        args.url,
        account=args.account,
        method=args.method,
        headers=_parse_headers(args.header),
        session=session,
        requests=args.requests,
    )

    if args.json:
        console.print_json(json.dumps(report.as_dict()))
    else:
        _print_report(report)
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
