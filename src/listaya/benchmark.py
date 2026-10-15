"""Extraction accuracy benchmark on the sample lists (PRD 13 and decision D-01).

    python -m listaya.benchmark
    python -m listaya.benchmark --text-model GLM-5.3-Flash --vision-model MiMo-V2.6-Flash

Extracts every sample list with the real Token Factory models and compares each row
with the hand-labeled expected result in samples/ferreteria/esperado.json: a row is
correct when its description matches and both its cost and VAT convention are exact.
Reports accuracy, time, model calls, tokens and cost per list.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from rapidfuzz import fuzz

from listaya.extraction.rows import ExtractedRow
from listaya.matching import normalize_description

SAMPLES = Path("samples") / "ferreteria"  # relative to the project folder (or /app in Docker)
DESCRIPTION_MATCH = 80  # WRatio needed to pair an extracted row with an expected one


@dataclass(frozen=True)
class ExpectedRow:
    description: str
    unit_cost: Decimal | None
    includes_vat: bool | None


@dataclass
class Score:
    expected: int
    extracted: int
    correct: int
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    wrong: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.expected if self.expected else 1.0


def load_expected(path: Path) -> dict[str, list[ExpectedRow]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        name: [
            ExpectedRow(
                row["description"],
                Decimal(row["unit_cost"]) if row["unit_cost"] is not None else None,
                row["includes_vat"],
            )
            for row in rows
        ]
        for name, rows in data.items()
    }


def score(expected: Sequence[ExpectedRow], rows: Sequence[ExtractedRow]) -> Score:
    """Pair each expected row with the most similar unused extracted row and check it."""
    unused = list(rows)
    result = Score(expected=len(expected), extracted=len(rows), correct=0)
    for want in expected:
        target = normalize_description(want.description)
        best, best_score = None, 0.0
        for row in unused:
            similarity = fuzz.WRatio(target, normalize_description(row.description))
            if similarity > best_score:
                best, best_score = row, similarity
        if best is None or best_score < DESCRIPTION_MATCH:
            result.missing.append(want.description)
            continue
        unused.remove(best)
        if best.unit_cost == want.unit_cost and best.includes_vat == want.includes_vat:
            result.correct += 1
        else:
            result.wrong.append(
                f"{want.description}: got cost={best.unit_cost} vat={best.includes_vat}, "
                f"expected cost={want.unit_cost} vat={want.includes_vat}"
            )
    result.extra = [row.description for row in unused]
    return result


async def run(text_model: str, vision_model: str, names: Sequence[str] | None) -> None:
    from listaya.extraction.model import ModelExtractor
    from listaya.extraction.router import ListExtractor
    from listaya.token_factory import DEFAULT_BASE_URL, TokenFactoryClient, UsageRecord, cost_usd

    api_key = os.environ.get("RADEON_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("RADEON_API_KEY is not set")
    expected = load_expected(SAMPLES / "esperado.json")
    usage: list[UsageRecord] = []
    client = TokenFactoryClient(
        api_key, base_url=os.environ.get("TOKEN_FACTORY_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        on_usage=usage.append,
    )
    extractor = ListExtractor(ModelExtractor(client, text_model=text_model, vision_model=vision_model))
    print(f"text model: {text_model} | vision model: {vision_model}\n")
    print(f"{'list':38} {'correct':>9} {'extra':>5} {'calls':>5} {'tokens':>7} {'USD':>9} {'seconds':>7}")
    totals = {"correct": 0, "expected": 0, "calls": 0, "tokens": 0, "usd": Decimal(0), "seconds": 0.0}
    details: list[str] = []
    try:
        for name in names or list(expected):
            path = SAMPLES / name
            usage.clear()
            started = time.monotonic()
            result = await extractor.extract_file(path.read_bytes(), path.name, None)
            seconds = time.monotonic() - started
            outcome = score(expected[name], result.rows)
            tokens = sum(u.prompt_tokens + u.completion_tokens for u in usage)
            usd = sum((cost_usd(u.model, u.prompt_tokens, u.completion_tokens) or Decimal(0) for u in usage), Decimal(0))
            print(
                f"{name:38} {outcome.correct:>4}/{outcome.expected:<4} {len(outcome.extra):>5} {len(usage):>5} "
                f"{tokens:>7} {usd:>9.5f} {seconds:>7.1f}"
            )
            details += [f"  {name}: {line}" for line in outcome.wrong + [f"missing {m}" for m in outcome.missing]]
            details += [f"  {name}: extra {e}" for e in outcome.extra] + [f"  {name}: problem {p}" for p in result.problems]
            for key, value in (("correct", outcome.correct), ("expected", outcome.expected), ("calls", len(usage)),
                               ("tokens", tokens), ("usd", usd), ("seconds", seconds)):
                totals[key] += value
    finally:
        await client.aclose()
    accuracy = totals["correct"] / totals["expected"] if totals["expected"] else 1.0
    print(
        f"\nTOTAL: {totals['correct']}/{totals['expected']} rows correct ({accuracy:.0%}), {totals['calls']} model calls, "
        f"{totals['tokens']} tokens, USD {totals['usd']:.5f}, {totals['seconds']:.1f} s"
    )
    if details:
        print("\nDetails:\n" + "\n".join(details))


def main() -> None:
    from dotenv import load_dotenv

    from listaya.app import DEFAULT_TEXT_MODEL, DEFAULT_VISION_MODEL

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text-model", default=DEFAULT_TEXT_MODEL)
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("files", nargs="*", help="sample file names (default: all)")
    args = parser.parse_args()
    load_dotenv()
    asyncio.run(run(args.text_model, args.vision_model, args.files))


if __name__ == "__main__":
    main()
