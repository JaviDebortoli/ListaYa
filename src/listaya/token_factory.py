"""Client for AMD Token Factory, the OpenAI-compatible API serving open models on AMD GPUs.

It enforces the shared-endpoint limits on our side: a sliding-window rate
limiter kept below the account limit, bounded concurrency, and retries only for
429 (honoring Retry-After) and 502/503 (exponential backoff with full jitter).
Every other error fails fast, since repeating it would only spend quota.
"""

from __future__ import annotations

import asyncio
import random as _random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

import httpx

DEFAULT_BASE_URL = "https://developer.amd.com.cn/radeon/api/v1"
RETRYABLE_STATUSES = frozenset({429, 502, 503})
# Failures before the request reached the model: safe to retry like a 502/503.
# Read timeouts and responses dropped midway are not: the request may already be
# processed and billed, so retrying could pay twice.
RETRYABLE_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)
# What httpx says when the server had already closed a pooled connection before our request.
_STALE_CONNECTION = "without sending a response"


def _never_reached_the_model(error: httpx.TransportError) -> bool:
    if isinstance(error, RETRYABLE_TRANSPORT_ERRORS):
        return True
    return isinstance(error, httpx.RemoteProtocolError) and _STALE_CONNECTION in str(error)

Sleep = Callable[[float], Awaitable[None]]


class TokenFactoryError(Exception):
    def __init__(self, status: int | None, message: str, attempts: int = 1, usage_status: str | None = None) -> None:
        super().__init__(f"Token Factory error {status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.attempts = attempts
        # How the failure is reported in UsageRecord.status: the HTTP status, or "network" / "malformed".
        self.usage_status = usage_status or str(status)


class RetriesExhaustedError(TokenFactoryError):
    """The service kept answering 429 or 502/503 after every allowed attempt."""


class BudgetExhaustedError(TokenFactoryError):
    """The daily spending cap is used up (reported as a 400 about insufficient credits)."""


class ModelNotAvailableError(TokenFactoryError):
    """A configured model is not in the catalog anymore."""


class EmptyResponseError(TokenFactoryError):
    """The model returned no content, typically because reasoning used up max_tokens."""


@dataclass(frozen=True)
class UsageRecord:
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_seconds: float
    attempts: int
    status: str  # "ok" or the final HTTP status


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    latency_seconds: float
    attempts: int
    rate_limit: dict[str, str] = field(default_factory=dict)


class RateLimiter:
    """Allows at most max_requests starts in any window of window_seconds."""

    def __init__(
        self,
        max_requests: int = 15,
        window_seconds: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if max_requests < 1 or window_seconds <= 0:
            raise ValueError("max_requests must be >= 1 and window_seconds > 0")
        self._max = max_requests
        self._window = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._starts and self._starts[0] <= now - self._window:
                    self._starts.popleft()
                if len(self._starts) < self._max:
                    self._starts.append(now)
                    return
                await self._sleep(self._starts[0] + self._window - now)


def _error_message(response: httpx.Response) -> str:
    """Read both error shapes: gateway {"error": {...}} and platform {"detail": {"error": {...}}}."""
    try:
        body = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    if isinstance(body, dict):
        error = body.get("error")
        detail = body.get("detail")
        if isinstance(detail, dict):
            error = detail.get("error", detail)
        elif isinstance(detail, str):
            return detail
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
    return response.text or response.reason_phrase


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


class TokenFactoryClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        limiter: RateLimiter | None = None,
        max_concurrency: int = 3,
        max_attempts: int = 5,
        backoff_base_seconds: float = 1.0,
        backoff_cap_seconds: float = 30.0,
        max_retry_after_seconds: float = 120.0,
        timeout_seconds: float = 600.0,
        on_usage: Callable[[UsageRecord], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        random: Callable[[], float] = _random.random,
    ) -> None:
        if max_concurrency < 1 or max_attempts < 1:
            raise ValueError("max_concurrency and max_attempts must be >= 1")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            # The service allows 10 minutes for a non-streaming request.
            timeout=httpx.Timeout(timeout_seconds, connect=30.0),
            transport=transport,
        )
        self._limiter = limiter or RateLimiter()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._max_retry_after = max_retry_after_seconds
        self._on_usage = on_usage
        self._sleep = sleep
        self._random = random

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        temperature: float = 0,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort

        started = time.monotonic()
        try:
            response, attempts = await self._send("POST", "chat/completions", json=payload)
        except TokenFactoryError as error:
            self._record(model, 0, 0, started, error.attempts, error.usage_status)
            raise

        try:
            body = response.json()
            choice = body["choices"][0]
            usage = body.get("usage") or {}
            details = usage.get("completion_tokens_details") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            reasoning_tokens = int(details.get("reasoning_tokens") or 0)
            finish_reason = choice.get("finish_reason")
            content = (choice.get("message") or {}).get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            # Any unexpected shape becomes one typed error, so a caller can report it and keep going.
            self._record(model, 0, 0, started, attempts, "malformed")
            raise TokenFactoryError(
                response.status_code, f"malformed response body: {response.text[:200]!r}", attempts, "malformed"
            ) from None
        if not isinstance(content, str) or not content.strip():
            self._record(model, prompt_tokens, completion_tokens, started, attempts, "empty")
            raise EmptyResponseError(
                200,
                f"empty content (finish_reason={finish_reason}); raise max_tokens or lower reasoning",
                attempts,
                "empty",
            )
        latency = self._record(model, prompt_tokens, completion_tokens, started, attempts, "ok")
        return ChatResult(
            content=content,
            model=body.get("model", model),
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            latency_seconds=latency,
            attempts=attempts,
            rate_limit={k: v for k, v in response.headers.items() if k.lower().startswith("x-ratelimit")},
        )

    async def list_models(self) -> list[str]:
        response, _ = await self._send("GET", "models")
        try:
            return [entry["id"] for entry in response.json().get("data", []) if "id" in entry]
        except (ValueError, TypeError, AttributeError):
            raise TokenFactoryError(
                response.status_code, f"malformed models list: {response.text[:200]!r}", 1, "malformed"
            ) from None

    async def validate_models(self, names: Iterable[str]) -> None:
        available = set(await self.list_models())
        missing = sorted(set(names) - available)
        if missing:
            raise ModelNotAvailableError(
                None, f"configured model(s) not available in Token Factory: {', '.join(missing)}"
            )

    async def usage(self) -> dict[str, Any]:
        response, _ = await self._send("GET", "usage", params={"include_recent": "true"})
        return response.json()

    def _record(self, model: str, prompt: int, completion: int, started: float, attempts: int, status: str) -> float:
        latency = time.monotonic() - started
        if self._on_usage is not None:
            self._on_usage(UsageRecord(model, prompt, completion, latency, attempts, status))
        return latency

    def _backoff(self, attempt: int) -> float:
        delay = min(self._backoff_cap, self._backoff_base * 2 ** (attempt - 1))
        return self._random() * delay  # full jitter

    async def _send(self, method: str, path: str, **kwargs: Any) -> tuple[httpx.Response, int]:
        for attempt in range(1, self._max_attempts + 1):
            await self._limiter.acquire()
            try:
                async with self._semaphore:
                    response = await self._http.request(method, path, **kwargs)
            except httpx.TransportError as error:
                if not _never_reached_the_model(error):
                    raise TokenFactoryError(None, f"network error: {error!r}", attempt, "network") from error
                if attempt == self._max_attempts:
                    raise RetriesExhaustedError(None, f"network error: {error!r}", attempt, "network") from error
                await self._sleep(self._backoff(attempt))
                continue

            if response.is_success:
                return response, attempt

            message = _error_message(response)
            if response.status_code not in RETRYABLE_STATUSES:
                raise self._classify(response.status_code, message, attempt)
            if attempt == self._max_attempts:
                raise RetriesExhaustedError(response.status_code, message, attempt)

            retry_after = _retry_after(response) if response.status_code == 429 else None
            if retry_after is not None and retry_after > self._max_retry_after:
                # A long wait (e.g. the daily cap) would look like a hang: fail now and say when to retry.
                raise RetriesExhaustedError(
                    429, f"{message} (service asks to retry after {retry_after:.0f} s)", attempt
                )
            if retry_after is not None:
                await self._sleep(retry_after + self._random())
            else:
                await self._sleep(self._backoff(attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def _classify(status: int, message: str, attempts: int) -> TokenFactoryError:
        lowered = message.lower()
        if status == 400 and "credit" in lowered:
            return BudgetExhaustedError(status, message, attempts)
        if status == 400 and "not supported" in lowered:
            return ModelNotAvailableError(status, message, attempts)
        return TokenFactoryError(status, message, attempts)
