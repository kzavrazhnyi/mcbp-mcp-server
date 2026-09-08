"""Typed errors for interacting with BAS MCBP_AI.

Each subclass carries a machine-readable `code` and `http_status`, so a host application can map
them onto its own surface: an HTTP service turns them into a `{"error": {"code","message"}}`
response with the right status, while the MCP server turns the fatal ones into `MCPError` and lets
the rest reach the model as tool errors it can correct.
"""

from __future__ import annotations


class MCBPError(Exception):
    code: str = "UPSTREAM_ERROR"
    http_status: int = 502

    def __init__(self, message: str, *, code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status


class UpstreamError(MCBPError):
    code = "UPSTREAM_ERROR"
    http_status = 502


class NotFoundError(MCBPError):
    code = "NOT_FOUND"
    http_status = 404


class ParameterError(MCBPError):
    code = "BAD_PARAMETER"
    http_status = 400


class KeyMismatchError(MCBPError):
    """The base's internal "key" did not match (legacy `Key not found!`)."""

    code = "KEY_MISMATCH"
    http_status = 502


class PlusRequiredError(MCBPError):
    """The method requires the external "MCBP Plus" extension (legacy `MCBP Plus not found!`)."""

    code = "PLUS_REQUIRED"
    http_status = 501


class AuthError(MCBPError):
    """BAS rejected the credentials (Basic Auth 401/403 from upstream)."""

    code = "AUTH_FAILED"
    http_status = 401


class ForbiddenError(MCBPError):
    """The BAS account authenticated, but has no right on the object it asked for.

    Three different 403s reach this module and must not be collapsed: `AuthError` means the
    credentials themselves were not accepted, `KeyMismatchError` means the infobase key did not
    match, and this one means the login succeeded and BAS then refused THIS object. Only the
    last is something the caller can act on by asking for something else — which is why the
    model must see `FORBIDDEN` and not a generic upstream failure.
    """

    code = "FORBIDDEN"
    http_status = 403


class NotConnectedError(MCBPError):
    """No active connection to BAS — the host application must connect before issuing requests
    (`MCBPClient.startup()`, or whatever login step that host exposes)."""

    code = "NOT_CONNECTED"
    http_status = 409


class WriteFailedError(MCBPError):
    """A write through MCBP Plus was accepted, but Plus returned a conversion error
    (`WRITE_FAILED`)."""

    code = "WRITE_FAILED"
    http_status = 422


class ConversionNotConfiguredError(MCBPError):
    """Plus is present, but this type has no configured conversion rule in
    `InformationRegister.MCBP_DataConversion` — no object was written."""

    code = "CONVERSION_NOT_CONFIGURED"
    http_status = 422


class ConversionChangedError(MCBPError):
    """Plus's conversion rules changed while writing — Plus refused and named the expected
    fields."""

    code = "CONVERSION_CHANGED"
    http_status = 409


class NotConfiguredError(MCBPError):
    """The base's configuration is incomplete for the operation (e.g. the predefined "AI"
    exchange infobase is missing)."""

    code = "NOT_CONFIGURED"
    http_status = 422
