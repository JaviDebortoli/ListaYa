from decimal import Decimal
from pathlib import Path

from listaya.metrics import collect, render
from listaya.store import SqliteStore
from listaya.token_factory import UsageRecord


def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "listaya.db")
    first = store.start_run("42", "Bulonera Sur", "lista.csv")
    store.finish_run(first, "waiting_approval", rows=11, matched=9, priced=9, alerts=3, unmatched=2)
    store.set_run_status(first, "applied", updated=9)
    second = store.start_run("42", "Distribuidora Norte", "texto pegado")
    store.log_usage(second, UsageRecord("DeepSeek-V4-Flash", 1_000_000, 0, 30.0, 1, "ok"))
    store.log_usage(second, UsageRecord("DeepSeek-V4-Flash", 0, 1_000_000, 5.0, 1, "ok"))
    store.finish_run(second, "waiting_approval", rows=13, matched=12, priced=11, alerts=3, unmatched=1)
    store.set_run_status(second, "cancelled")
    return store


def test_collect_adds_up_usage_and_cost_per_run(tmp_path):
    runs, summary = collect(make_store(tmp_path))
    assert [r.supplier for r in runs] == ["Bulonera Sur", "Distribuidora Norte"]
    norte = runs[1]
    assert (norte.model_calls, norte.prompt_tokens, norte.completion_tokens) == (2, 1_000_000, 1_000_000)
    assert norte.cost_usd == Decimal("0.42")  # 0.14 + 0.28 per million
    assert runs[0].cost_usd == Decimal("0")  # spreadsheet read without the model
    assert summary.lists == 2
    assert summary.prices_updated == 9
    assert summary.average_cost_usd == Decimal("0.21")


def test_render_shows_one_line_per_run_and_the_totals(tmp_path):
    text = render(*collect(make_store(tmp_path)))
    assert "Bulonera Sur" in text and "Distribuidora Norte" in text
    assert "applied" in text and "cancelled" in text
    assert "2 lists processed" in text
    assert "9 prices updated" in text


def test_empty_database(tmp_path):
    text = render(*collect(SqliteStore(tmp_path / "empty.db")))
    assert text == "No lists processed yet."
