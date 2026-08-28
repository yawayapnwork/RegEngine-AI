"""SDK-specific exception hierarchy.

All exceptions raised by ``RegEngineClient`` derive from ``RegEngineError``
so callers can catch at any granularity they need:

    except RegEngineError:           # catch everything from this SDK
    except AuthenticationError:      # token issues specifically
    except EvaluationError:          # rule evaluation failures
    except RateLimitError:           # 429s — caller should back off
    except RegEngineAPIError as e:   # any non-2xx response, with status_code
"""
from __future__ import annotations


class RegEngineError(Exception):
    """Base class for all regengine-python SDK exceptions."""


class AuthenticationError(RegEngineError):
    """Raised when the client cannot obtain or refresh an access token,
    or when the server returns 401 on an authenticated request."""


class AuthorizationError(RegEngineError):
    """Raised when the server returns 403 — the authenticated principal
    does not have the required role for the requested operation."""


class RegEngineAPIError(RegEngineError):
    """Raised for any non-2xx HTTP response that is not more specifically
    classified by one of the subclasses below.

    Attributes
    ----------
    status_code : int
        HTTP status code returned by the server.
    detail : str
        The ``detail`` field from the server's JSON error body, or the
        raw response text if the body is not valid JSON.
    request_id : str | None
        Value of the ``X-Request-ID`` response header, if present.
        Useful for correlating SDK errors with server-side logs.
    """

    def __init__(self, status_code: int, detail: str, request_id: str | None = None) -> None:
        self.status_code = status_code
        self.detail = detail
        self.request_id = request_id
        super().__init__(f"HTTP {status_code}: {detail}" + (f" (request_id={request_id})" if request_id else ""))


class NotFoundError(RegEngineAPIError):
    """Raised on HTTP 404 — the requested resource does not exist or is
    not visible to the authenticated tenant."""


class ValidationError(RegEngineAPIError):
    """Raised on HTTP 422 — the request payload failed server-side
    Pydantic validation.  ``detail`` will contain the validation errors."""


class RateLimitError(RegEngineAPIError):
    """Raised on HTTP 429.

    Attributes
    ----------
    retry_after_seconds : int | None
        Value of the ``Retry-After`` response header.  The caller should
        wait at least this many seconds before retrying.
    """

    def __init__(self, status_code: int, detail: str, retry_after_seconds: int | None = None, request_id: str | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(status_code, detail, request_id)


class EvaluationError(RegEngineError):
    """Raised when a transaction evaluation call returns an unexpected
    shape or the server reports an internal evaluation failure (5xx)."""


class WebhookVerificationError(RegEngineError):
    """Raised by ``verify_webhook_signature`` when the HMAC-SHA256 signature
    on an inbound webhook payload does not match, or is missing entirely.
    Receivers should treat this as a security event and reject the request."""


class SandboxError(RegEngineError):
    """Raised when a sandbox dry-run request fails (e.g. sandbox disabled,
    exceeded max transaction limit)."""


class TimeoutError(RegEngineError):  # noqa: A001 — intentional shadow of built-in in this namespace
    """Raised when an HTTPX request to the RegEngine AI server times out."""
