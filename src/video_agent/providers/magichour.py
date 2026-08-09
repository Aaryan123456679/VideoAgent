"""`MagicHourProvider`: the concrete `VideoProvider`, substituted for the PRD's Higgsfield
`[D-58]`. `providers.md` §7.

This module and `config/` are the only place in `src/` allowed to name the provider or its
models — `tests/unit/test_static_guards.py::test_the_tree_names_no_provider` enforces it, and
its `ALLOWLISTED_PATHS` is pinned to this exact file path, never a directory, so this adapter
is one flat module rather than a package.

**Wire vs. policy.** `MagicHourClient` is one HTTP attempt per method: it applies `providers.md`
§7.4's status table at the one place every upstream response passes through, so `MagicHourProvider`
never sees a provider-shaped error, only the shared taxonomy. `MagicHourProvider` owns the retry
policy (poll loop) and the `VideoProvider` Protocol surface. Field names below (`end_seconds`,
`style.prompt`, `assets.image_file_path`) are this adapter's own reading of the spec; correcting
one later is confined entirely to this module.

**Layering.** `generate()`/`lookup()`/`health()` are the whole of what this module owns: one
HTTP-backed `VideoProvider`. `ShotAttemptRepository.claim()`/`settle_cost()`, the budget
pre-flight veto against `NodeContext.budget_remaining`, and the webhook HTTP route are all one
layer up — the graph node that has a `TenantSession` and an API surface, neither of which this
adapter is handed. `[D-24]`'s crash-recovery promise ("submit response's `id` persisted
**before** polling begins") is therefore a contract on that caller, not on this module: the
caller must call `ShotAttemptRepository.record_submission` with the id `generate()` obtains from
`MagicHourClient.submit_*` before this method's poll loop is allowed to run for real. This
adapter's own `lookup()` is a process-local echo of what `generate()` last produced — sufficient
for an in-loop retry, not a substitute for the caller re-reading `GET /v1/video-projects/{id}`
by the persisted project id after a crash.

**Rates, and which ones are still placeholders.** `_MODEL_CREDITS_PER_SECOND` is this adapter's
estimate of how many credits are charged per second of rendered video, per model — a fact a
pricing page would supply and the visible spec excerpt does not. `price_per_second` is still
derived from `MAGICHOUR_USD_PER_1K_CREDITS` and never a separate hardcoded USD literal `[D-65]`.
`wan-2.2` and `ltx-2.3`'s entries are no longer placeholders — a live account confirmed the real
`credits_charged` for each at 10s/480p; models without a live measurement remain placeholders,
confined to this module, correctable once one exists.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import httpx

from video_agent.gateway.clock import Clock, SystemClock
from video_agent.gateway.models import ArtifactRef
from video_agent.observability.logging import get_logger
from video_agent.persistence.keys import provider_webhook_key
from video_agent.persistence.redis_client import RedisStore
from video_agent.providers.errors import (
    ProviderCredentialRejectedError,
    ProviderPaymentRequiredError,
    ProviderProjectNotFoundError,
    ProviderRenderCanceledError,
    ProviderRenderFailedError,
    ProviderRequestRejectedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnprocessableEntityError,
)
from video_agent.providers.models import (
    ArtifactStore,
    Capability,
    ProviderHealth,
    ProviderProfile,
    ShotResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterator, Mapping

    from video_agent.config.settings import Settings
    from video_agent.harness.context import NodeContext
    from video_agent.providers.models import ShotRequest

__all__ = [
    "ArtifactStore",
    "MagicHourClient",
    "MagicHourProvider",
    "PollResult",
    "RotatingApiKey",
    "SubmitResult",
    "UploadSlot",
    "build_magichour_provider",
]

_LOGGER = get_logger(__name__)

TEXT_TO_VIDEO_PATH: Final = "/v1/text-to-video"
IMAGE_TO_VIDEO_PATH: Final = "/v1/image-to-video"
UPLOAD_URLS_PATH: Final = "/v1/files/upload-urls"

NON_TERMINAL_STATUSES: Final = frozenset({"draft", "queued", "rendering"})
"""`providers.md` §7.3. Anything else (`complete`, `error`, `canceled`) is terminal."""

DRAFT_STATUS: Final = "draft"
"""Documented as unused by `providers.md` §7.3 — non-terminal, but alarm-worthy if it appears."""

FRAME_EXTENSION: Final = "png"
"""Anchor frames are always lossless PNG `[D-44]`."""

SHOT_DURATION_S = 10.0
"""Every beat renders at this length `[D-61]`; a model that cannot serve it fails at startup."""

POLL_INTERVAL_S = 2.0
"""How long `generate()` waits between polls once a render is in flight. `providers.md` §7.3."""

_HTTP_REQUEST_TIMEOUT_S = 60.0
"""The ceiling for one HTTP round trip — submit or poll — never for the wait-for-completion
time; see `build_magichour_provider`'s docstring for why this and `_PROVIDER_TIMEOUT_S` answer
different questions and must not be conflated."""

_MAX_PROMPT_CHARS = 1500
"""Measured live against the real API, not a guess: a 1499-char prompt was accepted, a
3425-char one rejected with a `400` (`VA-PROV-007`) — the real ceiling sits somewhere between
the two and was never precisely pinned down. `1500` is the highest value confirmed accepted, so
`compose_prompt`'s truncation (drop continuity note, then camera, then compress the beat action)
has a proven-safe target to truncate down to instead of the old, untested `4000` guess that
caused every real render to fail outright."""

WEBHOOK_SIGNATURE_HEADER = "X-Magic-Hour-Signature"
"""The header this adapter reads the delivery's HMAC-SHA256 signature from. Not confirmed
against Magic Hour's own documentation — a best-effort, industry-standard assumption (the same
scheme and header shape most webhook senders use) rather than a guess left unmarked. This is the
one line to correct if a real delivery's header name turns out to differ; nothing else in
`handle_webhook` depends on the exact name."""

_WEBHOOK_ID_FIELDS: Final = ("id", "project_id")
"""Field names tried, in order, to find the render id in a webhook body. `providers.md` §7.3:
the payload is never trusted for status or cost — only for which id to re-read — so accepting
either spelling costs nothing and a wrong guess here only skips the accelerant, never trust."""

_MODEL_DURATION_CONSTRAINTS: dict[str, tuple[float, float, frozenset[float] | None]] = {
    "wan-2.2": (3.0, 10.0, None),
    "ltx-2.3": (3.0, 30.0, None),
    "sora-2": (4.0, 60.0, frozenset({4.0, 8.0, 12.0, 24.0, 36.0, 48.0, 60.0})),
}
"""(min_duration_s, max_duration_s, allowed_durations_s) per model. `providers.md` §7.1, `[D-61]`.
A model absent here is refused at construction rather than silently defaulting.

`ltx-2.3`'s upper bound (30s) is confirmed from Magic Hour's own model comparison; its lower
bound is not independently verified against a live account and is carried over from `wan-2.2`
as the closest documented analogue (both are open-weight models served through the same
endpoint) — what *is* live-verified is that the fixed 10s beat length this adapter always
requests is accepted."""

_MODEL_CREDITS_PER_SECOND: dict[str, Decimal] = {
    "wan-2.2": Decimal("24"),
    "ltx-2.3": Decimal("24"),
    "sora-2": Decimal("100"),
}
"""`wan-2.2` and `ltx-2.3` are measured, not placeholders: a live account was charged exactly
240 credits for a 10s/480p render on each, i.e. 24 credits/second at that resolution — this
table does not vary by resolution, so treat it as accurate at 480p and an approximation
elsewhere. `sora-2` remains an unverified placeholder; see the module docstring."""

_DEFAULT_CREDITS_PER_SECOND = Decimal("50")


def _redacted_upload_url_note(file_path: str) -> str:
    """A logging-safe stand-in for an `upload_url`, which carries auth in its query string."""
    return f"upload slot for {file_path} (url redacted `[D-52]`)"


@contextmanager
def _httpx_request_logging_suppressed() -> Iterator[None]:
    """`[D-52]`: `upload_url`/`downloads[].url` carry auth in their query string. httpx's own
    request logger logs the full URL at `INFO` — silenced only around the one call that carries
    one of these two, restored immediately after."""
    httpx_logger = get_logger("httpx")
    was_disabled = httpx_logger.disabled
    httpx_logger.disabled = True
    try:
        yield
    finally:
        httpx_logger.disabled = was_disabled


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """What a submit call returned. `credits_charged` is provisional until the render settles."""

    project_id: str
    credits_charged: Decimal


@dataclass(frozen=True, slots=True)
class PollResult:
    """One read of `GET /v1/video-projects/{id}`. `providers.md` §7.3."""

    status: str
    download_url: str | None
    credits_charged: Decimal | None
    fps: int | None
    width: int | None
    height: int | None
    error_code: str | None
    error_message: str | None

    @property
    def is_terminal(self) -> bool:
        return self.status not in NON_TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class UploadSlot:
    """One upload target. `upload_url` is never logged — it carries auth in its query string."""

    upload_url: str
    file_path: str
    expires_at: datetime


@dataclass
class RotatingApiKey:
    """Cycles forward through multiple credentials, advancing only on a `402`.

    Every other failure means something the *same* account cannot recover from by retrying
    with a different one either, so `[D-62]`'s "a 402 is never retried" still holds — what
    changed is that a second, independent account can succeed where the first one's balance
    cannot. Never wraps back to an earlier key: once one is exhausted mid-job, staying on the
    next one for the rest of that render — and every later one — is the point; bouncing back
    to a key that already 402'd would just 402 again.

    Satisfies `MagicHourClient`'s `key_provider: Callable[[], str]` directly — nothing about
    the client changes to support this; only `MagicHourProvider.generate()` knows a rotator
    exists at all, and only so it can call `.advance()`.
    """

    keys: tuple[str, ...]
    _index: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.keys:
            message = "RotatingApiKey needs at least one key"
            raise ValueError(message)

    def __call__(self) -> str:
        return self.keys[self._index]

    @property
    def index(self) -> int:
        """Which key is current, for observability only — never logged as more than a number."""
        return self._index

    @property
    def has_next(self) -> bool:
        return self._index + 1 < len(self.keys)

    def advance(self) -> None:
        if self.has_next:
            self._index += 1


class MagicHourClient:
    """One HTTP attempt per method. No retry, no polling — that policy lives in this module's
    `MagicHourProvider`."""

    def __init__(self, client: httpx.AsyncClient, key_provider: Callable[[], str]) -> None:
        self._client = client
        self._key_provider = key_provider

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key_provider()}"}

    async def submit_text_to_video(
        self,
        *,
        prompt: str,
        duration_s: float,
        resolution: str,
        aspect_ratio: str,
        model: str,
        name: str,
    ) -> SubmitResult:
        """Shot 0 only, always — including on repair. Shot 0 has no anchor to condition on."""
        body = {
            "model": model,
            "end_seconds": duration_s,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "style": {"prompt": prompt},
            "name": name,
        }
        response = await self._post(TEXT_TO_VIDEO_PATH, body)
        return _submit_result(response)

    async def submit_image_to_video(
        self,
        *,
        prompt: str,
        duration_s: float,
        resolution: str,
        aspect_ratio: str,
        model: str,
        name: str,
        image_file_path: str,
    ) -> SubmitResult:
        """Shots 1-3 and repairs — start-frame conditioned generation. `providers.md` §7.1.

        No `end_image_file_path`: end-frame conditioning is "Not supported by `wan-2.2` or
        `sora-2`. Unused in v1" per the spec's field table.
        """
        body = {
            "model": model,
            "end_seconds": duration_s,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "style": {"prompt": prompt},
            "name": name,
            "assets": {"image_file_path": image_file_path},
        }
        response = await self._post(IMAGE_TO_VIDEO_PATH, body)
        return _submit_result(response)

    async def get_video_project(self, project_id: str) -> PollResult:
        """Poll for terminal status. Source of truth for status/cost `[CPS §Non-negotiables]`."""
        response = await self._get(f"/v1/video-projects/{project_id}")
        status = str(response.get("status", ""))
        if status == DRAFT_STATUS:
            _LOGGER.warning("magichour_draft_status_observed", extra={"project_id": project_id})
        downloads = response.get("downloads") or []
        first_download = downloads[0] if downloads else {}
        error = response.get("error") or {}
        credits_raw = response.get("credits_charged")
        return PollResult(
            status=status,
            download_url=first_download.get("url") if isinstance(first_download, dict) else None,
            credits_charged=Decimal(str(credits_raw)) if credits_raw is not None else None,
            fps=response.get("fps"),
            width=response.get("width"),
            height=response.get("height"),
            error_code=error.get("code") if isinstance(error, dict) else None,
            error_message=error.get("message") if isinstance(error, dict) else None,
        )

    async def create_upload_url(self, *, extension: str = FRAME_EXTENSION) -> UploadSlot:
        """`providers.md` §7.2. Re-request if `expires_at` has passed; never retry a stale PUT."""
        body = {"items": [{"type": "image", "extension": extension}]}
        response = await self._post(UPLOAD_URLS_PATH, body)
        items = response.get("items") or []
        if not items:
            message = "upload-urls response carried no items"
            raise ProviderRequestRejectedError(message)
        item = items[0]
        return UploadSlot(
            upload_url=item["upload_url"],
            file_path=item["file_path"],
            expires_at=datetime.fromisoformat(item["expires_at"]),
        )

    async def upload_bytes(self, upload_url: str, data: bytes, file_path: str) -> None:
        """`PUT` the raw frame bytes. `upload_url` carries auth in its query string `[D-52]`."""
        try:
            with _httpx_request_logging_suppressed():
                response = await self._client.put(upload_url, content=data)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(_redacted_upload_url_note(file_path)) from exc
        except httpx.HTTPError as exc:
            message = f"{_redacted_upload_url_note(file_path)}: {type(exc).__name__}"
            raise ProviderUnavailableError(message) from exc
        if response.status_code >= httpx.codes.BAD_REQUEST:
            _raise_for_status(response.status_code, _redacted_upload_url_note(file_path))

    async def download(self, download_url: str) -> bytes:
        """`GET` a completed render. Reuses the same transport as every other call — no unmanaged
        second client per shot — even though `download_url` carries its own auth `[D-52]`."""
        try:
            with _httpx_request_logging_suppressed():
                response = await self._client.get(download_url)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("download of completed render timed out") from exc
        except httpx.HTTPError as exc:
            message = f"download of completed render: {type(exc).__name__}"
            raise ProviderUnavailableError(message) from exc
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise ProviderUnavailableError(f"download returned {response.status_code}")
        return response.content

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=body, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"POST {path} timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"POST {path}: {type(exc).__name__}") from exc
        return _parsed_or_raise(response, path)

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            response = await self._client.get(path, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"GET {path} timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"GET {path}: {type(exc).__name__}") from exc
        return _parsed_or_raise(response, path)


def _submit_result(response: dict[str, Any]) -> SubmitResult:
    return SubmitResult(
        project_id=str(response["id"]),
        credits_charged=Decimal(str(response.get("credits_charged", 0))),
    )


def _parsed_or_raise(response: httpx.Response, path: str) -> dict[str, Any]:
    if response.status_code >= httpx.codes.BAD_REQUEST:
        message = _upstream_message(response, path)
        _raise_for_status(response.status_code, message)
    body = response.json()
    return body if isinstance(body, dict) else {}


def _upstream_message(response: httpx.Response, path: str) -> str:
    """The failing call's shape, never the upstream body. `providers.md` §7.4 marks that body
    untrusted content, and the raised error's text is one `AGENT.md` §3 emission path away from
    a log line — so the status and path travel, the upstream's own words do not."""
    return f"{path} returned {response.status_code}"


def _raise_for_status(status_code: int, message: str) -> None:
    """`providers.md` §7.4's HTTP-status table, applied once, at the one place every call passes
    through."""
    if status_code == httpx.codes.BAD_REQUEST:
        raise ProviderRequestRejectedError(message)
    if status_code == httpx.codes.UNAUTHORIZED:
        raise ProviderCredentialRejectedError(message)
    if status_code == httpx.codes.PAYMENT_REQUIRED:
        raise ProviderPaymentRequiredError(what_happened=message, what_was_preserved="n/a")
    if status_code == httpx.codes.NOT_FOUND:
        raise ProviderProjectNotFoundError(message)
    if status_code == httpx.codes.UNPROCESSABLE_ENTITY:
        raise ProviderUnprocessableEntityError(message)
    server_error_or_rate_limited = (
        status_code == httpx.codes.TOO_MANY_REQUESTS
        or status_code >= httpx.codes.INTERNAL_SERVER_ERROR
    )
    if server_error_or_rate_limited:
        raise ProviderUnavailableError(message)
    raise ProviderRequestRejectedError(message)


@dataclass
class MagicHourProvider:
    """The `VideoProvider` implementation. `providers.md` §7."""

    settings: Settings
    client: MagicHourClient
    artifacts: ArtifactStore
    clock: Clock = field(default_factory=SystemClock)
    webhook_cache: RedisStore | None = None
    """Where `handle_webhook` publishes a verified delivery's re-read result and where
    `_poll_until_terminal` checks for one, keyed by `persistence.keys.provider_webhook_key`.
    `None` (the default) is a real, working configuration — webhook support is an accelerant
    over polling, never a replacement for it, so every existing construction of this class
    keeps its current behaviour unchanged unless a caller opts in."""
    key_rotator: RotatingApiKey | None = None
    """The same rotator, if any, that `client`'s `key_provider` was built from — set only by
    `build_magichour_provider` when `Settings.magichour_api_keys()` returns more than one key.
    `None` (the default) means single-key behaviour is unchanged: nothing here retries a `402`
    against itself, matching every existing construction of this class before this field
    existed."""
    profile: ProviderProfile = field(init=False)
    _lookups: dict[str, ShotResult] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.profile = _build_profile(self.settings)

    async def generate(self, req: ShotRequest, *, ctx: NodeContext) -> ShotResult:
        del ctx  # no session at this layer; see the module docstring on layering.
        started = time.monotonic()
        model = self.settings.MAGICHOUR_MODEL
        name = f"{req.job_id}:{req.shot_index}:{req.attempt_no}"

        submission = await self._submit_with_rotation(req, model=model, name=name)

        poll = await self._poll_until_terminal(submission.project_id, timeout_s=req.timeout_s)
        result = self._finalize_status(submission, poll)
        if result is not None:
            raise result
        latency_ms = int((time.monotonic() - started) * 1000)
        clip = await self._store_clip(poll)
        credits_charged = (
            poll.credits_charged if poll.credits_charged is not None else submission.credits_charged
        )
        shot_result = ShotResult(
            clip=clip,
            provider_key=self.profile.provider_key,
            provider_model=model,
            provider_project_id=submission.project_id,
            seed_used=None,
            duration_s=req.duration_s,
            resolution=req.resolution,
            fps=poll.fps,
            width=poll.width,
            height=poll.height,
            cost_usd=self.settings.usd_for_credits(credits_charged),
            credits_charged=credits_charged,
            cost_is_final=True,
            latency_ms=latency_ms,
        )
        self._lookups[req.request_fingerprint] = shot_result
        return shot_result

    async def _submit_with_rotation(
        self, req: ShotRequest, *, model: str, name: str
    ) -> SubmitResult:
        """Submit once; on `402`, advance `key_rotator` (if any) and submit again.

        Safe to retry unconditionally: a `402` is a rejection, not a charge — nothing was
        created upstream, so resubmitting is not a second render of anything. Every other
        failure propagates immediately without touching the rotator; a `402` is the one
        rejection where a *different* account can plausibly answer differently, which is the
        whole reason `key_rotator` exists (`[D-62]` is still about not retrying one account).
        """
        while True:
            try:
                if req.shot_index == 0:
                    return await self.client.submit_text_to_video(
                        prompt=req.prompt,
                        duration_s=req.duration_s,
                        resolution=req.resolution,
                        aspect_ratio=req.aspect_ratio,
                        model=model,
                        name=name,
                    )
                image_file_path = await self._upload_conditioning_frame(req)
                return await self.client.submit_image_to_video(
                    prompt=req.prompt,
                    duration_s=req.duration_s,
                    resolution=req.resolution,
                    aspect_ratio=req.aspect_ratio,
                    model=model,
                    name=name,
                    image_file_path=image_file_path,
                )
            except ProviderPaymentRequiredError:
                if self.key_rotator is None or not self.key_rotator.has_next:
                    raise
                self.key_rotator.advance()
                _LOGGER.warning(
                    "magichour_key_rotated", extra={"key_index": self.key_rotator.index}
                )

    async def lookup(self, request_fingerprint: str) -> ShotResult | None:
        """A process-local echo of what `generate()` last produced for this fingerprint. Not a
        substitute for the caller's own crash-recovery read by `provider_project_id` — see the
        module docstring."""
        return self._lookups.get(request_fingerprint)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_key=self.profile.provider_key, healthy=True)

    async def _upload_conditioning_frame(self, req: ShotRequest) -> str:
        """`providers.md` §7.2: request a slot, `PUT` the bytes, hand back `file_path`."""
        if req.conditioning_frame is None:
            message = f"shot {req.shot_index} of job {req.job_id} has no conditioning frame"
            raise ValueError(message)
        data = await self.artifacts.read(req.conditioning_frame)
        slot = await self.client.create_upload_url()
        await self.client.upload_bytes(slot.upload_url, data, slot.file_path)
        return slot.file_path

    async def _poll_until_terminal(self, project_id: str, *, timeout_s: float) -> PollResult:
        """Poll until terminal, or until `timeout_s` of *elapsed budget* runs out.

        **A single poll request failing is not the render failing.** The render keeps running
        on Magic Hour's servers whether or not our `GET` happened to succeed; a transient
        network blip or a slow response on *this one status check* only means "try the status
        check again," never "abandon this attempt." Letting that kind of failure escape this
        loop is exactly what turned a flaky poll into a second real paid submission before this
        was caught: `ProviderTimeoutError`/`ProviderUnavailableError` from a poll is retryable
        at `PinnedProviderRegistry`'s level, and retrying *there* means calling `generate()`
        again — a brand new `submit_*` call, not a re-read of the render already in flight.
        """
        elapsed = 0.0
        while True:
            try:
                poll = await self.client.get_video_project(project_id)
            except (ProviderTimeoutError, ProviderUnavailableError):
                elapsed += POLL_INTERVAL_S
                if elapsed >= timeout_s:
                    message = (
                        f"render {project_id} did not reach a terminal state within "
                        f"{timeout_s}s (most recently because polling itself kept failing)"
                    )
                    raise ProviderTimeoutError(message) from None
                await self.clock.sleep(POLL_INTERVAL_S)
                continue
            if poll.is_terminal:
                return poll
            elapsed += POLL_INTERVAL_S
            if elapsed >= timeout_s:
                message = f"render {project_id} did not reach a terminal state within {timeout_s}s"
                raise ProviderTimeoutError(message)
            if not await self._notified(project_id):
                await self.clock.sleep(POLL_INTERVAL_S)

    async def _notified(self, project_id: str) -> bool:
        """Whether `handle_webhook` flagged this project since the last check. Consumes the
        flag: a webhook that names a non-terminal status (`video.started`) must not turn the
        remaining wait into a sleepless loop hammering `get_video_project` until the flag's TTL
        expires, so a read here is also a delete, and the next iteration sleeps normally again
        unless another delivery re-flags it.

        `True` skips the sleep and loops straight back to `get_video_project` — the *only*
        place this class ever reads status, cost or a download url, webhook or not. The flag
        itself carries none of that: `[D-52]` bans a download url from ever landing in a
        persisted row, and a cached `PollResult` would be exactly that the moment a render
        completes. This is purely an accelerant over the fixed poll interval, `providers.md`
        §7.3 — a cache miss (no flag, or none configured) changes nothing about correctness.
        """
        if self.webhook_cache is None:
            return False
        key = provider_webhook_key(self.profile.provider_key, project_id)
        flag = await self.webhook_cache.get(key)
        if flag is None:
            return False
        await self.webhook_cache.delete(key)
        return True

    async def handle_webhook(self, *, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        """Verify one delivery's signature, then flag the render it names for an early re-read.

        `providers.md` §7.3: the payload is never trusted for status or cost, and this method
        never calls `get_video_project` itself — it only tells `_poll_until_terminal` to stop
        waiting out its interval and make that call sooner. The flag it writes is the id alone;
        no field from the payload or from a subsequent poll is ever persisted here.
        """
        signature = headers.get(WEBHOOK_SIGNATURE_HEADER)
        if not signature:
            return False
        secret = self.settings.require_magichour_webhook_secret()
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return False
        try:
            payload = json.loads(raw_body)
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
        project_id = next(
            (payload[field] for field in _WEBHOOK_ID_FIELDS if isinstance(payload.get(field), str)),
            None,
        )
        if not project_id:
            return False
        if self.webhook_cache is not None:
            await self.webhook_cache.set(
                provider_webhook_key(self.profile.provider_key, project_id), project_id
            )
        return True

    @staticmethod
    def _finalize_status(submission: SubmitResult, poll: PollResult) -> Exception | None:
        """The error to raise for a terminal `error`/`canceled` render, or `None` for `complete`.
        `providers.md` §7.4: both are failed attempts eligible for repair; the request was valid.
        """
        if poll.status == "error":
            message = f"render {submission.project_id} failed: {poll.error_code or 'unknown'}"
            return ProviderRenderFailedError(message)
        if poll.status == "canceled":
            return ProviderRenderCanceledError(f"render {submission.project_id} was canceled")
        return None

    async def _store_clip(self, poll: PollResult) -> ArtifactRef:
        if poll.download_url is None:
            raise ProviderRenderFailedError("a complete render carried no download url")
        data = await self.client.download(poll.download_url)
        return await self.artifacts.write(content_type="video/mp4", data=data)


def build_magichour_provider(
    settings: Settings, *, artifacts: ArtifactStore
) -> tuple[MagicHourProvider, httpx.AsyncClient]:
    """The real adapter, wired from settings — plus the `httpx.AsyncClient` it owns, so the
    caller can close it when done (the same shape `api.clients.GatewayResources` uses for the
    LLM gateway's own client, for the same reason).

    Rotates through `Settings.magichour_api_keys()` automatically: one configured key behaves
    exactly as every existing construction of this class already does — `key_rotator` is only
    set at all once there is a second key to fall back to.

    **`timeout=_HTTP_REQUEST_TIMEOUT_S`, not left at httpx's default.** `MagicHourClient._post`/
    `._get` never pass a per-call override, so whatever this client is constructed with is the
    ceiling for *every single request* — submit, poll, and everything else. httpx's own default
    is 5 seconds, too short even for ordinary API jitter; a poll that times out at 5s is not a
    slow render, it is this client being unreasonably impatient. This bounds one HTTP round
    trip, never the wait for a render to finish — that is `_PROVIDER_TIMEOUT_S`'s job, enforced
    by `_poll_until_terminal`'s own elapsed-time budget across many short polls.
    """
    keys = settings.magichour_api_keys()
    rotator = RotatingApiKey(keys=keys)
    http_client = httpx.AsyncClient(
        base_url=settings.MAGICHOUR_BASE_URL, timeout=_HTTP_REQUEST_TIMEOUT_S
    )
    client = MagicHourClient(http_client, rotator)
    provider = MagicHourProvider(
        settings=settings,
        client=client,
        artifacts=artifacts,
        key_rotator=rotator if len(keys) > 1 else None,
    )
    return provider, http_client


def _build_profile(settings: Settings) -> ProviderProfile:
    model = settings.MAGICHOUR_MODEL
    constraints = _MODEL_DURATION_CONSTRAINTS.get(model)
    if constraints is None:
        message = (
            f"MAGICHOUR_MODEL={model!r} has no known duration constraints; add it to "
            f"_MODEL_DURATION_CONSTRAINTS before deploying with it."
        )
        raise ValueError(message)
    min_duration_s, max_duration_s, allowed_durations_s = constraints
    permitted = (
        min_duration_s <= SHOT_DURATION_S <= max_duration_s
        if allowed_durations_s is None
        else SHOT_DURATION_S in allowed_durations_s
    )
    if not permitted:
        message = (
            f"MAGICHOUR_MODEL={model!r} cannot render the fixed {SHOT_DURATION_S}s beat "
            f"length `[D-61]`; this fails deploy rather than every job."
        )
        raise ValueError(message)

    capabilities = {
        Capability.IMAGE_CONDITIONING,
        Capability.ASPECT_16_9,
        Capability.RES_720P,
        Capability.DURATION_10S,
        Capability.ASYNC_POLL,
        Capability.WEBHOOK_CALLBACK,
    }
    if settings.MAGICHOUR_RESOLUTION == "1080p":
        capabilities.add(Capability.RES_1080P)
    if settings.MAGICHOUR_RESOLUTION == "480p":
        # Temporary account-tier accommodation — see Capability.RES_480P's docstring.
        capabilities.add(Capability.RES_480P)

    credits_per_second = _MODEL_CREDITS_PER_SECOND.get(model, _DEFAULT_CREDITS_PER_SECOND)
    price_per_second = credits_per_second * settings.usd_per_credit

    return ProviderProfile(
        provider_key="magichour",
        capabilities=frozenset(capabilities),
        min_duration_s=min_duration_s,
        max_duration_s=max_duration_s,
        allowed_durations_s=allowed_durations_s,
        max_resolution=settings.MAGICHOUR_RESOLUTION,
        cost_unit="usd",
        price_per_second=price_per_second,
        credits_per_usd=Decimal(1) / settings.usd_per_credit,
        typical_latency_s=60.0,
        max_prompt_chars=_MAX_PROMPT_CHARS,
    )
