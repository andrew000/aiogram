"""Benchmark aiogram client session transports against a local Bot API-shaped server.

This script intentionally uses public Bot/session APIs, including method validation,
multipart upload construction, and stream_content(), so timings reflect aiogram's
actual session integration overhead rather than only the native HTTP clients.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from aiohttp import web

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.client.session.rust import RustSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.methods import GetMe, TelegramMethod
from aiogram.types import BufferedInputFile

TOKEN = "42:TEST"


class ComplexMethod(TelegramMethod[bool]):
    __api_method__ = "complexMethod"
    __returning__ = bool

    chat_id: int
    text: str
    disable_notification: bool
    reply_markup: dict[str, Any]
    entities: list[dict[str, Any]]


class UploadMethod(TelegramMethod[bool]):
    __api_method__ = "uploadMethod"
    __returning__ = bool

    chat_id: int
    caption: str
    document: BufferedInputFile


@dataclass(frozen=True)
class BenchResult:
    session: str
    scenario: str
    requests: int
    concurrency: int
    total_seconds: float
    requests_per_second: float
    mean_ms: float
    median_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float


@dataclass
class ServerStats:
    api_requests: int = 0
    request_bytes: int = 0
    download_requests: int = 0


class BenchmarkServer:
    def __init__(self, download_size: int) -> None:
        self.download_body = b"x" * download_size
        self.stats = ServerStats()
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.url: str | None = None

    async def __aenter__(self) -> BenchmarkServer:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self.handle_api)
        app.router.add_get("/file/bot{token}/{path:.*}", self.handle_file)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sockets = self.site._server.sockets
        port = sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    async def handle_api(self, request: web.Request) -> web.Response:
        self.stats.api_requests += 1
        method = request.match_info["method"]

        body = await request.read()
        self.stats.request_bytes += len(body)

        if method == "getMe":
            result: dict[str, Any] | bool = {
                "id": 42,
                "is_bot": True,
                "first_name": "Benchmark",
                "username": "benchmark_bot",
            }
        else:
            result = True

        return web.json_response({"ok": True, "result": result})

    async def handle_file(self, request: web.Request) -> web.Response:
        self.stats.download_requests += 1
        return web.Response(body=self.download_body)


def make_complex_method() -> ComplexMethod:
    return ComplexMethod(
        chat_id=42,
        text="Hello from benchmark " * 8,
        disable_notification=True,
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "Open", "url": "https://example.com"},
                    {"text": "Callback", "callback_data": "bench:1"},
                ],
            ],
        },
        entities=[
            {"type": "bold", "offset": 0, "length": 5},
            {"type": "italic", "offset": 6, "length": 4},
        ],
    )


def make_upload_method(upload_size: int) -> UploadMethod:
    return UploadMethod(
        chat_id=42,
        caption="upload benchmark",
        document=BufferedInputFile(b"x" * upload_size, filename="benchmark.bin"),
    )


async def run_timed(
    *,
    requests: int,
    concurrency: int,
    operation: Callable[[], Awaitable[object]],
) -> tuple[float, list[float]]:
    latencies: list[float] = []
    latencies_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(concurrency)

    async def worker() -> None:
        async with semaphore:
            start = time.perf_counter_ns()
            await operation()
            elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
            async with latencies_lock:
                latencies.append(elapsed_ms)

    start = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(requests)))
    total = time.perf_counter() - start
    return total, latencies


async def measure(
    *,
    session_name: str,
    scenario: str,
    requests: int,
    concurrency: int,
    operation: Callable[[], Awaitable[object]],
) -> BenchResult:
    total, latencies = await run_timed(
        requests=requests,
        concurrency=concurrency,
        operation=operation,
    )
    sorted_latencies = sorted(latencies)

    def percentile(percent: float) -> float:
        if not sorted_latencies:
            return 0.0
        index = min(len(sorted_latencies) - 1, round((len(sorted_latencies) - 1) * percent))
        return sorted_latencies[index]

    return BenchResult(
        session=session_name,
        scenario=scenario,
        requests=requests,
        concurrency=concurrency,
        total_seconds=total,
        requests_per_second=requests / total,
        mean_ms=statistics.fmean(latencies),
        median_ms=statistics.median(latencies),
        p95_ms=percentile(0.95),
        p99_ms=percentile(0.99),
        min_ms=min(latencies),
        max_ms=max(latencies),
    )


async def warmup(operation: Callable[[], Awaitable[object]], count: int) -> None:
    for _ in range(count):
        await operation()


async def run_session_benchmarks(
    *,
    session_name: str,
    session_factory: Callable[[TelegramAPIServer], BaseSession],
    api: TelegramAPIServer,
    base_url: str,
    requests: int,
    concurrency: int,
    warmup_requests: int,
    upload_size: int,
    chunk_size: int,
) -> list[BenchResult]:
    session = session_factory(api)
    bot = Bot(TOKEN, session=session)
    results: list[BenchResult] = []

    async def simple_request() -> object:
        return await bot(GetMe())

    async def complex_request() -> object:
        return await bot(make_complex_method())

    async def upload_request() -> object:
        return await bot(make_upload_method(upload_size))

    async def download_request() -> object:
        total = 0
        async for chunk in session.stream_content(
            f"{base_url}/file/bot{TOKEN}/benchmark.bin",
            timeout=30,
            chunk_size=chunk_size,
            raise_for_status=True,
        ):
            total += len(chunk)
        return total

    scenarios: list[tuple[str, Callable[[], Awaitable[object]]]] = [
        ("make_request:getMe", simple_request),
        ("make_request:complex_payload", complex_request),
        ("make_request:upload", upload_request),
        ("stream_content:download", download_request),
    ]

    try:
        for scenario, operation in scenarios:
            await warmup(operation, warmup_requests)
            gc.collect()
            results.append(
                await measure(
                    session_name=session_name,
                    scenario=scenario,
                    requests=requests,
                    concurrency=concurrency,
                    operation=operation,
                )
            )
    finally:
        await bot.session.close()

    return results


def print_table(results: list[BenchResult]) -> None:
    headers = [
        "session",
        "scenario",
        "req",
        "conc",
        "rps",
        "mean",
        "median",
        "p95",
        "p99",
        "min",
        "max",
    ]
    rows = [
        [
            result.session,
            result.scenario,
            str(result.requests),
            str(result.concurrency),
            f"{result.requests_per_second:.1f}",
            f"{result.mean_ms:.3f}",
            f"{result.median_ms:.3f}",
            f"{result.p95_ms:.3f}",
            f"{result.p99_ms:.3f}",
            f"{result.min_ms:.3f}",
            f"{result.max_ms:.3f}",
        ]
        for result in results
    ]
    widths = [max(len(row[index]) for row in [headers, *rows]) for index in range(len(headers))]
    sys.stdout.write(
        " | ".join(value.ljust(widths[index]) for index, value in enumerate(headers)) + "\n"
    )
    sys.stdout.write("-+-".join("-" * width for width in widths) + "\n")
    for row in rows:
        sys.stdout.write(
            " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)) + "\n"
        )


def print_context(args: argparse.Namespace) -> None:
    sys.stdout.write(
        f"requests={args.requests} concurrency={args.concurrency} warmup={args.warmup} "
        f"upload_size={args.upload_size} download_size={args.download_size}\n"
    )


def print_comparison(results: list[BenchResult]) -> None:
    by_scenario: dict[str, dict[str, BenchResult]] = {}
    for result in results:
        by_scenario.setdefault(result.scenario, {})[result.session] = result

    rows: list[list[str]] = []
    for scenario, sessions in by_scenario.items():
        aiohttp_result = sessions.get("aiohttp")
        rust_result = sessions.get("rust")
        if aiohttp_result is None or rust_result is None:
            continue
        rows.append(
            [
                scenario,
                f"{rust_result.requests_per_second / aiohttp_result.requests_per_second:.2f}x",
                f"{aiohttp_result.mean_ms / rust_result.mean_ms:.2f}x",
                f"{aiohttp_result.p95_ms / rust_result.p95_ms:.2f}x",
            ]
        )

    if not rows:
        return

    headers = ["scenario", "rust/aiohttp rps", "aiohttp/rust mean", "aiohttp/rust p95"]
    widths = [max(len(row[index]) for row in [headers, *rows]) for index in range(len(headers))]
    sys.stdout.write("\nComparison\n")
    sys.stdout.write(
        " | ".join(value.ljust(widths[index]) for index, value in enumerate(headers)) + "\n"
    )
    sys.stdout.write("-+-".join("-" * width for width in widths) + "\n")
    for row in rows:
        sys.stdout.write(
            " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)) + "\n"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark aiogram client sessions.")
    parser.add_argument("--requests", type=int, default=1_000)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--upload-size", type=int, default=256 * 1024)
    parser.add_argument("--download-size", type=int, default=1024 * 1024)
    parser.add_argument("--chunk-size", type=int, default=64 * 1024)
    parser.add_argument(
        "--sessions",
        nargs="+",
        choices=["aiohttp", "rust"],
        default=["aiohttp", "rust"],
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a table.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with BenchmarkServer(download_size=args.download_size) as server:
        if server.url is None:
            raise RuntimeError("Benchmark server did not start")

        api = TelegramAPIServer.from_base(server.url)
        session_factories: dict[str, Callable[[TelegramAPIServer], BaseSession]] = {
            "aiohttp": lambda api: AiohttpSession(api=api),
            "rust": lambda api: RustSession(api=api),
        }

        results: list[BenchResult] = []
        for session_name in args.sessions:
            results.extend(
                await run_session_benchmarks(
                    session_name=session_name,
                    session_factory=session_factories[session_name],
                    api=api,
                    base_url=server.url,
                    requests=args.requests,
                    concurrency=args.concurrency,
                    warmup_requests=args.warmup,
                    upload_size=args.upload_size,
                    chunk_size=args.chunk_size,
                )
            )

    if args.json:
        sys.stdout.write(json.dumps([asdict(result) for result in results], indent=2) + "\n")
    else:
        print_context(args)
        print_table(results)
        print_comparison(results)


if __name__ == "__main__":
    asyncio.run(main())
