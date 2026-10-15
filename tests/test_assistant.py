import asyncio
import json
import shutil
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from listaya.assistant import Assistant, normalize_supplier
from listaya.catalog import CsvCatalog
from listaya.channel import Attachment, IncomingMessage
from listaya.extraction.model import ExtractionResult
from listaya.extraction.rows import ExtractedRow
from listaya.matching import Matcher
from listaya.pricing import MarginRule, PricingConfig
from listaya.store import SqliteStore
from listaya.token_factory import BudgetExhaustedError
from tests.fakes import FakeChannel

D = Decimal
CHAT = "42"
SAMPLE_CATALOG = Path(__file__).resolve().parent.parent / "samples" / "ferreteria" / "catalogo.csv"


def row(description, cost, n, code=None, pack=D("100"), vat=False):
    return ExtractedRow(code, description, pack, None, cost, None, vat, n, 1.0, str(cost))


CLEAR_ROWS = [
    row("TORNILLO AUTOPERFORANTE 6X1", D("5180"), 1, "BS-6100"),  # 6000 -> 7400, margin alert
    row("TORNILLO AUTOPERFORANTE 6X2", D("5390"), 2, "BS-6200"),  # 7000 -> 7700
]
DOUBTFUL_ROW = row("TORNILLO AUTOPERFORANTE", D("5000"), 3, "BS-T000")


class FakeExtractor:
    def __init__(self, result=None, error=None):
        self.result = result or ExtractionResult(list(CLEAR_ROWS))
        self.error = error
        self.calls = []

    async def extract_file(self, content, file_name, mime_type):
        self.calls.append(("file", content, file_name))
        if self.error:
            raise self.error
        return self.result

    async def extract_pasted_text(self, text):
        self.calls.append(("text", text))
        return self.result


class FakeChatClient:
    """Model adjudication: always suggests the first candidate."""

    async def chat(self, model, messages, **kwargs):
        items = json.loads(messages[1]["content"])["items"]
        decisions = [{"row": item["row"], "sku": item["candidates"][0]["sku"]} for item in items]
        return SimpleNamespace(content=json.dumps({"decisions": decisions}))


@pytest.fixture
def env(tmp_path):
    catalog_path = tmp_path / "catalogo.csv"
    shutil.copy(SAMPLE_CATALOG, catalog_path)
    store = SqliteStore(tmp_path / "listaya.db")
    channel = FakeChannel(files={"doc-1": b"list bytes"})
    extractor = FakeExtractor()
    assistant = Assistant(
        channel=channel,
        extractor=extractor,
        matcher_factory=lambda catalog: Matcher(catalog, store, FakeChatClient(), model="T"),
        catalog=CsvCatalog(catalog_path),
        store=store,
        rule=MarginRule(D("0.30"), D("0.20")),
        pricing=PricingConfig(),
        allowed_chats=frozenset({CHAT}),
    )
    return SimpleNamespace(
        assistant=assistant, channel=channel, extractor=extractor, store=store, catalog_path=catalog_path
    )


def send(env, **message):
    async def go():
        await env.assistant.handle(IncomingMessage(chat_id=message.pop("chat_id", CHAT), **message))
        await env.assistant.wait_idle()

    asyncio.run(go())


def file_message(caption="Bulonera Sur"):
    return {"attachment": Attachment("doc-1", "lista.csv", "text/csv", 100), "text": caption}


def texts(env):
    return [m.text for m in env.channel.sent]


def last(env):
    return env.channel.sent[-1]


def press(env, label):
    button = next(b for b in last(env).buttons if b.label == label)
    send(env, button_data=button.data)


def catalog_prices(env):
    return {p.sku: p.price for p in CsvCatalog(env.catalog_path).load()}


class TestAccess:
    def test_unknown_chat_is_told_its_id_and_nothing_is_processed(self, env):
        send(env, chat_id="999", text="hola")
        assert env.extractor.calls == []
        assert "999" in texts(env)[0] and "no está autorizado" in texts(env)[0]


class TestHappyPath:
    def test_file_with_caption_is_acknowledged_processed_summarized_and_approved(self, env):
        send(env, **file_message())

        assert texts(env)[0] == "Recibí tu lista de Bulonera Sur, la estoy procesando. Te aviso en cuanto tenga el resumen."
        assert env.extractor.calls == [("file", b"list bytes", "lista.csv")]
        summary = last(env)
        assert summary.text.startswith("Listo, procesé la lista de Bulonera Sur (2 productos).")
        assert [b.label for b in summary.buttons] == ["Aprobar", "Ver alertas", "Cancelar"]
        assert catalog_prices(env)["1001"] == D("6000")  # nothing changes before approval

        press(env, "Aprobar")

        assert last(env).text == "Listo, actualicé 2 precios en tu catálogo."
        prices = catalog_prices(env)
        assert (prices["1001"], prices["1002"]) == (D("7400"), D("7700"))
        [record] = [env.store.run(1)]
        assert (record.status, record.rows, record.updated) == ("applied", 2, 2)

    def test_ok_text_also_approves(self, env):
        send(env, **file_message())
        send(env, text="ok")
        assert last(env).text == "Listo, actualicé 2 precios en tu catálogo."

    def test_cancel_leaves_the_catalog_untouched(self, env):
        send(env, **file_message())
        before = env.catalog_path.read_bytes()
        press(env, "Cancelar")
        assert last(env).text == "Listo, descarté la lista. No cambié ningún precio."
        assert env.catalog_path.read_bytes() == before
        assert env.store.run(1).status == "cancelled"

    def test_alerts_button_shows_the_details(self, env):
        send(env, **file_message())
        press(env, "Ver alertas")
        assert "Tornillo autoperforante 6x1: hoy lo vendés a $6.000" in last(env).text

    def test_old_button_after_finishing_is_rejected(self, env):
        send(env, **file_message())
        approve = next(b for b in last(env).buttons if b.label == "Aprobar")
        press(env, "Cancelar")
        send(env, button_data=approve.data)
        assert last(env).text == "Esa lista ya no está activa. Mandame la lista de nuevo si querés procesarla."
        assert catalog_prices(env)["1001"] == D("6000")


class TestSupplier:
    def test_list_without_supplier_asks_for_it(self, env):
        send(env, **file_message(caption=None))
        assert last(env).text == "¿De qué proveedor es esta lista?"
        assert env.extractor.calls == []

        send(env, text="Bulonera Sur")
        assert env.extractor.calls == [("file", b"list bytes", "lista.csv")]
        assert last(env).text.startswith("Listo, procesé la lista de Bulonera Sur")

    def test_pasted_list_is_processed_as_text(self, env):
        send(env, text="Tornillo 6x1 $5.180\nTornillo 6x2 $5.390")
        assert last(env).text == "¿De qué proveedor es esta lista?"
        send(env, text="Bulonera Sur")
        assert env.extractor.calls == [("text", "Tornillo 6x1 $5.180\nTornillo 6x2 $5.390")]

    def test_supplier_names_are_normalized(self):
        assert normalize_supplier("  Bulonera   SUR ") == "bulonera sur"


class TestQuestions:
    def test_doubtful_match_is_asked_and_the_answer_remembered(self, env):
        env.extractor.result = ExtractionResult(CLEAR_ROWS + [DOUBTFUL_ROW])
        send(env, **file_message())

        question = last(env)
        assert question.text == '¿"TORNILLO AUTOPERFORANTE" es "Tornillo autoperforante 6x1" (1001)?'
        assert [b.label for b in question.buttons] == ["Sí", "No"]

        press(env, "Sí")
        assert last(env).text.startswith("Listo, procesé la lista de Bulonera Sur (3 productos).")
        assert env.store.get("bulonera sur", "code:bs-t000") == "1001"

    def test_rejected_match_ends_up_unmatched(self, env):
        env.extractor.result = ExtractionResult(CLEAR_ROWS + [DOUBTFUL_ROW])
        send(env, **file_message())
        press(env, "No")
        assert "❓ 1 no pude emparejar con tu catálogo." in last(env).text
        assert env.store.get("bulonera sur", "code:bs-t000") is None

    def test_unknown_vat_is_asked_once_per_supplier(self, env):
        env.extractor.result = ExtractionResult([row("TORNILLO AUTOPERFORANTE 6X1", D("6267.80"), 1, vat=None)])
        send(env, **file_message())
        assert last(env).text == "¿Los precios de Bulonera Sur incluyen IVA?"

        press(env, "Con IVA")
        assert last(env).text.startswith("Listo, procesé la lista")
        assert env.store.supplier_includes_vat("bulonera sur") is True

        send(env, **file_message())  # second list from the same supplier: no question
        assert last(env).text.startswith("Listo, procesé la lista")
        assert "incluyen IVA" not in texts(env)[-2]


class TestFailures:
    def test_budget_exhausted_is_explained(self, env):
        env.extractor.error = BudgetExhaustedError(400, "Insufficient credits")
        send(env, **file_message())
        assert last(env).text == (
            "Se agotó el cupo diario de IA para leer listas. Se renueva a las 13:00; probá de nuevo después."
        )
        assert env.store.run(1).status == "failed"

    def test_list_without_products(self, env):
        env.extractor.result = ExtractionResult([], ["unsupported file type: send a PDF, Excel/CSV, photo or text"])
        send(env, **file_message())
        assert last(env).text == (
            "No puedo leer ese tipo de archivo. Mandame un PDF, un Excel o CSV, una foto o el texto pegado."
        )

    def test_message_while_processing_is_answered(self, env):
        async def scenario():
            gate = asyncio.Event()
            original = env.extractor.extract_file

            async def slow(*args):
                await gate.wait()
                return await original(*args)

            env.extractor.extract_file = slow
            await env.assistant.handle(IncomingMessage(chat_id=CHAT, **file_message()))
            await env.assistant.handle(IncomingMessage(chat_id=CHAT, text="¿ya está?"))
            gate.set()
            await env.assistant.wait_idle()

        asyncio.run(scenario())
        assert "Todavía estoy procesando tu lista. Te aviso en cuanto termine." in texts(env)

    def test_other_text_gets_help(self, env):
        send(env, text="hola")
        assert last(env).text.startswith("Hola, soy ListaYa.")


class TestRobustness:
    def test_failure_after_matching_tells_the_merchant_and_frees_the_chat(self, env, monkeypatch):
        import listaya.assistant as module

        def broken(*args, **kwargs):
            raise RuntimeError("pricing exploded")

        monkeypatch.setattr(module, "price_items", broken)
        send(env, **file_message())
        assert last(env).text == "No pude procesar la lista por un error. Probá de nuevo en unos minutos."
        send(env, text="hola")
        assert last(env).text.startswith("Hola, soy ListaYa.")  # not "still processing"

    def test_failed_acknowledgement_does_not_lock_the_chat(self, env):
        original = env.channel.send_text
        calls = {"n": 0}

        async def flaky(chat_id, text):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("telegram timeout")
            await original(chat_id, text)

        env.channel.send_text = flaky
        send(env, **file_message())
        assert last(env).text.startswith("Listo, procesé la lista de Bulonera Sur")

    def test_new_list_replaces_the_open_one(self, env):
        send(env, **file_message())
        send(env, **file_message(caption="Distribuidora Norte"))
        assert "Descarté la lista anterior de Bulonera Sur sin cambiar precios." in texts(env)
        assert env.store.run(1).status == "replaced"
        assert last(env).text.startswith("Listo, procesé la lista de Distribuidora Norte")

    def test_run_expires_after_24_hours(self, env):
        now = {"t": 1_000_000.0}
        env.assistant._clock = lambda: now["t"]
        send(env, **file_message())
        now["t"] += 24 * 60 * 60 + 1
        press(env, "Aprobar")
        assert last(env).text == "Esa lista venció porque pasaron más de 24 horas. Mandala de nuevo para procesarla."
        assert catalog_prices(env)["1001"] == D("6000")

    def test_catalog_write_failure_keeps_the_run_and_hides_internal_details(self, env, monkeypatch):
        from listaya.catalog import CatalogError

        send(env, **file_message())

        def locked(updates):
            raise CatalogError(f"permission denied: {env.catalog_path}")

        monkeypatch.setattr(env.assistant._catalog, "apply_updates", locked)
        press(env, "Aprobar")
        assert last(env).text == (
            "No pude actualizar tu catálogo (¿está abierto en otro programa?). No cambié ningún precio. "
            "Probá aprobar de nuevo en un momento."
        )
        assert str(env.catalog_path) not in last(env).text
        monkeypatch.undo()
        send(env, text="ok")  # the run is still waiting for approval
        assert last(env).text == "Listo, actualicé 2 precios en tu catálogo."


class TestRowsWithoutPrice:
    def test_doubtful_row_without_price_is_not_asked(self, env):
        no_price = ExtractedRow("BS-T000", "TORNILLO AUTOPERFORANTE", D("100"), None, None, None, False, 3, 1.0, "consultar")
        env.extractor.result = ExtractionResult(CLEAR_ROWS + [no_price])
        send(env, **file_message())
        assert not any(m.buttons and m.buttons[0].label == "Sí" for m in env.channel.sent)
        assert "ℹ 1 sin precio para calcular" in last(env).text
        assert "no pude emparejar" not in last(env).text
