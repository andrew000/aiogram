from __future__ import annotations

import argparse
import gc
import importlib
import json
import statistics
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

try:
    orjson: Any | None = importlib.import_module("orjson")
except ImportError:  # pragma: no cover
    orjson = None

try:
    msgspec_json: Any | None = importlib.import_module("msgspec.json")
except ImportError:  # pragma: no cover
    msgspec_json = None

try:
    ujson: Any | None = importlib.import_module("ujson")
except ImportError:  # pragma: no cover
    ujson = None

from aiogram import Bot
from aiogram.methods import DeleteMessage, GetMe, GetUpdates, Response, TelegramMethod


@dataclass(frozen=True)
class Case:
    name: str
    method: TelegramMethod[Any]
    content: str


@dataclass(frozen=True)
class Result:
    name: str
    old_ns: float
    orjson_ns: float | None
    msgspec_ns: float | None
    ujson_ns: float | None
    new_ns: float
    ratio: float


def make_cases() -> list[Case]:
    updates = [
        {
            "update_id": 100_000 + i,
            "message": {
                "message_id": i,
                "from": {
                    "id": 42,
                    "is_bot": False,
                    "first_name": "User",
                },
                "chat": {
                    "id": 42,
                    "type": "private",
                },
                "date": 1_700_000_000 + i,
                "text": "hello",
            },
        }
        for i in range(100)
    ]

    return [
        Case(
            name="bool result",
            method=DeleteMessage(chat_id=42, message_id=42),
            content=json.dumps({"ok": True, "result": True}),
        ),
        Case(
            name="object result",
            method=GetMe(),
            content=json.dumps(
                {
                    "ok": True,
                    "result": {
                        "id": 42,
                        "is_bot": True,
                        "first_name": "Test",
                        "username": "test_bot",
                    },
                }
            ),
        ),
        Case(
            name="update list",
            method=GetUpdates(),
            content=json.dumps(
                {
                    "ok": True,
                    "result": updates,
                }
            ),
        ),
        Case(
            name="error response",
            method=DeleteMessage(chat_id=42, message_id=42),
            content=json.dumps(
                {
                    "ok": False,
                    "description": "Too Many Requests: retry after 1",
                    "parameters": {"retry_after": 1},
                }
            ),
        ),
    ]


def ns_per_call(
    function: Callable[[], Any],
    *,
    repeats: int,
    iterations: int,
    disable_gc: bool,
) -> float:
    samples: list[float] = []
    for _ in range(repeats):
        if disable_gc:
            gc.disable()
        try:
            start = time.perf_counter_ns()
            for _ in range(iterations):
                function()
            end = time.perf_counter_ns()
        finally:
            if disable_gc:
                gc.enable()
        samples.append((end - start) / iterations)
    return statistics.median(samples)


def benchmark_case(
    case: Case,
    *,
    bot: Bot,
    repeats: int,
    iterations: int,
    disable_gc: bool,
) -> Result:
    returning: Any = case.method.__returning__
    response_type: Any = Response[returning]
    context = {"bot": bot}

    def old_variant() -> Any:
        data = json.loads(case.content)
        return response_type.model_validate(data, context=context)

    def new_variant() -> Any:
        return response_type.model_validate_json(case.content, context=context)

    variants: list[tuple[str, Callable[[], Any]]] = [
        ("json", old_variant),
        ("pydantic", new_variant),
    ]
    if orjson is not None:

        def orjson_variant() -> Any:
            data = orjson.loads(case.content)
            return response_type.model_validate(data, context=context)

        variants.append(("orjson", orjson_variant))
    if msgspec_json is not None:

        def msgspec_variant() -> Any:
            data = msgspec_json.decode(case.content)
            return response_type.model_validate(data, context=context)

        variants.append(("msgspec", msgspec_variant))
    if ujson is not None:

        def ujson_variant() -> Any:
            data = ujson.loads(case.content)
            return response_type.model_validate(data, context=context)

        variants.append(("ujson", ujson_variant))

    # Warmup
    for _ in range(1_000):
        for _, variant in variants:
            variant()

    timings = {
        name: ns_per_call(
            variant,
            repeats=repeats,
            iterations=iterations,
            disable_gc=disable_gc,
        )
        for name, variant in variants
    }
    old_ns = timings["json"]
    new_ns = timings["pydantic"]
    return Result(
        name=case.name,
        old_ns=old_ns,
        orjson_ns=timings.get("orjson"),
        msgspec_ns=timings.get("msgspec"),
        ujson_ns=timings.get("ujson"),
        new_ns=new_ns,
        ratio=old_ns / new_ns,
    )


def print_results(results: Iterable[Result]) -> None:
    header = (
        f"{'case':<16} "
        f"{'json ns/op':>12} "
        f"{'orjson ns/op':>12} "
        f"{'msgspec ns/op':>13} "
        f"{'ujson ns/op':>12} "
        f"{'pydantic ns/op':>14} "
        f"{'json/pyd':>9} "
        f"{'orjson/pyd':>10}"
        f" {'msgspec/pyd':>11}"
        f" {'ujson/pyd':>10}"
    )
    lines = [header, "-" * len(header)]
    lines.extend(
        (
            f"{result.name:<16} "
            f"{result.old_ns:>12.1f} "
            f"{format_optional_ns(result.orjson_ns):>12} "
            f"{format_optional_ns(result.msgspec_ns):>13} "
            f"{format_optional_ns(result.ujson_ns):>12} "
            f"{result.new_ns:>14.1f} "
            f"{result.ratio:>8.2f}x "
            f"{format_optional_ratio(result.orjson_ns, result.new_ns):>10}"
            f" {format_optional_ratio(result.msgspec_ns, result.new_ns):>11}"
            f" {format_optional_ratio(result.ujson_ns, result.new_ns):>10}"
        )
        for result in results
    )
    if orjson is None:
        lines.append("")
        lines.append("orjson is not installed; install it to include the orjson column.")
    if msgspec_json is None:
        lines.append("")
        lines.append("msgspec is not installed; install it to include the msgspec column.")
    if ujson is None:
        lines.append("")
        lines.append("ujson is not installed; install it to include the ujson column.")
    sys.stdout.write("\n".join(lines))
    sys.stdout.write("\n")


def format_optional_ns(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}"


def format_optional_ratio(value: float | None, base: float) -> str:
    if value is None:
        return "n/a"
    return f"{value / base:.2f}x"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare json.loads(...)+model_validate(...) with Pydantic model_validate_json(...)."
        )
    )
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument(
        "--disable-gc",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Disable garbage collection during timed loops.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bot = Bot("42:TEST")
    results = [
        benchmark_case(
            case,
            bot=bot,
            repeats=args.repeats,
            iterations=args.iterations,
            disable_gc=args.disable_gc,
        )
        for case in make_cases()
    ]
    print_results(results)


if __name__ == "__main__":
    main()
