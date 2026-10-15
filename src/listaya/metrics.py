"""Measured metrics of every processed list (PRD 13 and acceptance criterion 14.5).

    python -m listaya.metrics
    python -m listaya.metrics --db data/listaya.db

Reads the SQLite store the bot writes and prints, per list: status, rows, alerts,
unmatched rows, prices updated, processing time, model calls, tokens and cost in USD
at the published Token Factory prices, followed by totals and averages.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from listaya.store import SqliteStore
from listaya.token_factory import cost_usd


@dataclass(frozen=True)
class RunMetrics:
    id: int
    supplier: str
    source: str
    status: str
    rows: int
    alerts: int
    unmatched: int
    updated: int
    duration_seconds: float
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class Summary:
    lists: int
    prices_updated: int
    total_cost_usd: Decimal
    average_cost_usd: Decimal
    average_seconds: float


def collect(store: SqliteStore) -> tuple[list[RunMetrics], Summary]:
    runs = []
    for run_id in store.run_ids():
        record = store.run(run_id)
        if record is None:
            continue
        cost = sum(
            (cost_usd(model, prompt, completion) or Decimal(0) for model, prompt, completion in store.usage_by_model(run_id)),
            Decimal(0),
        )
        runs.append(
            RunMetrics(
                id=record.id,
                supplier=record.supplier,
                source=record.source,
                status=record.status,
                rows=record.rows,
                alerts=record.alerts,
                unmatched=record.unmatched,
                updated=record.updated,
                duration_seconds=record.duration_seconds,
                model_calls=record.model_calls,
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
                cost_usd=cost,
            )
        )
    total = sum((run.cost_usd for run in runs), Decimal(0))
    count = len(runs)
    return runs, Summary(
        lists=count,
        prices_updated=sum(run.updated for run in runs),
        total_cost_usd=total,
        average_cost_usd=total / count if count else Decimal(0),
        average_seconds=sum(run.duration_seconds for run in runs) / count if count else 0.0,
    )


def render(runs: list[RunMetrics], summary: Summary) -> str:
    if not runs:
        return "No lists processed yet."
    lines = [
        f"{'#':>3} {'supplier':24} {'status':17} {'rows':>4} {'alerts':>6} {'unmatched':>9} {'updated':>7} "
        f"{'seconds':>7} {'calls':>5} {'tokens':>7} {'USD':>9}"
    ]
    for run in runs:
        lines.append(
            f"{run.id:>3} {run.supplier[:24]:24} {run.status[:17]:17} {run.rows:>4} {run.alerts:>6} {run.unmatched:>9} "
            f"{run.updated:>7} {run.duration_seconds:>7.1f} {run.model_calls:>5} "
            f"{run.prompt_tokens + run.completion_tokens:>7} {run.cost_usd:>9.5f}"
        )
    lines.append("")
    lines.append(
        f"{summary.lists} lists processed, {summary.prices_updated} prices updated, "
        f"average {summary.average_seconds:.1f} s and USD {summary.average_cost_usd:.5f} per list "
        f"(total USD {summary.total_cost_usd:.5f})."
    )
    return "\n".join(lines)


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("LISTAYA_DB_PATH", "data/listaya.db"))
    args = parser.parse_args()
    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    print(render(*collect(SqliteStore(path))))


if __name__ == "__main__":
    main()
