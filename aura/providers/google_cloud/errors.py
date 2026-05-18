class GoogleCloudError(Exception):
    """Base error for all Google Cloud / Vertex AI API failures."""

    def __init__(self, message: str = "", status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthError(GoogleCloudError):
    """401 — missing or invalid credentials."""


class ResourceExhaustedError(GoogleCloudError):
    """429 — quota or rate-limit exceeded."""


class BadRequestError(GoogleCloudError):
    """400 — malformed request."""


class ForbiddenError(GoogleCloudError):
    """403 — insufficient permissions."""


class NotFoundError(GoogleCloudError):
    """404 — resource not found."""


class TransientError(GoogleCloudError):
    """408 or 5xx — retriable server-side issue."""


STATUS_CODE_MAP: dict[int, type[GoogleCloudError]] = {
    400: BadRequestError,
    401: AuthError,
    403: ForbiddenError,
    404: NotFoundError,
    408: TransientError,
    429: ResourceExhaustedError,
}


def classify_error(status_code: int, message: str = "") -> GoogleCloudError:
    """Return the correct GoogleCloudError subclass for a given HTTP status.

    - 5xx codes → TransientError
    - Unknown codes → base GoogleCloudError
    """
    if 500 <= status_code < 600:
        return TransientError(message, status_code=status_code)
    cls = STATUS_CODE_MAP.get(status_code, GoogleCloudError)
    return cls(message, status_code=status_code)
