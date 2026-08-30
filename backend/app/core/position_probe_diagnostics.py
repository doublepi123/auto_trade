from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Final, Literal, TypedDict

POSITION_PROBE_MESSAGE_LIMIT: Final = 512
POSITION_PROBE_STDERR_LIMIT: Final = 1_024
_FIELD_LIMIT: Final = 80
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(access[_ -]?token|authorization|api[_ -]?key|app[_ -]?secret)"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


class PositionProbeErrorPayload(TypedDict):
    status: Literal["error"]
    error_type: str
    retryable: bool
    sdk_error_code: str
    sdk_error_category: str
    error_message: str


@dataclass(frozen=True, slots=True)
class PositionProbeDiagnostics:
    error_type: str
    sdk_error_code: str = ""
    sdk_error_category: str = ""
    error_message: str = ""
    probe_duration_ms: float = 0.0
    exit_code: int | None = None
    retry_count: int = 0
    stderr: str = ""


def redact_probe_text(value: str, *, limit: int) -> str:
    redacted = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        value,
    )
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    return redacted[:limit]


def _attribute_text(error: BaseException, names: tuple[str, ...]) -> str:
    for name in names:
        value = getattr(error, name, None)
        if value is not None and str(value).strip():
            return str(value).strip()[:_FIELD_LIMIT]
    return ""


def _error_category(error: BaseException, message: str) -> str:
    explicit = _attribute_text(error, ("category", "error_category"))
    if explicit:
        return explicit.upper()
    normalized = message.lower()
    if "token" in normalized and any(
        marker in normalized
        for marker in ("expired", "invalid", "unauthorized", "401")
    ):
        return "AUTHENTICATION"
    if "rate limit" in normalized or "429" in normalized:
        return "RATE_LIMIT"
    if "timeout" in normalized:
        return "TIMEOUT"
    if "connection" in normalized or "network" in normalized:
        return "NETWORK"
    return type(error).__name__.upper()[:_FIELD_LIMIT]


def build_position_probe_error_payload(
    error: BaseException,
    *,
    retryable: bool,
) -> PositionProbeErrorPayload:
    message = redact_probe_text(
        str(error),
        limit=POSITION_PROBE_MESSAGE_LIMIT,
    )
    return {
        "status": "error",
        "error_type": type(error).__name__[:_FIELD_LIMIT],
        "retryable": retryable,
        "sdk_error_code": _attribute_text(
            error,
            ("code", "error_code", "status_code"),
        ),
        "sdk_error_category": _error_category(error, message),
        "error_message": message,
    }


class PositionProbeRuntimeError(RuntimeError):
    def __init__(self, diagnostics: PositionProbeDiagnostics) -> None:
        self.diagnostics = diagnostics
        super().__init__(
            "isolated broker position snapshot failed "
            f"({diagnostics.error_type})"
        )

    def with_retry_count(self, retry_count: int) -> PositionProbeRuntimeError:
        return PositionProbeRuntimeError(
            replace(self.diagnostics, retry_count=retry_count)
        )


class PositionProbeConnectionError(ConnectionError):
    def __init__(self, diagnostics: PositionProbeDiagnostics) -> None:
        self.diagnostics = diagnostics
        super().__init__(
            "isolated broker position snapshot failed "
            f"({diagnostics.error_type})"
        )

    def with_retry_count(self, retry_count: int) -> PositionProbeConnectionError:
        return PositionProbeConnectionError(
            replace(self.diagnostics, retry_count=retry_count)
        )


class PositionProbeTimeoutError(TimeoutError):
    def __init__(self, diagnostics: PositionProbeDiagnostics) -> None:
        self.diagnostics = diagnostics
        super().__init__(diagnostics.error_message)

    def with_retry_count(self, retry_count: int) -> PositionProbeTimeoutError:
        return PositionProbeTimeoutError(
            replace(self.diagnostics, retry_count=retry_count)
        )


class PositionProbeProtocolError(RuntimeError):
    def __init__(self, diagnostics: PositionProbeDiagnostics) -> None:
        self.diagnostics = diagnostics
        super().__init__("malformed broker position probe payload")

    def with_retry_count(self, retry_count: int) -> PositionProbeProtocolError:
        return PositionProbeProtocolError(
            replace(self.diagnostics, retry_count=retry_count)
        )
