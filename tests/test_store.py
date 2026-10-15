from pathlib import Path

import pytest

from listaya.store import RunRecord, SqliteStore
from listaya.token_factory import UsageRecord


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "data" / "listaya.db")


def test_creates_the_database_and_its_folder(tmp_path: Path):
    SqliteStore(tmp_path / "nested" / "listaya.db")
    assert (tmp_path / "nested" / "listaya.db").exists()


class TestAliases:
    def test_saved_alias_is_returned_per_supplier(self, store: SqliteStore):
        store.save("bulonera sur", "code:bs-6100", "1001")
        assert store.get("bulonera sur", "code:bs-6100") == "1001"
        assert store.get("otro", "code:bs-6100") is None

    def test_saving_again_replaces_the_alias(self, store: SqliteStore):
        store.save("bulonera sur", "desc:tornillo", "1001")
        store.save("bulonera sur", "desc:tornillo", "1002")
        assert store.get("bulonera sur", "desc:tornillo") == "1002"

    def test_aliases_survive_reopening(self, tmp_path: Path):
        path = tmp_path / "listaya.db"
        SqliteStore(path).save("s", "code:x", "1")
        assert SqliteStore(path).get("s", "code:x") == "1"


class TestSupplierVat:
    def test_unknown_supplier_has_no_vat_answer(self, store: SqliteStore):
        assert store.supplier_includes_vat("bulonera sur") is None

    @pytest.mark.parametrize("value", [True, False])
    def test_remembers_the_vat_answer(self, store: SqliteStore, value: bool):
        store.set_supplier_includes_vat("bulonera sur", value)
        assert store.supplier_includes_vat("bulonera sur") is value


class TestRunsAndUsage:
    def test_records_a_run_and_its_usage(self, store: SqliteStore):
        run_id = store.start_run(chat_id="42", supplier="bulonera sur", source="lista.csv")
        store.log_usage(run_id, UsageRecord("DeepSeek-V4-Flash", 1200, 300, 2.5, 1, "ok"))
        store.log_usage(run_id, UsageRecord("DeepSeek-V4-Flash", 800, 200, 1.5, 2, "ok"))
        store.finish_run(run_id, "applied", rows=11, matched=8, priced=7, alerts=2, unmatched=1, updated=7)

        record = store.run(run_id)
        assert record == RunRecord(
            id=run_id,
            chat_id="42",
            supplier="bulonera sur",
            source="lista.csv",
            status="applied",
            rows=11,
            matched=8,
            priced=7,
            alerts=2,
            unmatched=1,
            updated=7,
            model_calls=2,
            prompt_tokens=2000,
            completion_tokens=500,
            duration_seconds=record.duration_seconds,
        )
        assert record.duration_seconds >= 0

    def test_unknown_run_is_none(self, store: SqliteStore):
        assert store.run(999) is None
