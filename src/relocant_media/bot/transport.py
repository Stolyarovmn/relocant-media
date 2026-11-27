"""Safe diagnostics for Bot API requests, whose URLs carry credentials."""

import logging

import httpx


class TelegramAPIError(RuntimeError):
    pass


class _TelegramRequestLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple) and len(args) == 5
            and isinstance(args[1], httpx.URL)
            and args[1].host == "api.telegram.org" and args[1].path.startswith("/bot")
        ):
            record.msg = "Telegram HTTP request: %s %d"
            record.args = (args[0], args[3])
        return True


logging.getLogger("httpx").addFilter(_TelegramRequestLogFilter())


def _safe_request(method: str) -> httpx.Request:
    return httpx.Request("POST", f"https://api.telegram.org/{method}")


def telegram_transport_error(method: str, error: httpx.RequestError) -> httpx.RequestError:
    """Keep the original transport error type and discard its message and request."""
    return type(error)(
        f"Telegram {method} transport failed ({type(error).__name__})",
        request=_safe_request(method),
    )


def _response_object(response: httpx.Response) -> dict[str, object] | None:
    try:
        value: object = response.json()
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _keyboard_description(data: dict[str, object] | None) -> str | None:
    value = data.get("description") if data is not None else None
    if not isinstance(value, str):
        return None
    reason = value.lower().removeprefix("bad request: ")
    if reason.startswith("message is not modified"):
        return "Bad Request: message is not modified"
    if reason in {"message to edit not found", "message can't be edited"}:
        return f"Bad Request: {reason}"
    return None


def telegram_result(method: str, response: httpx.Response) -> object:
    """Keep the status and recognized edit outcomes without exposing the raw response."""
    status = response.status_code
    if not response.is_success:
        description = _keyboard_description(_response_object(response)) if status == 400 else None
        request = _safe_request(method)
        details = {"description": description} if description is not None else {}
        raise httpx.HTTPStatusError(
            f"Telegram {method} returned HTTP {status}",
            request=request,
            response=httpx.Response(status, json=details, request=request),
        )
    data = _response_object(response)
    if data is None:
        raise TelegramAPIError(f"Telegram {method} returned an invalid JSON object")
    if data.get("ok") is not True:
        if description := _keyboard_description(data):
            raise TelegramAPIError(f"Telegram {method} failed: {description}")
        raise TelegramAPIError(f"Telegram {method} rejected the request")
    return data.get("result")
