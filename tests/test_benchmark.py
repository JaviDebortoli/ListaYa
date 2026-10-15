import json
from decimal import Decimal
from pathlib import Path

import pytest

from listaya.benchmark import ExpectedRow, load_expected, score
from listaya.extraction.rows import ExtractedRow
from listaya.token_factory import cost_usd, reasoning_effort_for

D = Decimal
SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "ferreteria"


def extracted(description, cost, vat=True, n=1):
    return ExtractedRow(None, description, None, None, cost, "ARS", vat, n, 0.9, str(cost))


class TestScore:
    EXPECTED = [
        ExpectedRow("Martillo carpintero 27mm", D("14520"), True),
        ExpectedRow("Pinza universal 8\"", D("12705"), True),
        ExpectedRow("Pintura látex int. blanco 4lts", None, True),
    ]

    def test_all_correct(self):
        rows = [extracted("MARTILLO CARPINTERO 27 MM", D("14520")), extracted("Pinza universal 8", D("12705")),
                extracted("Pintura latex int blanco 4lts", None)]
        result = score(self.EXPECTED, rows)
        assert (result.expected, result.extracted, result.correct) == (3, 3, 3)
        assert result.accuracy == 1.0

    def test_wrong_cost_or_vat_is_not_correct(self):
        rows = [extracted("Martillo carpintero 27mm", D("1452")), extracted("Pinza universal 8\"", D("12705"), vat=False)]
        result = score(self.EXPECTED, rows)
        assert result.correct == 0
        assert result.missing == ["Pintura látex int. blanco 4lts"]

    def test_extra_rows_are_counted(self):
        rows = [extracted("Martillo carpintero 27mm", D("14520")), extracted("Envio a domicilio", D("5000"))]
        result = score(self.EXPECTED, rows)
        assert result.correct == 1
        assert result.extra == ["Envio a domicilio"]

    def test_each_extracted_row_is_used_once(self):
        expected = [ExpectedRow("Lija al agua 120", D("665.50"), True), ExpectedRow("Lija al agua 220", D("665.50"), True)]
        rows = [extracted("Lija al agua 120", D("665.50"))]
        assert score(expected, rows).correct == 1


def test_bundled_expected_results_cover_every_sample_list():
    expected = load_expected(SAMPLES / "esperado.json")
    assert set(expected) == {
        "lista_bulonera_sur.csv",
        "lista_distribuidora_norte.txt",
        "lista_pintureria_oeste.pdf",
        "foto_corralon_san_martin.jpg",
        "escaneo_corralon_san_martin.pdf",
    }
    assert all(path.exists() for path in (SAMPLES / name for name in expected))


@pytest.mark.parametrize(
    "model, prompt, completion, usd",
    [("DeepSeek-V4-Flash", 1_000_000, 1_000_000, "0.42"), ("Qwen3.8-27B", 1000, 1000, "0.0035"), ("Unknown-Model", 10, 10, None)],
)
def test_cost_usd_uses_the_published_prices(model, prompt, completion, usd):
    result = cost_usd(model, prompt, completion)
    assert result == (D(usd) if usd else None)


@pytest.mark.parametrize(
    "model, effort",
    [("DeepSeek-V4-Flash", None), ("MiMo-V2.6-Flash", "none"), ("Qwen3.8-Flash-Next", "none"), ("GLM-5.3-Flash", "low")],
)
def test_models_that_think_by_default_are_asked_not_to(model, effort):
    assert reasoning_effort_for(model) == effort


def test_unknown_file_names_are_rejected_before_any_model_call(monkeypatch):
    import asyncio

    import listaya.benchmark as benchmark

    monkeypatch.setenv("RADEON_API_KEY", "rc-test")
    monkeypatch.setattr(benchmark, "SAMPLES", SAMPLES)
    with pytest.raises(SystemExit, match="no expected result for: otra_lista.pdf"):
        asyncio.run(benchmark.run("T", "V", ["otra_lista.pdf"]))
