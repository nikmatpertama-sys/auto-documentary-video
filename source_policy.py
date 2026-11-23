"""HTTP failure policy for optional footage providers."""

_DISABLED_HTTP_STATUSES = {
    401: "unauthorized",
    403: "forbidden",
    429: "rate-limited",
}


def source_failure_reason(status_code):
    """Return a disabling reason for permanent/auth/quota failures, else None."""
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return None
    return _DISABLED_HTTP_STATUSES.get(code)
