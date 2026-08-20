"""Типізовані помилки взаємодії з BAS MCBP_AI.

Кожен підклас має машинний `code` і `http_status`; обробник у `main.py`
перетворює їх на відповідь `{ "error": {"code","message"} }` з потрібним кодом.
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
    """Внутрішній «ключ» бази не збігся (legacy `Key not found!`)."""

    code = "KEY_MISMATCH"
    http_status = 502


class PlusRequiredError(MCBPError):
    """Метод потребує зовнішнього розширення «MCBP Plus» (legacy `MCBP Plus not found!`)."""

    code = "PLUS_REQUIRED"
    http_status = 501


class AuthError(MCBPError):
    """BAS відхилила облікові дані (Basic Auth 401/403 від upstream)."""

    code = "AUTH_FAILED"
    http_status = 401


class NotConnectedError(MCBPError):
    """Немає активного сеансу підключення до BAS — потрібен POST /api/v1/session/login."""

    code = "NOT_CONNECTED"
    http_status = 409


class WriteFailedError(MCBPError):
    """Запис через MCBP Plus прийнято, але Plus повернув помилку конвертації (`WRITE_FAILED`)."""

    code = "WRITE_FAILED"
    http_status = 422


class ConversionNotConfiguredError(MCBPError):
    """Plus є, але для цього типу немає налаштованого правила конвертації в
    `InformationRegister.MCBP_DataConversion` — жодного об'єкта не записано."""

    code = "CONVERSION_NOT_CONFIGURED"
    http_status = 422


class ConversionChangedError(MCBPError):
    """Правила конвертації Plus змінились під час запису — Plus відмовив і назвав очікувані поля."""

    code = "CONVERSION_CHANGED"
    http_status = 409


class NotConfiguredError(MCBPError):
    """Базова конфігурація неповна для операції (напр. немає предвизначеної інфобази обміну "AI")."""

    code = "NOT_CONFIGURED"
    http_status = 422
