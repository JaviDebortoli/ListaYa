import asyncio
import base64
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from listaya.extraction.model import ModelExtractor
from listaya.token_factory import TokenFactoryError


class FakeChatClient:
    """Answers each chat call with the next scripted reply (a dict, a raw string, or an exception)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        reply = self.replies.pop(0) if self.replies else {"rows": []}
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return SimpleNamespace(content=content)


def row(source_row, description="Tornillo 6x1", cost="1.234,56", **extra):
    return {"source_row": source_row, "description": description, "unit_cost_text": cost, "confidence": 0.9, **extra}


def run(coro):
    return asyncio.run(coro)


class TestExtractText:
    def test_numbers_lines_and_asks_for_json(self):
        client = FakeChatClient({"rows": [row(2)]})
        extractor = ModelExtractor(client, text_model="TextModel", vision_model="VisionModel")

        result = run(extractor.extract_text("LISTA FERRETERIA\nTornillo 6x1 $1.234,56\n\n"))

        call = client.calls[0]
        assert call["model"] == "TextModel"
        assert call["response_format"] == {"type": "json_object"}
        assert call["temperature"] == 0
        assert call["messages"][0]["role"] == "system"
        assert call["messages"][1]["content"] == "L1: LISTA FERRETERIA\nL2: Tornillo 6x1 $1.234,56"
        assert [(r.source_row, r.unit_cost) for r in result.rows] == [(2, Decimal("1234.56"))]
        assert result.problems == []

    def test_long_text_is_sent_in_batches_and_merged_in_order(self):
        lines = "\n".join(f"Producto {n} ${n}00" for n in range(1, 6))
        client = FakeChatClient({"rows": [row(1), row(2)]}, {"rows": [row(3), row(4)]}, {"rows": [row(5)]})
        extractor = ModelExtractor(client, text_model="T", vision_model="V", batch_lines=2)

        result = run(extractor.extract_text(lines))

        assert len(client.calls) == 3
        assert client.calls[1]["messages"][1]["content"].startswith("L3: Producto 3")
        assert [r.source_row for r in result.rows] == [1, 2, 3, 4, 5]

    def test_invalid_json_batch_is_reported_not_dropped_silently(self):
        client = FakeChatClient({"rows": [row(1)]}, "not json at all")
        extractor = ModelExtractor(client, text_model="T", vision_model="V", batch_lines=1)

        result = run(extractor.extract_text("a $1\nb $2"))

        assert [r.source_row for r in result.rows] == [1]
        assert result.problems == ["lines 2-2: the model did not return valid JSON"]

    def test_fenced_json_is_accepted(self):
        client = FakeChatClient('```json\n{"rows": [{"source_row": 1, "description": "a", "unit_cost_text": "5"}]}\n```')
        extractor = ModelExtractor(client, text_model="T", vision_model="V")
        assert len(run(extractor.extract_text("a $5")).rows) == 1

    def test_service_error_in_a_batch_is_reported(self):
        client = FakeChatClient(TokenFactoryError(503, "busy"))
        extractor = ModelExtractor(client, text_model="T", vision_model="V")

        result = run(extractor.extract_text("a $1"))

        assert result.rows == []
        assert result.problems == ["lines 1-1: Token Factory error 503: busy"]

    def test_empty_text_makes_no_calls(self):
        client = FakeChatClient()
        result = run(ModelExtractor(client, text_model="T", vision_model="V").extract_text(" \n \n"))
        assert client.calls == []
        assert result.rows == [] and result.problems == []


class TestExtractImages:
    def test_sends_each_image_as_base64_data_url_to_the_vision_model(self):
        client = FakeChatClient({"rows": [row(1)]}, {"rows": [row(1), row(2)]})
        extractor = ModelExtractor(client, text_model="T", vision_model="VisionModel")

        result = run(extractor.extract_images([(b"page-one", "image/png"), (b"page-two", "image/jpeg")]))

        first = client.calls[0]
        assert first["model"] == "VisionModel"
        parts = first["messages"][1]["content"]
        image = next(p for p in parts if p["type"] == "image_url")
        assert image["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(b"page-one").decode()
        # Rows are numbered across pages so every row keeps a unique, ordered source_row.
        assert [r.source_row for r in result.rows] == [1, 2, 3]

    def test_failed_page_is_reported(self):
        client = FakeChatClient({"rows": []}, TokenFactoryError(None, "network error"))
        extractor = ModelExtractor(client, text_model="T", vision_model="V")

        result = run(extractor.extract_images([(b"a", "image/png"), (b"b", "image/png")]))

        assert result.problems == ["page 2: network error"]

    def test_rejects_invalid_batch_size(self):
        with pytest.raises(ValueError):
            ModelExtractor(FakeChatClient(), text_model="T", vision_model="V", batch_lines=0)


class TestReplyShape:
    @pytest.mark.parametrize("reply", ["{}", {"products": [row(1)]}, {"rows": "none"}])
    def test_reply_without_a_rows_list_is_a_problem(self, reply):
        client = FakeChatClient(reply)
        result = run(ModelExtractor(client, text_model="T", vision_model="V").extract_text("a $1"))
        assert result.rows == []
        assert result.problems == ["lines 1-1: the model did not return valid JSON"]

    def test_rows_with_a_source_row_outside_the_batch_are_marked(self):
        # Batch 2 holds lines 3-4; a reply numbering its rows 1 and 4 must not collide with batch 1.
        client = FakeChatClient({"rows": [row(1), row(2)]}, {"rows": [row(1), row(4)]})
        extractor = ModelExtractor(client, text_model="T", vision_model="V", batch_lines=2)

        result = run(extractor.extract_text("a $1\nb $2\nc $3\nd $4"))

        assert [r.source_row for r in result.rows] == [1, 2, 3, 4]
        assert "unknown_source_row" in result.rows[2].issues
        assert "unknown_source_row" not in result.rows[3].issues

    def test_several_out_of_batch_rows_still_get_unique_source_rows(self):
        client = FakeChatClient({"rows": [row(1), row(2)]}, {"rows": [row(3), row(1), row(2), row(9)]})
        extractor = ModelExtractor(client, text_model="T", vision_model="V", batch_lines=2)

        result = run(extractor.extract_text("a $1\nb $2\nc $3\nd $4"))

        numbers = [r.source_row for r in result.rows]
        assert len(numbers) == len(set(numbers))
        assert sum("unknown_source_row" in r.issues for r in result.rows) == 3


def test_prompt_tells_the_model_what_a_bare_dollar_sign_means():
    from listaya.extraction.rows import ListFormat

    client = FakeChatClient({"rows": [row(1, cost="$1,250.00")]})
    extractor = ModelExtractor(
        client, text_model="T", vision_model="V", list_format=ListFormat(decimal_separator=".", local_currency="USD")
    )
    result = run(extractor.extract_text("Hammer $1,250.00"))
    system = client.calls[0]["messages"][0]["content"]
    assert '"$" alone means USD' in system
    assert result.rows[0].unit_cost == Decimal("1250.00")


class TestVatStatement:
    @pytest.mark.parametrize(
        "header, expected",
        [("Precios + IVA. Consultar por cantidad.", False), ("Precios con IVA incluido", True), ("Lista octubre", None)],
    )
    def test_a_clear_vat_statement_in_the_text_overrides_the_model(self, header, expected):
        # The model read "+ IVA" as VAT included on a real run; code decides when the list says it.
        client = FakeChatClient({"rows": [row(2, includes_vat=True)]})
        result = run(ModelExtractor(client, text_model="T", vision_model="V").extract_text(f"{header}\nTornillo $1.234,56"))
        assert result.rows[0].includes_vat is (expected if expected is not None else True)

    def test_contradictory_statements_leave_the_model_answer(self):
        client = FakeChatClient({"rows": [row(3, includes_vat=True)]})
        text = "Precios con IVA incluido\nExcepto herramientas: + IVA\nTornillo $1.234,56"
        assert run(ModelExtractor(client, text_model="T", vision_model="V").extract_text(text)).rows[0].includes_vat is True


class TestListVatWiring:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("LISTA DE PRECIOS\n+ IVA\nTornillo $1.234,56", False),
            ("IVA incluido\nTornillo $1.234,56", True),
            ("Precios finales\nTornillo $1.234,56\nFlete + IVA", None),  # footnote: keep the model's answer
        ],
    )
    def test_extractor_applies_the_list_statement_to_every_row(self, text, expected):
        client = FakeChatClient({"rows": [row(3, includes_vat=None), row(2, includes_vat=True)]})
        result = run(ModelExtractor(client, text_model="T", vision_model="V").extract_text(text))
        model_answers = {3: None, 2: True}
        for extracted in result.rows:
            want = expected if expected is not None else model_answers[extracted.source_row]
            assert extracted.includes_vat is want


def test_models_that_think_by_default_get_their_reasoning_turned_down():
    client = FakeChatClient({"rows": []}, {"rows": []})
    extractor = ModelExtractor(client, text_model="DeepSeek-V4-Flash", vision_model="MiMo-V2.6-Flash")
    run(extractor.extract_text("a $1"))
    run(extractor.extract_images([(b"x", "image/png")]))
    assert [call["reasoning_effort"] for call in client.calls] == [None, "none"]


def test_numeric_prices_from_the_model_are_read_with_the_list_format():
    # A model may answer 4.828 (a JSON number) for "$ 4.828": in an Argentine list that is 4828, not 4.83.
    client = FakeChatClient({"rows": [{"source_row": 1, "description": "Clavo", "unit_cost_text": 4.828}]})
    result = run(ModelExtractor(client, text_model="T", vision_model="V").extract_text("Clavo $ 4.828"))
    assert result.rows[0].unit_cost == Decimal("4828")
