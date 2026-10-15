import asyncio
import json
from typing import Callable

import httpx
import pytest

from listaya.token_factory import (
    BudgetExhaustedError,
    EmptyResponseError,
    ModelNotAvailableError,
    RateLimiter,
    RetriesExhaustedError,
    TokenFactoryClient,
    TokenFactoryError,
    UsageRecord,
)

MESSAGES = [{"role": "system", "content": "Extract rows."}, {"role": "user", "content": "Tornillo 6x1 $85"}]


def ok_body(content: str = '{"rows": []}', finish_reason: str = "stop") -> dict:
    return {
        "model": "DeepSeek-V4-Flash",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "completion_tokens_details": {"reasoning_tokens": 0}},
    }


class Recorder:
    """Fake sleep and clock: sleeping advances the clock instead of waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> tuple[TokenFactoryClient, Recorder]:
    rec = Recorder()
    kwargs.setdefault("limiter", RateLimiter(max_requests=100, window_seconds=60, clock=rec.clock, sleep=rec.sleep))
    client = TokenFactoryClient(
        "rc-test",
        transport=httpx.MockTransport(handler),
        sleep=rec.sleep,
        random=lambda: 0.5,
        **kwargs,
    )
    return client, rec


def run(coro):
    return asyncio.run(coro)


class TestChat:
    def test_sends_documented_request_shape(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=ok_body())

        client, _ = make_client(handler)
        run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=2000, response_format={"type": "json_object"}))

        request = seen[0]
        assert request.url == "https://developer.amd.com.cn/radeon/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer rc-test"
        body = json.loads(request.content)
        assert body == {
            "model": "DeepSeek-V4-Flash",
            "messages": MESSAGES,
            "max_tokens": 2000,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }

    def test_returns_content_usage_and_rate_limit_headers(self):
        def handler(request: httpx.Request) -> httpx.Response:
            headers = {"X-RateLimit-Remaining-Requests": "19", "X-Other": "x"}
            return httpx.Response(200, json=ok_body(), headers=headers)

        records: list[UsageRecord] = []
        client, _ = make_client(handler, on_usage=records.append)
        result = run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=2000))

        assert result.content == '{"rows": []}'
        assert result.prompt_tokens == 120
        assert result.completion_tokens == 30
        assert result.attempts == 1
        assert result.rate_limit == {"x-ratelimit-remaining-requests": "19"}
        assert [(r.model, r.prompt_tokens, r.completion_tokens, r.status) for r in records] == [
            ("DeepSeek-V4-Flash", 120, 30, "ok")
        ]

    def test_reasoning_effort_is_sent_only_when_given(self):
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=ok_body())

        client, _ = make_client(handler)
        run(client.chat("MiMo-V2.6-Flash", MESSAGES, max_tokens=100, reasoning_effort="none"))
        assert bodies[0]["reasoning_effort"] == "none"

    def test_empty_content_cut_by_length_is_an_error(self):
        client, _ = make_client(lambda r: httpx.Response(200, json=ok_body(content="", finish_reason="length")))
        with pytest.raises(EmptyResponseError):
            run(client.chat("MiMo-V2.6-Flash", MESSAGES, max_tokens=10))


class TestRetries:
    def test_retries_429_respecting_retry_after(self):
        responses = [httpx.Response(429, headers={"Retry-After": "60"}, json={"error": {"message": "rate"}})]
        responses.append(httpx.Response(200, json=ok_body()))
        client, rec = make_client(lambda r: responses.pop(0))

        result = run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert result.attempts == 2
        assert rec.sleeps == [60.5]  # Retry-After plus jitter (random 0.5 * 1 s)

    @pytest.mark.parametrize("status", [502, 503])
    def test_retries_gateway_errors_with_exponential_backoff(self, status):
        responses = [httpx.Response(status), httpx.Response(status), httpx.Response(200, json=ok_body())]
        client, rec = make_client(lambda r: responses.pop(0))

        result = run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert result.attempts == 3
        assert rec.sleeps == [0.5, 1.0]  # full jitter (0.5) over 1 s, then 2 s

    def test_gives_up_after_max_attempts(self):
        records: list[UsageRecord] = []
        client, rec = make_client(lambda r: httpx.Response(503), max_attempts=3, on_usage=records.append)
        with pytest.raises(RetriesExhaustedError) as excinfo:
            run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert excinfo.value.status == 503
        assert len(rec.sleeps) == 2
        assert [r.status for r in records] == ["503"]

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500])
    def test_does_not_retry_other_errors(self, status):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(status, json={"error": {"message": "bad request"}})

        client, rec = make_client(handler)
        with pytest.raises(TokenFactoryError) as excinfo:
            run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert len(calls) == 1
        assert rec.sleeps == []
        assert excinfo.value.status == status
        assert "bad request" in str(excinfo.value)

    def test_reads_platform_error_shape(self):
        body = {"detail": {"error": {"message": "account_not_verified"}}}
        client, _ = make_client(lambda r: httpx.Response(403, json=body))
        with pytest.raises(TokenFactoryError, match="account_not_verified"):
            run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))

    def test_insufficient_credits_means_budget_exhausted(self):
        body = {"error": {"message": "Insufficient credits"}}
        client, _ = make_client(lambda r: httpx.Response(400, json=body))
        with pytest.raises(BudgetExhaustedError):
            run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))

    def test_unknown_model_error(self):
        body = {"error": {"message": "Requested model Foo not supported"}}
        client, _ = make_client(lambda r: httpx.Response(400, json=body))
        with pytest.raises(ModelNotAvailableError):
            run(client.chat("Foo", MESSAGES, max_tokens=10))


class TestRateLimiter:
    def test_waits_when_the_window_is_full(self):
        rec = Recorder()
        limiter = RateLimiter(max_requests=2, window_seconds=60, clock=rec.clock, sleep=rec.sleep)

        async def scenario():
            await limiter.acquire()
            rec.now = 10
            await limiter.acquire()
            await limiter.acquire()  # oldest request was at t=0, so wait until t=60

        run(scenario())
        assert rec.sleeps == [50]

    def test_rejects_invalid_configuration(self):
        with pytest.raises(ValueError):
            RateLimiter(max_requests=0, window_seconds=60)

    def test_client_uses_the_limiter_for_every_attempt(self):
        rec = Recorder()
        limiter = RateLimiter(max_requests=1, window_seconds=60, clock=rec.clock, sleep=rec.sleep)
        responses = [httpx.Response(503), httpx.Response(200, json=ok_body())]
        client = TokenFactoryClient(
            "rc-test",
            transport=httpx.MockTransport(lambda r: responses.pop(0)),
            limiter=limiter,
            sleep=rec.sleep,
            random=lambda: 0.0,
        )
        run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert rec.sleeps == [0.0, 60.0]  # backoff, then the limiter holds the retry until the window frees


class TestConcurrency:
    def test_caps_simultaneous_requests(self):
        active = 0
        peak = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, json=ok_body())

        async def scenario():
            client = TokenFactoryClient(
                "rc-test", transport=httpx.MockTransport(handler), max_concurrency=2
            )
            await asyncio.gather(*(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10) for _ in range(6)))
            await client.aclose()

        run(scenario())
        assert peak == 2


class TestModelsAndUsage:
    def test_validate_models_accepts_available_models(self):
        body = {"data": [{"id": "DeepSeek-V4-Flash"}, {"id": "DeepSeek-V4.1-Flash"}]}
        client, _ = make_client(lambda r: httpx.Response(200, json=body))
        run(client.validate_models(["DeepSeek-V4-Flash", "DeepSeek-V4.1-Flash"]))

    def test_validate_models_names_the_missing_ones(self):
        body = {"data": [{"id": "DeepSeek-V4-Flash"}]}
        client, _ = make_client(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ModelNotAvailableError, match="Gone-Model"):
            run(client.validate_models(["DeepSeek-V4-Flash", "Gone-Model"]))

    def test_usage_returns_the_raw_report(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"used": 0.1, "remaining": 0.9})

        client, _ = make_client(handler)
        assert run(client.usage()) == {"used": 0.1, "remaining": 0.9}
        assert seen[0].url == "https://developer.amd.com.cn/radeon/api/v1/usage?include_recent=true"


class TestNetworkAndMalformedResponses:
    @pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.RemoteProtocolError("dropped")])
    def test_retries_connection_failures(self, error):
        outcomes: list = [error, httpx.Response(200, json=ok_body())]

        def handler(request: httpx.Request) -> httpx.Response:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        client, rec = make_client(handler)
        result = run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert result.attempts == 2
        assert rec.sleeps == [0.5]

    def test_connection_failures_exhaust_into_typed_error_with_usage_record(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        records: list[UsageRecord] = []
        client, _ = make_client(handler, max_attempts=2, on_usage=records.append)
        with pytest.raises(RetriesExhaustedError, match="refused"):
            run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert [(r.status, r.attempts) for r in records] == [("network", 2)]

    def test_read_timeout_is_not_retried(self):
        # The request may already have been processed and billed: fail instead of paying twice.
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ReadTimeout("no answer")

        records: list[UsageRecord] = []
        client, _ = make_client(handler, on_usage=records.append)
        with pytest.raises(TokenFactoryError, match="no answer"):
            run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert len(calls) == 1
        assert [r.status for r in records] == ["network"]

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, text="<html>gateway</html>"),
            httpx.Response(200, json={"choices": []}),
            httpx.Response(200, json={"object": "chat.completion"}),
        ],
    )
    def test_malformed_success_body_is_a_typed_error_with_usage_record(self, response):
        records: list[UsageRecord] = []
        client, _ = make_client(lambda r: response, on_usage=records.append)
        with pytest.raises(TokenFactoryError, match="malformed"):
            run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert [r.status for r in records] == ["malformed"]

    def test_long_retry_after_fails_fast_instead_of_sleeping(self):
        body = {"error": {"message": "daily quota"}}
        client, rec = make_client(lambda r: httpx.Response(429, headers={"Retry-After": "3600"}, json=body))
        with pytest.raises(RetriesExhaustedError, match="3600"):
            run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert rec.sleeps == []

    def test_base_url_can_be_overridden(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=ok_body())

        client, _ = make_client(handler, base_url="https://proxy.example/v1")
        run(client.chat("DeepSeek-V4-Flash", MESSAGES, max_tokens=10))
        assert str(seen[0].url) == "https://proxy.example/v1/chat/completions"


class TestUsageAccounting:
    def test_empty_reply_is_recorded_as_empty_with_real_attempts(self):
        responses = [httpx.Response(503), httpx.Response(200, json=ok_body(content="", finish_reason="length"))]
        records: list[UsageRecord] = []
        client, _ = make_client(lambda r: responses.pop(0), on_usage=records.append)
        with pytest.raises(EmptyResponseError) as excinfo:
            run(client.chat("MiMo-V2.6-Flash", MESSAGES, max_tokens=10))
        assert excinfo.value.attempts == 2
        assert [(r.status, r.attempts, r.prompt_tokens) for r in records] == [("empty", 2, 120)]

    @pytest.mark.parametrize("response", [httpx.Response(200, text="<html>"), httpx.Response(200, json=["x"])])
    def test_malformed_models_list_is_a_typed_error(self, response):
        client, _ = make_client(lambda r: response)
        with pytest.raises(TokenFactoryError, match="malformed"):
            run(client.list_models())
