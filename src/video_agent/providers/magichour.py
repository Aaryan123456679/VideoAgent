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

**Placeholder rates.** `_MODEL_CREDITS_PER_SECOND` is this adapter's estimate of how many
credits are charged per second of rendered video, per model — a fact a pricing page would
supply and the visible spec excerpt does not. `price_per_second` is still derived from
`MAGICHOUR_USD_PER_1K_CREDITS` and never a separate hardcoded USD literal `[D-65]`; only the
credits-per-second multiplier is a placeholder, confined to this module, correctable once a
live account is available.
"""

from __future__ import annotations

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
    from collections.abc import Callable, Iterator

    from video_agent.config.settings import Settings
    from video_agent.harness.context import NodeContext
    from video_agent.providers.models import ShotRequest

__all__ = [
    "ArtifactStore",
    "MagicHourClient",
    "MagicHourProvider",
    "PollResult",
    "SubmitResult",
    "UploadSlot",
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

_MODEL_DURATION_CONSTRAINTS: dict[str, tuple[float, float, frozenset[float] | None]] = {
    "wan-2.2": (3.0, 10.0, None),
    "sora-2": (4.0, 60.0, frozenset({4.0, 8.0, 12.0, 24.0, 36.0, 48.0, 60.0})),
}
"""(min_duration_s, max_duration_s, allowed_durations_s) per model. `providers.md` §7.1, `[D-61]`.
A model absent here is refused at construction rather than silently defaulting."""

_MODEL_CREDITS_PER_SECOND: dict[str, Decimal] = {
    "wan-2.2": Decimal("50"),
    "sora-2": Decimal("100"),
}
"""Placeholder estimates — see the module docstring. Never used for billing, only for
`price_per_second` (negotiation ranking) and the caller's pre-flight budget estimate."""

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
    profile: ProviderProfile = field(init=False)
    _lookups: dict[str, ShotResult] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.profile = _build_profile(self.settings)

    async def generate(self, req: ShotRequest, *, ctx: NodeContext) -> ShotResult:
        del ctx  # no session at this layer; see the module docstring on layering.
        started = time.monotonic()
        model = self.settings.MAGICHOUR_MODEL
        name = f"{req.job_id}:{req.shot_index}:{req.attempt_no}"

        if req.shot_index == 0:
            submission = await self.client.submit_text_to_video(
                prompt=req.prompt,
                duration_s=req.duration_s,
                resolution=req.resolution,
                aspect_ratio=req.aspect_ratio,
                model=model,
                name=name,
            )
        else:
            image_file_path = await self._upload_conditioning_frame(req)
            submission = await self.client.submit_image_to_video(
                prompt=req.prompt,
                duration_s=req.duration_s,
                resolution=req.resolution,
                aspect_ratio=req.aspect_ratio,
                model=model,
                name=name,
                image_file_path=image_file_path,
            )

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
        elapsed = 0.0
        while True:
            poll = await self.client.get_video_project(project_id)
            if poll.is_terminal:
                return poll
            elapsed += POLL_INTERVAL_S
            if elapsed >= timeout_s:
                message = f"render {project_id} did not reach a terminal state within {timeout_s}s"
                raise ProviderTimeoutError(message)
            await self.clock.sleep(POLL_INTERVAL_S)

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
        max_prompt_chars=4000,
    )
