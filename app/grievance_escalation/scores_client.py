"""The actual outbound REST calls to SEBI SCORES -- see this package's
`__init__.py` for why the exact endpoint paths/payload shape below are
a best-effort placeholder, not a verified transcription of a real,
published SCORES 2.0 API. Structured exactly like
`app.regulatory_filing.submission.PortalApiFilingSubmitter` (same
`httpx.AsyncClient`, same error-wrapping, same "tested against
`httpx.MockTransport` since no sandboxed SEBI portal is reachable"
posture -- see that class's own docstring, which states this
explicitly for the closest existing precedent in this codebase).
"""
from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.grievance_escalation.schemas import (
    GrievanceStatusResponse,
    GrievanceSubmissionRequest,
    GrievanceSubmissionResponse,
    ScoresGrievanceStatus,
)

logger = logging.getLogger(__name__)


class ScoresApiError(RuntimeError):
    """A SCORES API call failed. Classify transient vs. permanent via
    `app.resilience.retry_policy.is_transient` exactly like every other
    outbound-integration error in this codebase (see
    app.regulatory_filing.submission.SubmissionError's identical
    docstring) -- this class carries no separate flag of its own."""


class ScoresApiNotConfiguredError(ScoresApiError):
    """`settings.scores_api_base_url` is unset -- refuses to submit
    against a guessed URL rather than silently no-op'ing or raising a
    confusing httpx connection error against `None`."""


def _map_scores_status(raw_status: str) -> ScoresGrievanceStatus:
    """Maps SCORES' real status string onto this codebase's own
    `ScoresGrievanceStatus` -- an UNRECOGNIZED value fails closed to
    UNKNOWN (never silently assumed to be RESOLVED or any other
    specific state a caller might act on), since treating an unmapped
    status as resolved-when-it-isn't would incorrectly stop this
    grievance's status polling."""
    try:
        return ScoresGrievanceStatus(raw_status.strip().lower().replace(" ", "_"))
    except ValueError:
        logger.warning("Unrecognized SCORES status value %r; mapping to UNKNOWN rather than guessing.", raw_status)
        return ScoresGrievanceStatus.UNKNOWN


class ScoresApiClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _require_base_url(self) -> str:
        if not self._settings.scores_api_base_url:
            raise ScoresApiNotConfiguredError("settings.scores_api_base_url is not set; refusing to guess a SCORES endpoint URL.")
        return self._settings.scores_api_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.scores_api_key:
            headers["Authorization"] = f"Bearer {self._settings.scores_api_key}"
        return headers

    async def submit_grievance(self, request: GrievanceSubmissionRequest) -> GrievanceSubmissionResponse:
        base_url = self._require_base_url()
        url = f"{base_url}/v2/grievances"  # placeholder path -- see this module's docstring
        try:
            async with httpx.AsyncClient(timeout=self._settings.scores_api_timeout_seconds) as client:
                response = await client.post(url, content=request.model_dump_json(), headers=self._headers())
        except httpx.HTTPError as exc:
            raise ScoresApiError(f"SCORES grievance submission request failed: {exc}") from exc

        if response.status_code >= 300:
            raise ScoresApiError(f"SCORES rejected grievance submission (reference_id={request.reference_id}): {response.status_code} {response.text}")

        body = response.json()
        return GrievanceSubmissionResponse(
            scores_reference_number=body["scores_reference_number"],
            status=_map_scores_status(body.get("status", "submitted")),
            raw_response=response.text,
        )

    async def get_grievance_status(self, scores_reference_number: str) -> GrievanceStatusResponse:
        base_url = self._require_base_url()
        url = f"{base_url}/v2/grievances/{scores_reference_number}"  # placeholder path -- see this module's docstring
        try:
            async with httpx.AsyncClient(timeout=self._settings.scores_api_timeout_seconds) as client:
                response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ScoresApiError(f"SCORES status poll request failed for {scores_reference_number}: {exc}") from exc

        if response.status_code >= 300:
            raise ScoresApiError(f"SCORES rejected status poll for {scores_reference_number}: {response.status_code} {response.text}")

        body = response.json()
        return GrievanceStatusResponse(
            scores_reference_number=scores_reference_number,
            status=_map_scores_status(body.get("status", "")),
            last_updated_at=body["last_updated_at"],
            resolution_summary=body.get("resolution_summary"),
            raw_response=response.text,
        )
