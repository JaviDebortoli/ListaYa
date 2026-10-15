import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from listaya.catalog import CatalogProduct
from listaya.extraction.rows import ExtractedRow
from listaya.matching import (
    InMemoryAliasStore,
    Matcher,
    MatchStatus,
    attributes_compatible,
    measure_numbers,
    normalize_description,
)
from listaya.token_factory import TokenFactoryError

D = Decimal
SUPPLIER = "proveedor-x"


def product(sku, name, pack_size=None):
    return CatalogProduct(sku, name, "unidad", pack_size, D("100"), D("150"), None)


CATALOG = [
    product("2231", "Tornillo autoperforante 6x1"),
    product("2232", "Tornillo autoperforante 6x2"),
    product("3001", "Gaseosa Coca Cola 2.25 L x6"),
    product("4001", "Tarugo nylon 8 mm"),
    product("5001", "Martillo galponero"),
]


def row(description, code=None, n=1, pack_size=None):
    return ExtractedRow(code, description, pack_size, None, D("10"), "ARS", False, n, 0.9, "10")


class FakeChatClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=json.dumps(reply))


def matcher(client=None, aliases=None):
    return Matcher(CATALOG, aliases or InMemoryAliasStore(), client or FakeChatClient(), model="TextModel")


def run(coro):
    return asyncio.run(coro)


class TestNormalization:
    def test_normalize_description(self):
        assert normalize_description("  GASEOSA Coca-Cola 2,25L  ") == "gaseosa coca cola 2.25l"

    @pytest.mark.parametrize(
        "text, numbers",
        [
            ("Tornillo 6x1", {"6", "1"}),
            ("Coca Cola 2.25 x6", {"2.25"}),
            ("GASEOSA COCA 2,25L PACK", {"2.25"}),
            ("Tarugo 8 mm x 100", {"8"}),
            ("Martillo", set()),
            ("Cable 2.50", {"2.5"}),
        ],
    )
    def test_measure_numbers_ignore_pack_counts(self, text, numbers):
        assert measure_numbers(text) == {D(n) for n in numbers}

    def test_attributes_compatible(self):
        assert attributes_compatible(row("Tornillo 6x1"), CATALOG[0])
        assert not attributes_compatible(row("Tornillo 6x1"), CATALOG[1])
        assert attributes_compatible(row("Martillo grande"), CATALOG[4])
        assert not attributes_compatible(row("Martillo", pack_size=D("6")), product("9", "Martillo", D("1")))


class TestStages:
    def test_supplier_code_alias_matches_first(self):
        aliases = InMemoryAliasStore()
        aliases.save(SUPPLIER, "code:tor-61", "2231")
        [result] = run(matcher(aliases=aliases).match(SUPPLIER, [row("Algo distinto", code="TOR-61")]))
        assert (result.status, result.sku, result.method) == (MatchStatus.MATCHED, "2231", "code")

    def test_confirmed_description_alias_matches(self):
        aliases = InMemoryAliasStore()
        aliases.save(SUPPLIER, "desc:tornillo 6x1 ac", "2231")
        [result] = run(matcher(aliases=aliases).match(SUPPLIER, [row("TORNILLO 6x1 AC")]))
        assert (result.status, result.sku, result.method) == (MatchStatus.MATCHED, "2231", "alias")

    def test_aliases_are_isolated_by_supplier(self):
        aliases = InMemoryAliasStore()
        aliases.save("otro-proveedor", "code:tor-61", "2231")
        client = FakeChatClient({"decisions": []})
        [result] = run(matcher(client, aliases).match(SUPPLIER, [row("Producto raro", code="TOR-61")]))
        assert result.method != "code"

    def test_close_and_compatible_description_is_matched_automatically(self):
        client = FakeChatClient()
        [result] = run(matcher(client).match(SUPPLIER, [row("TORNILLO AUTOPERFORANTE 6X1")]))
        assert (result.status, result.sku, result.method) == (MatchStatus.MATCHED, "2231", "fuzzy")
        assert result.score >= 0.90
        assert client.calls == []

    def test_unrelated_description_is_unmatched_without_asking_the_model(self):
        client = FakeChatClient()
        [result] = run(matcher(client).match(SUPPLIER, [row("Pintura latex blanca 20 lt")]))
        assert (result.status, result.sku) == (MatchStatus.UNMATCHED, None)
        assert client.calls == []

    def test_doubtful_rows_go_to_the_model_in_one_batch_and_become_questions(self):
        client = FakeChatClient({"decisions": [{"row": 1, "sku": "3001"}, {"row": 2, "sku": None}]})
        rows = [row("GASEOSA COCA 2,25L PACK", n=1), row("Tornillo autoperforante 6x3", n=2)]

        first, second = run(matcher(client).match(SUPPLIER, rows))

        assert len(client.calls) == 1
        assert client.calls[0]["response_format"] == {"type": "json_object"}
        prompt = client.calls[0]["messages"][1]["content"]
        assert "GASEOSA COCA 2,25L PACK" in prompt and "3001" in prompt
        assert (first.status, first.suggestion, first.method) == (MatchStatus.NEEDS_CONFIRMATION, "3001", "model")
        assert first.sku is None  # nothing is matched until the merchant confirms
        assert (second.status, second.suggestion) == (MatchStatus.UNMATCHED, None)

    def test_model_choice_outside_the_candidates_is_ignored(self):
        client = FakeChatClient({"decisions": [{"row": 1, "sku": "9999"}]})
        [result] = run(matcher(client).match(SUPPLIER, [row("GASEOSA COCA 2,25L PACK")]))
        assert result.status == MatchStatus.NEEDS_CONFIRMATION
        assert result.suggestion == result.candidates[0][0]

    def test_model_failure_falls_back_to_asking_with_the_best_candidate(self):
        client = FakeChatClient(TokenFactoryError(503, "busy"))
        [result] = run(matcher(client).match(SUPPLIER, [row("GASEOSA COCA 2,25L PACK")]))
        assert result.status == MatchStatus.NEEDS_CONFIRMATION
        assert result.suggestion == "3001"
        assert result.method == "fuzzy"

    def test_close_but_incompatible_measure_is_not_matched_automatically(self):
        client = FakeChatClient({"decisions": [{"row": 1, "sku": None}]})
        [result] = run(matcher(client).match(SUPPLIER, [row("Tornillo autoperforante 6x3")]))
        assert result.status != MatchStatus.MATCHED
        assert len(client.calls) == 1

    def test_row_without_description_or_code_is_unmatched(self):
        [result] = run(matcher().match(SUPPLIER, [row("")]))
        assert result.status == MatchStatus.UNMATCHED

    def test_empty_catalog_leaves_everything_unmatched(self):
        m = Matcher([], InMemoryAliasStore(), FakeChatClient(), model="T")
        [result] = run(m.match(SUPPLIER, [row("Tornillo 6x1")]))
        assert result.status == MatchStatus.UNMATCHED


class TestConfirmation:
    def test_confirmed_answer_is_remembered_by_code_and_description(self):
        aliases = InMemoryAliasStore()
        m = matcher(aliases=aliases)
        doubtful = row("GASEOSA COCA 2,25L PACK", code="GAS-225")

        m.confirm(SUPPLIER, doubtful, "3001")

        assert aliases.get(SUPPLIER, "code:gas-225") == "3001"
        assert aliases.get(SUPPLIER, "desc:gaseosa coca 2.25l pack") == "3001"
        [again] = run(m.match(SUPPLIER, [row("Gaseosa  Coca 2,25L pack")]))
        assert (again.status, again.sku, again.method) == (MatchStatus.MATCHED, "3001", "alias")

    def test_confirming_an_unknown_sku_is_rejected(self):
        with pytest.raises(ValueError):
            matcher().confirm(SUPPLIER, row("x"), "9999")


def test_rejects_inverted_thresholds():
    with pytest.raises(ValueError):
        Matcher(CATALOG, InMemoryAliasStore(), FakeChatClient(), model="T", auto_accept=0.5, ask_from=0.6)
