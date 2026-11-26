# -*- coding: utf-8 -*-
"""Standalone probe harness for the async engine.

Phase 1 deliberately does NOT submit TikTok reports. It exercises the
transport layer against a configurable URL (httpbin by default) so the worker
pool, proxy pool, retries and shutdown path can be validated safely.

Examples:
    python -m core.async_engine.runner --requests 10 --workers 2
    python -m core.async_engine.runner --url https://www.tiktok.com/@tiktok \
        --requests 3 --workers 1
    python -m core.async_engine.runner --direct --url http://127.0.0.1:8080/ok
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .engine import AsyncReportEngine
from .events import EventBus
from .models import Job, Outcome, Stats, load_proxy_lines
from .proxy_pool import AsyncProxyPool, NoProxyAvailable
from .rate import TokenBucket
from .session_pool import (
    NoSessionAvailable,
    RotationStrategy,
    SessionHandle,
    SessionPool,
)
from .templates import load_templates_from_file, render
from .transport import DEFAULT_IMPERSONATE

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_PROXY_FILE = BASE_DIR / "data" / "proxies.txt"
DEFAULT_SESSION_FILE = BASE_DIR / "data" / "saved_sessions.json"

# core.gemini_client configures root logging at INFO; keep httpx chatter out of the UI.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

console = Console()

_OUTCOME_COLOUR = {
    Outcome.OK: "green",
    Outcome.WAF: "yellow",
    Outcome.CAPTCHA: "yellow",
    Outcome.RATE_LIMIT: "yellow",
    Outcome.PROXY_ERROR: "red",
    Outcome.TIMEOUT: "red",
    Outcome.NETWORK: "red",
    Outcome.HTTP_ERROR: "red",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.async_engine.runner",
        description="THUNDER async engine probe harness (safe, no reports submitted)",
    )
    parser.add_argument("--proxy-file", default=str(DEFAULT_PROXY_FILE))
    parser.add_argument("--direct", action="store_true", help="bypass proxies (tests only)")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--requests", type=int, default=10, dest="request_count")
    parser.add_argument("--url", default="https://httpbin.org/ip")
    parser.add_argument("--method", default="GET", choices=["GET", "POST"])
    parser.add_argument("--health-url", default="https://httpbin.org/ip")
    parser.add_argument("--health-timeout", type=float, default=15.0)
    parser.add_argument(
        "--impersonate",
        default=DEFAULT_IMPERSONATE,
        help="curl_cffi browser profile for the TLS/HTTP2 fingerprint "
        "(e.g. chrome, chrome131, safari, firefox; empty disables)",
    )
    parser.add_argument(
        "--health-interval",
        type=float,
        default=300.0,
        help="seconds between background health checks (0 disables)",
    )
    parser.add_argument("--no-validate", action="store_true")
    parser.add_argument("--per-proxy-concurrency", type=int, default=2)
    parser.add_argument("--max-soft-failures", type=int, default=10)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument(
        "--rate",
        type=float,
        default=60.0,
        help="global requests per minute (0 disables)",
    )
    parser.add_argument(
        "--worker-jitter",
        type=float,
        nargs=2,
        default=(0.0, 0.0),
        metavar=("MIN", "MAX"),
    )
    # Session pool options
    parser.add_argument(
        "--session-file",
        default=str(DEFAULT_SESSION_FILE),
        help="path to saved_sessions.json",
    )
    parser.add_argument(
        "--session-strategy",
        choices=[s.value for s in RotationStrategy],
        default=RotationStrategy.ROUND_ROBIN.value,
        help="session rotation strategy",
    )
    parser.add_argument(
        "--no-sessions",
        action="store_true",
        help="disable session pool (run anonymously)",
    )
    parser.add_argument("--dry-run", action="store_true")
    # Template options
    parser.add_argument(
        "--template-file",
        type=str,
        help="path to YAML/JSON template file",
    )
    parser.add_argument(
        "--template-name",
        type=str,
        help="name of template to use from template file",
    )
    return parser


def _print_proxies(pool: AsyncProxyPool) -> None:
    table = Table(title="Proxy pool", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("proxy")
    table.add_column("healthy", justify="center")
    table.add_column("score", justify="right")
    table.add_column("latency", justify="right")
    table.add_column("ok/hard/soft", justify="right")
    for idx, row in enumerate(pool.snapshot(), 1):
        table.add_row(
            str(idx),
            row["label"],
            "yes" if row["enabled"] else "NO",
            f"{row['score']:.3f}",
            "—" if row["latency_ms"] is None else f"{row['latency_ms']} ms",
            f"{row['successes']}/{row['failures']}/{row['soft_failures']}",
        )
    console.print(table)


def _print_sessions(pool: SessionPool) -> None:
    table = Table(title="Session pool", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("label")
    table.add_column("identifier")
    table.add_column("uses", justify="right")
    table.add_column("success_rate", justify="right")
    table.add_column("status")
    for idx, row in enumerate(pool.stats()["sessions"], 1):
        table.add_row(
            str(idx),
            row["label"] or "—",
            row["identifier"] or "—",
            str(row["uses"]),
            f"{row['success_rate']:.1%}",
            "DISABLED" if row["disabled"] else "active",
        )
    console.print(table)


def _print_stats(stats: Stats) -> None:
    table = Table(title="Run summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("total", str(stats.total))
    table.add_row("succeeded", str(stats.ok))
    table.add_row("failed", str(stats.failed))
    table.add_row("success rate", f"{stats.success_rate:.1%}")
    table.add_row("avg latency", f"{stats.avg_latency_ms:.0f} ms")
    table.add_row("duration", f"{stats.duration_s:.1f} s")
    table.add_row(
        "outcomes",
        ", ".join(f"{k}={v}" for k, v in sorted(stats.by_outcome.items())) or "—",
    )
    console.print(table)


async def _consume(bus: EventBus) -> None:
    async for event in bus:
        if event.kind == "log":
            console.print(f"[dim]{event.message}[/dim]")
            continue
        result = event.result
        if result is None:
            continue
        colour = _OUTCOME_COLOUR.get(result.outcome, "white")
        status = result.status_code if result.status_code is not None else "—"
        proxy = result.proxy if len(result.proxy) <= 44 else result.proxy[:41] + "…"
        detail = f"  [dim]{result.error}[/dim]" if result.error else ""
        console.print(
            f"[{colour}]{result.outcome.value:>10}[/{colour}] "
            f"{result.label:<24} {status:>4} "
            f"{result.latency_ms:7.0f} ms  {proxy}{detail}"
        )


async def run(args: argparse.Namespace) -> int:
    # The event bus must exist before anything can register an on_disable
    # callback: those callbacks are invoked at any time during the run.
    bus = EventBus()

    proxies = [] if args.direct else await asyncio.to_thread(load_proxy_lines, args.proxy_file)
    if not proxies and not args.direct:
        console.print(f"[red]No proxies parsed from {args.proxy_file}[/red]")
        return 2

    # Load session pool
    session_pool = None
    if not args.no_sessions:
        try:
            session_pool = await asyncio.to_thread(
                SessionPool.from_file,
                args.session_file,
                strategy=RotationStrategy(args.session_strategy),
                only_active=True,
                on_disable=lambda s, r: bus.log(f"session disabled ({r}): {s.label or s.identifier}"),
            )
            if session_pool.get_available():
                console.print(f"[green]Loaded {len(session_pool.get_available())} active sessions[/green]")
            else:
                console.print("[yellow]No active sessions found in session file[/yellow]")
                session_pool = None
        except Exception as e:
            console.print(f"[yellow]Failed to load sessions: {e}[/yellow]")
            session_pool = None

    # Load templates
    template = None
    if args.template_file:
        try:
            templates = await asyncio.to_thread(load_templates_from_file, args.template_file)
            if not templates:
                console.print(f"[yellow]No templates found in {args.template_file}[/yellow]")
            elif args.template_name:
                if args.template_name not in templates:
                    console.print(f"[red]Template '{args.template_name}' not found. Available: {list(templates.keys())}[/red]")
                    return 2
                template = templates[args.template_name]
            else:
                # Use first template if not specified
                template = next(iter(templates.values()))
            console.print(f"[green]Loaded template: {template.name or 'unnamed'}[/green]")
        except Exception as e:
            console.print(f"[red]Failed to load templates: {e}[/red]")
            return 2

    if args.dry_run:
        console.print(f"proxy file: {args.proxy_file}")
        console.print(f"tls impersonation: {args.impersonate or 'disabled'}")
        for cfg in proxies:
            console.print(f"  {cfg.label}")
        if session_pool:
            console.print(f"session file: {args.session_file}")
            console.print(f"session strategy: {args.session_strategy}")
            for s in session_pool.get_available():
                console.print(f"  {s.label or s.identifier}")
        if template:
            console.print(f"template: {template.name or 'unnamed'} ({template.method} {template.path})")
        console.print(f"direct mode: {args.direct}")
        console.print(f"jobs: {args.request_count} × {args.method} {args.url}")
        return 0

    pool = AsyncProxyPool(
        proxies,
        allow_direct=args.direct,
        health_url=args.health_url,
        health_timeout=args.health_timeout,
        per_proxy_concurrency=args.per_proxy_concurrency,
        max_soft_failures=args.max_soft_failures,
        impersonate=args.impersonate or None,
        on_disable=lambda label: bus.log(f"proxy disabled after repeated failures: {label}"),
    )
    consumer = asyncio.create_task(_consume(bus), name="thunder-events")
    health_stop: asyncio.Event | None = None
    health_task: asyncio.Task | None = None

    try:
        await pool.start(validate=not args.no_validate)
        _print_proxies(pool)
        if session_pool:
            _print_sessions(session_pool)
        if args.health_interval and args.health_interval > 0:
            health_stop = asyncio.Event()
            health_task = asyncio.create_task(
                pool.health_loop(health_stop, interval=args.health_interval),
                name="thunder-health",
            )

        rate = TokenBucket(args.rate) if args.rate and args.rate > 0 else None
        engine = AsyncReportEngine(
            pool,
            session_pool=session_pool,
            workers=args.workers,
            max_retries=args.max_retries,
            rate=rate,
            events=bus,
            worker_jitter=tuple(args.worker_jitter),
        )

        # Create jobs - use template if provided
        if template:
            # Pre-acquire exactly one session per job. The handle is attached
            # to the job so the identity baked into the rendered request is the
            # same one the engine releases with the result: rotation order stays
            # aligned with the job list and no session is borrowed twice.
            count = max(1, args.request_count)
            handles: list[SessionHandle | None] = [None] * count
            if session_pool and session_pool.get_available():
                for idx in range(count):
                    try:
                        handles[idx] = await session_pool.acquire()
                    except NoSessionAvailable:
                        break

            jobs = []
            for idx in range(count):
                handle = handles[idx]
                job = render(template, {}, session=handle.config if handle else None)
                job.name = f"{template.name or 'probe'}-{idx + 1}"
                if handle is not None:
                    job.meta["session_handle"] = handle
                jobs.append(job)
        else:
            jobs = [
                Job(
                    url=args.url,
                    method=args.method,
                    name=f"probe-{idx + 1}",
                    headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                for idx in range(max(1, args.request_count))
            ]

        stats = await engine.run(jobs)
        _print_stats(stats)
        if session_pool:
            console.print("\n[bold]Session Pool Final Stats:[/bold]")
            _print_sessions(session_pool)
        return 0 if stats.ok > 0 else 1
    except NoProxyAvailable as exc:
        console.print(f"[red]{exc}[/red]")
        return 3
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/yellow]")
        return 130
    finally:
        if health_stop is not None:
            health_stop.set()
        if health_task is not None:
            # Cancel instead of waiting: a health check in flight can block on
            # the network for health_timeout and stall shutdown with it.
            health_task.cancel()
            await asyncio.gather(health_task, return_exceptions=True)
        bus.close()
        await asyncio.gather(consumer, return_exceptions=True)
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
