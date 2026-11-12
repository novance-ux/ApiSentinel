"""Evaluate each apitest strategy against examples/buggy_api using BUGS.md as ground truth.

Runs in-process (httpx ASGITransport), with a fresh app for every strategy group, so no
server is needed and groups cannot affect each other. Prints a markdown table.

    uv run python scripts/evaluate.py [--seed 0] [--output docs/evaluation.md] [--llm]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apitest.config import ApiKeyAuth, RunConfig  # noqa: E402
from apitest.evaluation import Score, load_bugs, render_markdown, score  # noqa: E402
from apitest.models import ApiSpec, RunReport, Strategy  # noqa: E402
from apitest.session import run_session  # noqa: E402
from apitest.spec.loader import load_spec  # noqa: E402
from examples.buggy_api.app import API_KEY, create_app  # noqa: E402

BASE_URL = "http://buggy.test"
GROUPS: list[tuple[str, list[Strategy]]] = [
    ("schema-only (positive + security)", [Strategy.POSITIVE, Strategy.SECURITY]),
    ("negative/boundary", [Strategy.NEGATIVE, Strategy.BOUNDARY]),
    ("stateful", [Strategy.STATEFUL]),
    (
        "all combined",
        [
            Strategy.POSITIVE,
            Strategy.SECURITY,
            Strategy.STATEFUL,
            Strategy.NEGATIVE,
            Strategy.BOUNDARY,
        ],
    ),
]
LLM_GROUPS: list[tuple[str, list[Strategy]]] = [
    ("llm only", [Strategy.LLM]),
    (
        "all combined + llm",
        [
            Strategy.POSITIVE,
            Strategy.SECURITY,
            Strategy.STATEFUL,
            Strategy.NEGATIVE,
            Strategy.BOUNDARY,
            Strategy.LLM,
        ],
    ),
]


async def evaluate_group(spec: ApiSpec, strategies: list[Strategy], seed: int) -> RunReport:
    config = RunConfig(
        base_url=BASE_URL,
        auth=ApiKeyAuth(type="api_key", value=API_KEY),
        strategies=strategies,
        seed=seed,
    )
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, timeout=10)  # run_session opens/closes it
    return await run_session(spec, config, BASE_URL, client=client)


async def main_async(seed: int, llm: bool) -> list[Score]:
    spec = load_spec(str(ROOT / "examples" / "specs" / "buggy_api.yaml"))
    bugs = load_bugs(ROOT / "examples" / "buggy_api" / "BUGS.md")
    scores = []
    for label, strategies in GROUPS + (LLM_GROUPS if llm else []):
        report = await evaluate_group(spec, strategies, seed)
        skipped = next((n for n in report.notes if n.startswith("llm: skipped")), None)
        if skipped:
            label = f"{label} (NOT RUN: {skipped.removeprefix('llm: skipped ').strip('()')})"
        scores.append(score(label, report, spec, bugs))
        print(f"evaluated {label}", file=sys.stderr)
    return scores


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--llm", action="store_true", help="also evaluate LLM-generated cases")
    args = parser.parse_args()
    scores = asyncio.run(main_async(args.seed, args.llm))
    bugs = load_bugs(ROOT / "examples" / "buggy_api" / "BUGS.md")
    markdown = f"Seed {args.seed}. {len(bugs)} injected bugs.\n\n" + render_markdown(scores, bugs)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # Windows consoles
    print(markdown)


if __name__ == "__main__":
    main()
