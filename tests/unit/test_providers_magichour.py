"""T2.2 — the concrete adapter, driven through `httpx.MockTransport` per `providers.md` §7.

Mirrors `test_gateway_transport.py`'s pattern: scripted upstream responses, no live socket, no
exhaustive field-by-field coverage — just enough to prove the submit/poll/error-mapping wiring
does what §7 says it does.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from tests.unit.test_persistence_redis_support import FakeRedis
from video_agent.config.settings import Settings
from video_agent.gateway.breaker import CircuitBreaker, InMemoryCircuitStateStore
from video_agent.gateway.clock import SystemClock
from video_agent.gateway.models import ArtifactRef
from video_agent.persistence.redis_client import RedisStore
from video_agent.providers.errors import (
    ProviderCredentialRejectedError,
    ProviderPaymentRequiredError,
    ProviderProjectNotFoundError,
    ProviderRenderCanceledError,
    ProviderRenderFailedError,
    ProviderRequestRejectedError,
    ProviderUnavailableError,
    ProviderUnprocessableEntityError,
)
from video_agent.providers.magichour import (
    WEBHOOK_SIGNATURE_HEADER,
    ArtifactStore,
    MagicHourClient,
    MagicHourProvider,
    RotatingApiKey,
    _build_profile,
)
from video_agent.providers.models import ShotRequest
from video_agent.providers.registry import PinnedProviderRegistry

API_KEY = "mhk-test-credential-for-tests"
"""A fake, and named so it is not mistaken for one."""


def settings(**overrides: object) -> Settings:
    fields: dict[str, object] = {
        "MAGICHOUR_API_KEY": SecretStr(API_KEY),
        "DATABASE_URL": SecretStr("postgresql+asyncpg://u:p@localhost/db"),
        "REDIS_URL": SecretStr("redis://localhost:6379/0"),
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)  # type: ignore[arg-type]


@dataclass
class FakeClock:
    """`sleep()` is a no-op so poll-loop tests run in microseconds, not `POLL_INTERVAL_S` real
    ones — mirrors `gateway.clock.Clock`'s shape without waiting for real time."""

    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@dataclass
class FakeArtifactStore:
    """Bytes in, bytes out, entirely in memory — no object-store client exists yet (T2.3/T2.4)."""

    frames: dict[str, bytes] = field(default_factory=dict)
    written: list[bytes] = field(default_factory=list)

    async def read(self, ref: ArtifactRef) -> bytes:
        return self.frames[ref.artifact_id]

    async def write(self, *, content_type: str, data: bytes) -> ArtifactRef:
        del content_type
        self.written.append(data)
        return ArtifactRef(artifact_id=f"clip-{len(self.written)}", storage_key="clips/out.mp4")


def a_request(**overrides: object) -> ShotRequest:
    fields: dict[str, object] = {
        "job_id": uuid4(),
        "shot_index": 0,
        "attempt_no": 1,
        "prompt": "a wide establishing shot",
        "duration_s": 10.0,
        "request_fingerprint": "fingerprint-1",
        "timeout_s": 30.0,
    }
    fields.update(overrides)
    return ShotRequest.model_validate(fields)


def build_provider(
    routed: RoutedTransport,
    *,
    clock: FakeClock | None = None,
    artifacts: ArtifactStore | None = None,
    webhook_cache: RedisStore | None = None,
    webhook_secret: str | None = None,
    key_provider: Callable[[], str] | None = None,
    key_rotator: RotatingApiKey | None = None,
) -> tuple[MagicHourProvider, FakeClock]:
    http_client = httpx.AsyncClient(base_url="http://magichour.invalid", transport=routed.transport)
    client = MagicHourClient(http_client, key_provider or (lambda: API_KEY))
    real_clock = clock or FakeClock()
    provider_settings = (
        settings(MAGICHOUR_WEBHOOK_SECRET=SecretStr(webhook_secret))
        if webhook_secret is not None
        else settings()
    )
    provider = MagicHourProvider(
        settings=provider_settings,
        client=client,
        artifacts=artifacts or FakeArtifactStore(),
        clock=cast(Any, real_clock),
        webhook_cache=webhook_cache,
        key_rotator=key_rotator,
    )
    return provider, real_clock


@dataclass
class RoutedTransport:
    """A mock wire keyed by request path, plus the requests it actually saw."""

    transport: httpx.MockTransport
    seen: list[httpx.Request]


def route(responses: dict[str, httpx.Response | list[httpx.Response]]) -> RoutedTransport:
    """A mock wire keyed by request path; a list is consumed in order, one call each."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        entry = responses[request.url.path]
        if isinstance(entry, list):
            return entry.pop(0)
        return entry

    return RoutedTransport(transport=httpx.MockTransport(handle), seen=seen)


EXPECTED_FPS = 24
EXPECTED_WIDTH = 1280
EXPECTED_HEIGHT = 720
EXPECTED_POLL_SLEEPS = 2

SUBMIT_RESPONSE: dict[str, Any] = {"id": "proj-1", "credits_charged": 5}
COMPLETE_RESPONSE: dict[str, Any] = {
    "status": "complete",
    "downloads": [
        {"url": "https://cdn.invalid/clip.mp4?sig=abc", "expires_at": "2099-01-01T00:00:00Z"}
    ],
    "credits_charged": 5,
    "fps": EXPECTED_FPS,
    "width": EXPECTED_WIDTH,
    "height": EXPECTED_HEIGHT,
}


@pytest.mark.asyncio
async def test_shot_zero_submits_text_to_video_and_never_uploads_a_frame() -> None:
    """`providers.md` §7.1: shot 0 has no anchor to condition on."""
    transport = route(
        {
            "/v1/text-to-video": httpx.Response(200, json=SUBMIT_RESPONSE),
            "/v1/video-projects/proj-1": httpx.Response(200, json=COMPLETE_RESPONSE),
            "/clip.mp4": httpx.Response(200, content=b"fake mp4 bytes"),
        }
    )
    provider, _clock = build_provider(transport)
    result = await provider.generate(a_request(shot_index=0), ctx=cast(Any, None))
    assert result.provider_project_id == "proj-1"
    assert result.credits_charged == Decimal(5)
    assert result.fps == EXPECTED_FPS
    assert result.width == EXPECTED_WIDTH
    assert result.height == EXPECTED_HEIGHT
    submit_request = next(r for r in transport.seen if r.url.path == "/v1/text-to-video")
    assert "image-to-video" not in str(submit_request.url)
    assert json.loads(submit_request.content)["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_a_later_shot_uploads_the_conditioning_frame_then_submits_image_to_video() -> None:
    """`providers.md` §7.2: request a slot, `PUT` the bytes, submit with the returned path."""
    upload_url_response = {
        "items": [
            {
                "upload_url": "https://upload.invalid/slot?sig=xyz",
                "file_path": "files/frame-1.png",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        ]
    }
    transport = route(
        {
            "/v1/files/upload-urls": httpx.Response(200, json=upload_url_response),
            "/slot": httpx.Response(200),
            "/v1/image-to-video": httpx.Response(200, json=SUBMIT_RESPONSE),
            "/v1/video-projects/proj-1": httpx.Response(200, json=COMPLETE_RESPONSE),
            "/clip.mp4": httpx.Response(200, content=b"fake mp4 bytes"),
        }
    )
    artifacts = FakeArtifactStore(frames={"frame-1": b"\x89PNG raw bytes"})
    provider, _clock = build_provider(transport, artifacts=artifacts)
    frame_ref = ArtifactRef(artifact_id="frame-1", storage_key="frames/frame-1.png")
    result = await provider.generate(
        a_request(shot_index=1, conditioning_frame=frame_ref), ctx=cast(Any, None)
    )
    assert result.provider_project_id == "proj-1"
    upload_request = next(r for r in transport.seen if r.url.path == "/slot")
    assert upload_request.content == b"\x89PNG raw bytes"
    submit_request = next(r for r in transport.seen if r.url.path == "/v1/image-to-video")
    assert b"files/frame-1.png" in submit_request.content
    assert json.loads(submit_request.content)["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_a_shot_with_no_conditioning_frame_past_shot_zero_is_a_programming_error() -> None:
    provider, _clock = build_provider(route({}))
    with pytest.raises(ValueError, match="conditioning frame"):
        await provider.generate(a_request(shot_index=1), ctx=cast(Any, None))


@pytest.mark.asyncio
async def test_polling_continues_until_a_terminal_status_and_then_downloads_the_clip() -> None:
    """`providers.md` §7.3: `queued`/`rendering` are non-terminal; the loop keeps polling."""
    poll_sequence = [
        httpx.Response(200, json={"status": "queued"}),
        httpx.Response(200, json={"status": "rendering"}),
        httpx.Response(200, json=COMPLETE_RESPONSE),
    ]
    transport = route(
        {
            "/v1/text-to-video": httpx.Response(200, json=SUBMIT_RESPONSE),
            "/v1/video-projects/proj-1": poll_sequence,
            "/clip.mp4": httpx.Response(200, content=b"fake mp4 bytes"),
        }
    )
    clock = FakeClock()
    artifacts = FakeArtifactStore()
    provider, _clock = build_provider(transport, clock=clock, artifacts=artifacts)
    result = await provider.generate(a_request(), ctx=cast(Any, None))
    assert result.cost_is_final is True
    assert len(clock.sleeps) == EXPECTED_POLL_SLEEPS
    assert artifacts.written == [b"fake mp4 bytes"]


@pytest.mark.asyncio
async def test_a_terminal_error_status_raises_a_repairable_render_failed_error() -> None:
    """`providers.md` §7.4: `VA-PROV-012`, eligible for repair — the request was valid."""
    error_response = {"status": "error", "error": {"code": "content_policy", "message": "nope"}}
    transport = route(
        {
            "/v1/text-to-video": httpx.Response(200, json=SUBMIT_RESPONSE),
            "/v1/video-projects/proj-1": httpx.Response(200, json=error_response),
        }
    )
    provider, _clock = build_provider(transport)
    with pytest.raises(ProviderRenderFailedError):
        await provider.generate(a_request(), ctx=cast(Any, None))


@pytest.mark.asyncio
async def test_a_terminal_canceled_status_raises_a_repairable_canceled_error() -> None:
    """`providers.md` §7.4: `VA-PROV-013`, treated as a failed attempt."""
    transport = route(
        {
            "/v1/text-to-video": httpx.Response(200, json=SUBMIT_RESPONSE),
            "/v1/video-projects/proj-1": httpx.Response(200, json={"status": "canceled"}),
        }
    )
    provider, _clock = build_provider(transport)
    with pytest.raises(ProviderRenderCanceledError):
        await provider.generate(a_request(), ctx=cast(Any, None))


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, ProviderRequestRejectedError),
        (401, ProviderCredentialRejectedError),
        (402, ProviderPaymentRequiredError),
        (404, ProviderProjectNotFoundError),
        (422, ProviderUnprocessableEntityError),
        (429, ProviderUnavailableError),
        (500, ProviderUnavailableError),
    ],
)
@pytest.mark.asyncio
async def test_each_http_status_maps_to_its_documented_exception(
    status_code: int, expected: type[Exception]
) -> None:
    """`providers.md` §7.4's status table, exercised at the one place every call passes through."""
    transport = route({"/v1/text-to-video": httpx.Response(status_code, json={"message": "nope"})})
    provider, _clock = build_provider(transport)
    with pytest.raises(expected):
        await provider.generate(a_request(), ctx=cast(Any, None))


@pytest.mark.asyncio
async def test_lookup_echoes_the_last_successful_generate_for_the_same_fingerprint() -> None:
    """A process-local echo, sufficient for an in-loop retry — see the module docstring."""
    transport = route(
        {
            "/v1/text-to-video": httpx.Response(200, json=SUBMIT_RESPONSE),
            "/v1/video-projects/proj-1": httpx.Response(200, json=COMPLETE_RESPONSE),
            "/clip.mp4": httpx.Response(200, content=b"fake mp4 bytes"),
        }
    )
    provider, _clock = build_provider(transport)
    assert await provider.lookup("fingerprint-1") is None
    req = a_request(request_fingerprint="fingerprint-1")
    result = await provider.generate(req, ctx=cast(Any, None))
    assert await provider.lookup("fingerprint-1") == result
    assert await provider.lookup("some-other-fingerprint") is None


def test_a_model_that_cannot_serve_the_fixed_beat_duration_fails_at_construction() -> None:
    """`providers.md` §7.4, `[D-34, amended]`: a bad model choice fails deploy, not every job."""
    with pytest.raises(ValueError, match="cannot render"):
        _build_profile(settings(MAGICHOUR_MODEL="sora-2"))


def test_an_unknown_model_fails_at_construction_rather_than_defaulting_silently() -> None:
    with pytest.raises(ValueError, match="no known duration constraints"):
        _build_profile(settings(MAGICHOUR_MODEL="totally-unknown-model"))


# --- webhooks: an accelerant over polling, providers.md §7.3 -------------------------------

SIGNING_VALUE = "webhook-shared-credential-for-tests"
"""A fake, and named so it is not mistaken for one."""


def _signed(body: bytes, secret: str = SIGNING_VALUE) -> dict[str, str]:
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {WEBHOOK_SIGNATURE_HEADER: signature}


@pytest.mark.asyncio
async def test_handle_webhook_rejects_a_bad_signature() -> None:
    transport = route({})
    provider, _clock = build_provider(transport, webhook_secret=SIGNING_VALUE)
    body = json.dumps({"id": "proj-1"}).encode()
    headers = {WEBHOOK_SIGNATURE_HEADER: "wrong"}
    verified = await provider.handle_webhook(raw_body=body, headers=headers)
    assert verified is False


@pytest.mark.asyncio
async def test_handle_webhook_rejects_a_missing_signature_header() -> None:
    transport = route({})
    provider, _clock = build_provider(transport, webhook_secret=SIGNING_VALUE)
    body = json.dumps({"id": "proj-1"}).encode()
    assert await provider.handle_webhook(raw_body=body, headers={}) is False


@pytest.mark.asyncio
async def test_handle_webhook_rejects_a_body_with_no_recognisable_id() -> None:
    transport = route({})
    provider, _clock = build_provider(transport, webhook_secret=SIGNING_VALUE)
    body = json.dumps({"event": "video.completed"}).encode()
    verified = await provider.handle_webhook(raw_body=body, headers=_signed(body))
    assert verified is False


@pytest.mark.asyncio
async def test_handle_webhook_never_calls_get_video_project_itself() -> None:
    """`providers.md` §7.3: a webhook only ever triggers a *later* re-read by the poll loop —
    it must never be the source of truth, and it must never fetch that truth either."""
    transport = route({})
    cache = RedisStore(cast(Any, FakeRedis()))
    provider, _clock = build_provider(transport, webhook_secret=SIGNING_VALUE, webhook_cache=cache)
    body = json.dumps({"id": "proj-1"}).encode()
    verified = await provider.handle_webhook(raw_body=body, headers=_signed(body))
    assert verified is True
    assert transport.seen == []  # no HTTP call was made


@pytest.mark.asyncio
async def test_a_verified_webhook_makes_the_poll_loop_skip_its_sleep() -> None:
    transport = route(
        {
            "/v1/text-to-video": httpx.Response(200, json=SUBMIT_RESPONSE),
            "/v1/video-projects/proj-1": httpx.Response(200, json=COMPLETE_RESPONSE),
            "/clip.mp4": httpx.Response(200, content=b"fake mp4 bytes"),
        }
    )
    cache = RedisStore(cast(Any, FakeRedis()))
    provider, clock = build_provider(transport, webhook_secret=SIGNING_VALUE, webhook_cache=cache)
    body = json.dumps({"id": "proj-1"}).encode()
    assert await provider.handle_webhook(raw_body=body, headers=_signed(body)) is True

    await provider.generate(a_request(), ctx=cast(Any, None))

    assert clock.sleeps == []  # the flag was already there, so the sleep never ran


@pytest.mark.asyncio
async def test_no_webhook_cache_configured_is_identical_to_today_pure_polling() -> None:
    """The default (`webhook_cache=None`) must change nothing about existing behaviour."""
    transport = route(
        {
            "/v1/text-to-video": httpx.Response(200, json=SUBMIT_RESPONSE),
            "/v1/video-projects/proj-1": httpx.Response(200, json=COMPLETE_RESPONSE),
            "/clip.mp4": httpx.Response(200, content=b"fake mp4 bytes"),
        }
    )
    provider, _clock = build_provider(transport)
    result = await provider.generate(a_request(), ctx=cast(Any, None))
    assert result.provider_project_id == "proj-1"


@pytest.mark.asyncio
async def test_registry_dispatches_a_verified_webhook_to_the_provider_that_recognises_it() -> None:
    transport = route({})
    provider, _clock = build_provider(transport, webhook_secret=SIGNING_VALUE)
    registry = PinnedProviderRegistry(
        providers=(provider,),
        breaker=CircuitBreaker(store=InMemoryCircuitStateStore(), clock=SystemClock()),
    )
    body = json.dumps({"id": "proj-1"}).encode()
    assert await registry.handle_webhook(raw_body=body, headers=_signed(body)) is True


@pytest.mark.asyncio
async def test_registry_returns_false_when_no_provider_recognises_the_delivery() -> None:
    transport = route({})
    provider, _clock = build_provider(transport, webhook_secret=SIGNING_VALUE)
    registry = PinnedProviderRegistry(
        providers=(provider,),
        breaker=CircuitBreaker(store=InMemoryCircuitStateStore(), clock=SystemClock()),
    )
    body = json.dumps({"id": "proj-1"}).encode()
    assert await registry.handle_webhook(raw_body=body, headers={"X-Wrong-Header": "x"}) is False


# --- key rotation -----------------------------------------------------------------------

SECOND_KEY = "mhk-test-second-credential-for-tests"
"""A fake, distinct from `API_KEY`, so a rotation is visible as a different header value."""
EXPECTED_ROTATED_SUBMIT_CALLS = 2


def test_a_fresh_rotator_starts_at_the_first_key() -> None:
    rotator = RotatingApiKey(keys=(API_KEY, SECOND_KEY))
    assert rotator() == API_KEY
    assert rotator.index == 0
    assert rotator.has_next is True


def test_advance_moves_to_the_next_key_and_stops_at_the_last() -> None:
    rotator = RotatingApiKey(keys=(API_KEY, SECOND_KEY))
    rotator.advance()
    assert rotator() == SECOND_KEY
    assert rotator.index == 1
    assert rotator.has_next is False
    rotator.advance()  # no third key — never wraps back to the first
    assert rotator() == SECOND_KEY


def test_a_rotator_needs_at_least_one_key() -> None:
    with pytest.raises(ValueError, match="at least one"):
        RotatingApiKey(keys=())


@pytest.mark.asyncio
async def test_a_402_rotates_to_the_second_key_and_the_retry_succeeds() -> None:
    transport = route(
        {
            "/v1/text-to-video": [
                httpx.Response(402, json={"message": "insufficient credits"}),
                httpx.Response(200, json=SUBMIT_RESPONSE),
            ],
            "/v1/video-projects/proj-1": httpx.Response(200, json=COMPLETE_RESPONSE),
            "/clip.mp4": httpx.Response(200, content=b"fake mp4 bytes"),
        }
    )
    rotator = RotatingApiKey(keys=(API_KEY, SECOND_KEY))
    provider, _clock = build_provider(transport, key_provider=rotator, key_rotator=rotator)

    result = await provider.generate(a_request(), ctx=cast(Any, None))

    assert result.provider_project_id == "proj-1"
    assert rotator.index == 1
    submit_calls = [r for r in transport.seen if r.url.path == "/v1/text-to-video"]
    assert len(submit_calls) == EXPECTED_ROTATED_SUBMIT_CALLS
    assert submit_calls[0].headers["authorization"] == f"Bearer {API_KEY}"
    assert submit_calls[1].headers["authorization"] == f"Bearer {SECOND_KEY}"


@pytest.mark.asyncio
async def test_without_a_rotator_a_402_is_never_retried() -> None:
    transport = route({"/v1/text-to-video": httpx.Response(402, json={"message": "nope"})})
    provider, _clock = build_provider(transport)  # no key_rotator: default None

    with pytest.raises(ProviderPaymentRequiredError):
        await provider.generate(a_request(), ctx=cast(Any, None))

    assert len([r for r in transport.seen if r.url.path == "/v1/text-to-video"]) == 1


@pytest.mark.asyncio
async def test_a_402_with_no_further_keys_still_raises() -> None:
    transport = route({"/v1/text-to-video": httpx.Response(402, json={"message": "nope"})})
    rotator = RotatingApiKey(keys=(API_KEY,))  # only one key — has_next is always False
    provider, _clock = build_provider(transport, key_provider=rotator, key_rotator=rotator)

    with pytest.raises(ProviderPaymentRequiredError):
        await provider.generate(a_request(), ctx=cast(Any, None))

    assert rotator.index == 0
    assert len([r for r in transport.seen if r.url.path == "/v1/text-to-video"]) == 1
