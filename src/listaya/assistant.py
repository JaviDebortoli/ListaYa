"""The conversation with the merchant (PRD section 6).

A list arrives (file, photo or pasted text), the assistant acknowledges at once and
processes it in the background: extraction, matching, the questions it needs answered
(supplier, VAT, doubtful matches), pricing and the summary. Prices change only when
the merchant approves. Merchant-facing text is in Argentine Spanish.

Conversation state lives in memory per chat; what must survive (aliases, VAT answers,
run metrics) is in the SQLite store.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from listaya.catalog import CatalogError, CatalogProduct, CsvCatalog, PriceUpdate
from listaya.channel import MAX_DOWNLOAD_BYTES, Attachment, Button, Channel, FileTooLargeError, IncomingMessage
from listaya.extraction.model import ExtractionResult
from listaya.matching import Matcher, MatchResult, MatchStatus
from listaya.pricing import MarginRule, PricingConfig
from listaya.store import SqliteStore
from listaya.summary import RunItem, RunSummary, alerts_text, price_items, summarize, summary_text
from listaya.token_factory import BudgetExhaustedError

logger = logging.getLogger(__name__)

# The run being processed, so model usage can be logged against it (see app.py).
current_run_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_run_id", default=None)

RUN_TTL_SECONDS = 24 * 60 * 60  # PRD 6.1: an unanswered run expires

HELP = (
    "Hola, soy ListaYa. Mandame la lista de precios de un proveedor (PDF, Excel o CSV, una foto o el texto "
    "pegado) y te digo cómo quedan tus precios y qué productos quedan con poco margen. "
    "Poné el nombre del proveedor en el mensaje del archivo."
)
NOT_ALLOWED = "Este chat no está autorizado para usar ListaYa. Si es tuyo, agregá el id {chat_id} a la configuración."
ACK = "Recibí tu lista de {supplier}, la estoy procesando. Te aviso en cuanto tenga el resumen."
ASK_SUPPLIER = "¿De qué proveedor es esta lista?"
ASK_VAT = "¿Los precios de {supplier} incluyen IVA?"
ASK_MATCH = '¿"{line}" es "{name}" ({sku})?'
BUSY = "Todavía estoy procesando tu lista. Te aviso en cuanto termine."
REPLACED = "Descarté la lista anterior de {supplier} sin cambiar precios."
APPLIED = "Listo, actualicé {count} {noun} en tu catálogo."
CANCELLED = "Listo, descarté la lista. No cambié ningún precio."
EXPIRED = "Esa lista venció porque pasaron más de 24 horas. Mandala de nuevo para procesarla."
STALE = "Esa lista ya no está activa. Mandame la lista de nuevo si querés procesarla."
NOTHING_TO_APPLY = "No hay precios para actualizar en esta lista."
NO_ROWS = "No encontré productos con precio en la lista. ¿Podés mandarla de nuevo o en otro formato?"
FILE_TOO_LARGE = "El archivo pesa más de 20 MB y Telegram no me deja descargarlo. ¿Podés mandarlo más liviano?"
BUDGET = "Se agotó el cupo diario de IA para leer listas. Se renueva a las 13:00; probá de nuevo después."
FAILED = "No pude procesar la lista por un error. Probá de nuevo en unos minutos."
CATALOG_FAILED = (
    "No pude actualizar tu catálogo (¿está abierto en otro programa?). No cambié ningún precio. "
    "Probá aprobar de nuevo en un momento."
)

# Known extraction problems, translated for the merchant.
_PROBLEM_MESSAGES = [
    ("unsupported file type", "No puedo leer ese tipo de archivo. Mandame un PDF, un Excel o CSV, una foto o el texto pegado."),
    ("old .xls files", "Los archivos .xls viejos no los puedo leer. Guardalo como .xlsx o .csv y mandámelo de nuevo."),
    ("could not be read", "No pude abrir el archivo. ¿Podés mandarlo de nuevo?"),
    ("could not be rendered", "No pude abrir el archivo. ¿Podés mandarlo de nuevo?"),
]

APPROVE_WORDS = {"ok", "okay", "dale", "aprobar", "aprobado", "si", "sí"}
CANCEL_WORDS = {"cancelar", "cancela", "no"}


def normalize_supplier(name: str) -> str:
    return " ".join(name.lower().split())


def looks_like_list(text: str) -> bool:
    """Pasted lists have several lines with numbers; a chat message usually does not."""
    return sum(1 for line in text.splitlines() if re.search(r"\d", line)) >= 2


def describe_problem(problems: Sequence[str]) -> str:
    for problem in problems:
        for marker, message in _PROBLEM_MESSAGES:
            if marker in problem:
                return message
    return NO_ROWS


class ExtractorPort(Protocol):
    async def extract_file(self, content: bytes, file_name: str | None, mime_type: str | None) -> ExtractionResult: ...

    async def extract_pasted_text(self, text: str) -> ExtractionResult: ...


@dataclass
class Run:
    id: int
    supplier: str
    supplier_id: str
    created_at: float
    results: list[MatchResult]
    problems: list[str]
    questions: list[int] = field(default_factory=list)  # indexes of results awaiting confirmation
    items: list[RunItem] | None = None
    summary: RunSummary | None = None


@dataclass
class Session:
    state: str = "idle"  # idle, waiting_supplier, processing, waiting_vat, waiting_match, waiting_approval
    pending: Attachment | str | None = None
    run: Run | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Assistant:
    def __init__(
        self,
        *,
        channel: Channel,
        extractor: ExtractorPort,
        matcher_factory: Callable[[list[CatalogProduct]], Matcher],
        catalog: CsvCatalog,
        store: SqliteStore,
        rule: MarginRule,
        pricing: PricingConfig,
        allowed_chats: frozenset[str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._channel = channel
        self._extractor = extractor
        self._matcher_factory = matcher_factory
        self._catalog = catalog
        self._store = store
        self._rule = rule
        self._pricing = pricing
        self._allowed = allowed_chats
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._matchers: dict[int, Matcher] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def wait_idle(self) -> None:
        """Wait for background processing to finish (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def handle(self, message: IncomingMessage) -> None:
        # "*" opens the bot to every chat (a public demo); otherwise only the listed chats are served.
        if "*" not in self._allowed and message.chat_id not in self._allowed:
            await self._channel.send_text(message.chat_id, NOT_ALLOWED.format(chat_id=message.chat_id))
            return
        session = self._sessions.setdefault(message.chat_id, Session())
        async with session.lock:
            await self._dispatch(message, session)

    async def _dispatch(self, message: IncomingMessage, session: Session) -> None:
        chat = message.chat_id
        if message.button_data:
            await self._on_button(chat, session, message.button_data)
            return
        if session.state == "processing":
            await self._channel.send_text(chat, BUSY)
            return
        if message.attachment is not None:
            await self._new_list(chat, session, message.attachment, (message.text or "").strip() or None)
            return
        text = (message.text or "").strip()
        if session.state == "waiting_supplier" and text:
            await self._start_processing(chat, session, session.pending, text)
            return
        if session.state == "waiting_approval" and text.lower() in APPROVE_WORDS:
            await self._approve(chat, session)
            return
        if session.state == "waiting_approval" and text.lower() in CANCEL_WORDS:
            await self._cancel(chat, session)
            return
        if looks_like_list(text):
            await self._new_list(chat, session, text, None)
            return
        await self._channel.send_text(chat, HELP)

    async def _new_list(self, chat: str, session: Session, source: Attachment | str, supplier: str | None) -> None:
        if isinstance(source, Attachment) and source.size is not None and source.size > MAX_DOWNLOAD_BYTES:
            await self._channel.send_text(chat, FILE_TOO_LARGE)
            return
        if session.run is not None:
            # PRD 6.1: a new list replaces the open one.
            self._store.set_run_status(session.run.id, "replaced")
            await self._channel.send_text(chat, REPLACED.format(supplier=session.run.supplier))
            self._close_run(session)
        if supplier is None:
            session.state, session.pending = "waiting_supplier", source
            await self._channel.send_text(chat, ASK_SUPPLIER)
            return
        await self._start_processing(chat, session, source, supplier)

    async def _start_processing(self, chat: str, session: Session, source: Any, supplier: str) -> None:
        session.state, session.pending = "processing", None
        try:
            await self._channel.send_text(chat, ACK.format(supplier=supplier))
        except Exception:
            # The acknowledgement is a courtesy: never let a failed send leave the chat stuck.
            logger.warning("could not acknowledge the list in chat %s", chat, exc_info=True)
        # Process in the background so the bot keeps answering while the list is read (PRD 7.4).
        task = asyncio.create_task(self._process(chat, session, source, supplier))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, chat: str, session: Session, source: Attachment | str, supplier: str) -> None:
        """Background work for one list. Any failure frees the chat and tells the merchant."""
        run_id: int | None = None
        token: contextvars.Token[int | None] | None = None
        try:
            name = (source.file_name or "archivo") if isinstance(source, Attachment) else "texto pegado"
            run_id = self._store.start_run(chat, supplier, name)
            token = current_run_id.set(run_id)
            if isinstance(source, Attachment):
                content = await self._channel.download_file(source)
                result = await self._extractor.extract_file(content, source.file_name, source.mime_type)
            else:
                result = await self._extractor.extract_pasted_text(source)
            rows = [row for row in result.rows if row.description]
            if not rows:
                self._store.finish_run(run_id, "empty", rows=0, matched=0, priced=0, alerts=0, unmatched=0)
                async with session.lock:
                    self._close_run(session)
                await self._channel.send_text(chat, describe_problem(result.problems))
                return
            matcher = self._matcher_factory(self._catalog.load())
            results = await matcher.match(normalize_supplier(supplier), rows)

            run = Run(
                id=run_id,
                supplier=supplier,
                supplier_id=normalize_supplier(supplier),
                created_at=self._clock(),
                results=results,
                problems=result.problems,
                # Rows without a price cannot be priced anyway: do not spend a question on them.
                questions=[
                    i
                    for i, r in enumerate(results)
                    if r.status == MatchStatus.NEEDS_CONFIRMATION and r.row.unit_cost is not None
                ],
            )
            self._matchers[run_id] = matcher
            async with session.lock:
                session.run = run
                await self._advance(chat, session)
        except FileTooLargeError:
            await self._fail(chat, session, run_id, FILE_TOO_LARGE)
        except BudgetExhaustedError:
            await self._fail(chat, session, run_id, BUDGET)
        except Exception:
            logger.exception("processing list for chat %s (run %s) failed", chat, run_id)
            await self._fail(chat, session, run_id, FAILED)
        finally:
            if token is not None:
                current_run_id.reset(token)

    async def _fail(self, chat: str, session: Session, run_id: int | None, message: str) -> None:
        try:
            if run_id is not None:
                self._store.finish_run(run_id, "failed", rows=0, matched=0, priced=0, alerts=0, unmatched=0)
        except Exception:
            logger.exception("could not record the failure of run %s", run_id)
        async with session.lock:
            self._close_run(session)
        try:
            await self._channel.send_text(chat, message)
        except Exception:
            logger.exception("could not tell chat %s about the failure", chat)

    async def _advance(self, chat: str, session: Session) -> None:
        """Ask the next pending question, or send the summary when none is left."""
        run = session.run
        assert run is not None
        if self._needs_vat_answer(run):
            session.state = "waiting_vat"
            await self._channel.send_buttons(
                chat,
                ASK_VAT.format(supplier=run.supplier),
                [Button("Con IVA", f"vat:{run.id}:yes"), Button("Sin IVA", f"vat:{run.id}:no")],
            )
            return
        if run.questions:
            index = run.questions[0]
            result = run.results[index]
            product = self._product(result.suggestion)
            session.state = "waiting_match"
            await self._channel.send_buttons(
                chat,
                ASK_MATCH.format(line=result.row.description, name=product.name, sku=product.sku),
                [Button("Sí", f"match:{run.id}:{index}:yes"), Button("No", f"match:{run.id}:{index}:no")],
            )
            return
        await self._send_summary(chat, session)

    def _needs_vat_answer(self, run: Run) -> bool:
        if self._store.supplier_includes_vat(run.supplier_id) is not None:
            return False
        return any(
            r.row.includes_vat is None and r.status != MatchStatus.UNMATCHED and r.row.unit_cost is not None
            for r in run.results
        )

    def _product(self, sku: str | None) -> CatalogProduct:
        matcher_products = {p.sku: p for p in self._catalog.load()}
        return matcher_products[sku]

    async def _send_summary(self, chat: str, session: Session) -> None:
        run = session.run
        assert run is not None
        products = {p.sku: p for p in self._catalog.load()}
        run.items = price_items(
            run.results,
            products,
            self._rule,
            self._pricing,
            supplier_includes_vat=self._store.supplier_includes_vat(run.supplier_id),
            default_currency=self._pricing.currency,
        )
        run.summary = summarize(run.supplier, run.items, run.problems)
        self._store.finish_run(
            run.id,
            "waiting_approval",
            rows=run.summary.total,
            matched=sum(1 for item in run.items if item.product is not None),
            priced=len(run.summary.updatable),
            alerts=len(run.summary.alerts),
            unmatched=len(run.summary.unmatched),
        )
        buttons = [Button("Ver alertas", f"alerts:{run.id}"), Button("Cancelar", f"cancel:{run.id}")]
        if run.summary.updatable:
            buttons.insert(0, Button("Aprobar", f"approve:{run.id}"))
        session.state = "waiting_approval"
        await self._channel.send_buttons(chat, summary_text(run.summary), buttons)

    async def _on_button(self, chat: str, session: Session, data: str) -> None:
        parts = data.split(":")
        action = parts[0]
        run = session.run
        if run is None or len(parts) < 2 or parts[1] != str(run.id):
            await self._channel.send_text(chat, STALE)
            return
        if action == "vat" and session.state == "waiting_vat" and len(parts) == 3:
            self._store.set_supplier_includes_vat(run.supplier_id, parts[2] == "yes")
            await self._advance(chat, session)
        elif action == "match" and session.state == "waiting_match" and len(parts) == 4:
            await self._answer_match(chat, session, int(parts[2]), parts[3] == "yes")
        elif action == "approve" and session.state == "waiting_approval":
            await self._approve(chat, session)
        elif action == "cancel":
            await self._cancel(chat, session)
        elif action == "alerts" and run.summary is not None:
            await self._channel.send_text(chat, alerts_text(run.summary))
        else:
            await self._channel.send_text(chat, STALE)

    async def _answer_match(self, chat: str, session: Session, index: int, accepted: bool) -> None:
        run = session.run
        assert run is not None
        if not run.questions or run.questions[0] != index:
            await self._channel.send_text(chat, STALE)
            return
        run.questions.pop(0)
        result = run.results[index]
        if accepted and result.suggestion is not None:
            self._matchers[run.id].confirm(run.supplier_id, result.row, result.suggestion)
            run.results[index] = dataclasses.replace(
                result, status=MatchStatus.MATCHED, sku=result.suggestion, method="merchant"
            )
        else:
            run.results[index] = dataclasses.replace(result, status=MatchStatus.UNMATCHED, suggestion=None)
        await self._advance(chat, session)

    async def _approve(self, chat: str, session: Session) -> None:
        run = session.run
        assert run is not None and run.summary is not None
        if self._clock() - run.created_at > RUN_TTL_SECONDS:
            self._store.set_run_status(run.id, "expired")
            self._close_run(session)
            await self._channel.send_text(chat, EXPIRED)
            return
        updates = [
            PriceUpdate(sku=item.product.sku, cost=item.quote.new_cost, price=item.quote.suggested_price)
            for item in run.summary.updatable
        ]
        if not updates:
            await self._channel.send_text(chat, NOTHING_TO_APPLY)
            return
        try:
            self._catalog.apply_updates(updates)
        except (CatalogError, OSError) as error:
            logger.warning("catalog update for run %s failed: %s", run.id, error)
            # Details stay in the log: the error can include local paths.
            await self._channel.send_text(chat, CATALOG_FAILED)
            return
        self._store.set_run_status(run.id, "applied", updated=len(updates))
        self._close_run(session)
        noun = "precio" if len(updates) == 1 else "precios"
        await self._channel.send_text(chat, APPLIED.format(count=len(updates), noun=noun))

    async def _cancel(self, chat: str, session: Session) -> None:
        run = session.run
        assert run is not None
        self._store.set_run_status(run.id, "cancelled")
        self._close_run(session)
        await self._channel.send_text(chat, CANCELLED)

    def _close_run(self, session: Session) -> None:
        if session.run is not None:
            self._matchers.pop(session.run.id, None)
        session.run, session.state, session.pending = None, "idle", None
