"""Configuration validation CLI.

Loads and validates the current ``Settings`` without importing the database or
runner and without exposing secrets. Produces a sanitized report of issues
(ERROR/WARNING) identified only by safe codes, severities, setting/env names,
and generic messages. Never prints input/env values, URLs, keys, tokens, or
credentials.

Usage::

    python -m app.cli.validate_config          # text report (default)
    python -m app.cli.validate_config --json   # stable JSON report

Exit code is 0 when no ERROR issues are present (warnings allowed) and 1 when
any ERROR issue exists. Import-time/Pydantic configuration failures are caught
and reported as a sanitized ERROR rather than surfacing a traceback.

This CLI imports ``app.config`` (which is side-effect-free) but does NOT import
``app.database``, so no data directory or engine is created during validation.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["ERROR", "WARNING"]

# Safe, stable issue codes. These are the only identifiers emitted besides
# setting/env names and generic messages — never values.
CODE_NON_SQLITE_DATABASE = "NON_SQLITE_DATABASE"
CODE_P0_SHORT_ENTRIES = "P0_SHORT_ENTRIES_NOT_FAIL_CLOSED"
CODE_P0_POSITION_ADDONS = "P0_POSITION_ADDONS_NOT_FAIL_CLOSED"
CODE_P0_LLM_SHADOW = "P0_LLM_SHADOW_NOT_FAIL_CLOSED"
CODE_EMPTY_API_KEY_DEV_TEST = "EMPTY_API_KEY_DEV_TEST"
CODE_FULL_BUYING_POWER = "FULL_BUYING_POWER_ENABLED"
CODE_UNSAFE_OVERRIDE_SHORT_ENTRIES = "UNSAFE_OVERRIDE_SHORT_ENTRIES_IGNORED"
CODE_UNSAFE_OVERRIDE_POSITION_ADDONS = "UNSAFE_OVERRIDE_POSITION_ADDONS_IGNORED"
CODE_UNSAFE_OVERRIDE_LLM_SHADOW = "UNSAFE_OVERRIDE_LLM_SHADOW_IGNORED"
CODE_MISNAMED_DEEPSEEK_KEY = "MISNAMED_DEEPSEEK_API_KEY"
CODE_MISNAMED_MINIMAX_KEY = "MISNAMED_MINIMAX_API_KEY"
CODE_CONFIG_LOAD_FAILED = "CONFIG_LOAD_FAILED"


@dataclass(frozen=True)
class ValidationIssue:
    """A single sanitized validation finding.

    ``setting`` is a setting/env *name* only (never a value). ``message`` is a
    generic, human-readable explanation that must not contain secrets, URLs, or
    input values.
    """

    code: str
    severity: Severity
    setting: str = ""
    message: str = ""


@dataclass
class ValidationReport:
    """Aggregated validation result with pure formatting helpers."""

    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "ERROR" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == "WARNING" for i in self.issues)

    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "WARNING"]

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)


def _is_supported_sqlite_url(db_url: str) -> bool:
    """Return True iff *db_url* is a supported SQLite URL.

    Uses SQLAlchemy ``make_url`` for real URL parsing (not a prefix check) and
    an explicit driver allowlist so lookalikes (``sqliteevil://``,
    ``sqlite+evil://``, bare ``sqlite``, ``sqlitefoo``) are rejected. Also
    rejects any parsed URL with authority components the SQLite dialect cannot
    accept: a non-null username, password, host, or port. Valid forms such as
    ``sqlite://``, ``sqlite:///:memory:``, ``sqlite:///relative.db``,
    ``sqlite:////absolute.db``, and the supported ``sqlite+pysqlite`` equivalents
    remain accepted. Does not import ``app.database`` or instantiate/connect an
    engine. Never echoes the URL/credential values.
    """
    from sqlalchemy.engine import make_url  # noqa: PLC0415 — local import keeps the helper pure/testable

    supported_drivers = {"sqlite", "sqlite+pysqlite"}
    try:
        url = make_url(db_url)
    except Exception:
        # Malformed URLs are not supported SQLite URLs.
        return False
    # drivername is the full "dialect+driver" or bare "dialect"; compare against
    # the explicit allowlist so lookalikes cannot slip through.
    if url.drivername not in supported_drivers:
        return False
    # The SQLite dialect has no notion of a server authority: any username,
    # password, host, or port is invalid (e.g. ``sqlite://host/db`` or
    # ``sqlite://user:pass@host/db``). Reject these without echoing the values.
    if url.username is not None or url.password is not None:
        return False
    if url.host is not None or url.port is not None:
        return False
    return True


def validate_settings(settings: Any) -> ValidationReport:
    """Run all validation checks against a loaded ``Settings`` instance.

    Pure: takes a Settings object and returns a report. Does not import the
    database or runner and does not read secrets into output. The ``settings``
    object is only inspected for *shape* (booleans/strings) and never for
    secret *values*; where a value check is needed (e.g. database_url scheme),
    only a boolean/scheme derivation is used and the value itself is never
    emitted.
    """
    report = ValidationReport()

    # --- SQLite-only database -------------------------------------------------
    # Real URL parsing with an explicit driver allowlist; the URL value is
    # never emitted, only the boolean result is used.
    db_url = getattr(settings, "database_url", "") or ""
    if not _is_supported_sqlite_url(db_url):
        report.add(
            ValidationIssue(
                code=CODE_NON_SQLITE_DATABASE,
                severity="ERROR",
                setting="database_url",
                message="repository supports SQLite only; non-SQLite database_url is not permitted",
            )
        )

    # --- P0 invariants must remain fail-closed --------------------------------
    if getattr(settings, "allow_short_entries", False) is not False:
        report.add(
            ValidationIssue(
                code=CODE_P0_SHORT_ENTRIES,
                severity="ERROR",
                setting="allow_short_entries",
                message="P0 invariant violated: short entries must remain disabled (fail-closed)",
            )
        )
    if getattr(settings, "hard_allow_position_addons", False) is not False:
        report.add(
            ValidationIssue(
                code=CODE_P0_POSITION_ADDONS,
                severity="ERROR",
                setting="hard_allow_position_addons",
                message="P0 invariant violated: position add-ons must remain disabled (fail-closed)",
            )
        )
    if getattr(settings, "llm_shadow_mode", True) is not True:
        report.add(
            ValidationIssue(
                code=CODE_P0_LLM_SHADOW,
                severity="ERROR",
                setting="llm_shadow_mode",
                message="P0 invariant violated: LLM shadow mode must remain enabled (fail-closed)",
            )
        )

    # --- Empty API key in dev/test is a WARNING (prod invalidity is an ERROR
    # captured by Settings itself at load time) -------------------------------
    env = getattr(settings, "env", "dev")
    api_key_present = bool(getattr(settings, "api_key", ""))
    if not api_key_present and env in ("dev", "test"):
        report.add(
            ValidationIssue(
                code=CODE_EMPTY_API_KEY_DEV_TEST,
                severity="WARNING",
                setting="api_key",
                message="API key is empty in dev/test environment; authentication is disabled",
            )
        )

    # --- Full buying-power mode is a WARNING ----------------------------------
    if getattr(settings, "full_buying_power_usage_enabled", False):
        report.add(
            ValidationIssue(
                code=CODE_FULL_BUYING_POWER,
                severity="WARNING",
                setting="full_buying_power_usage_enabled",
                message="full buying-power usage is enabled; intended for paper accounts only",
            )
        )

    # --- Attempted unsafe env overrides (by NAME only) -------------------------
    # These are clamped/ignored by Settings; warn the operator. We check the
    # raw environment by name only and never echo the attempted value.
    if os.environ.get("AUTO_TRADE_ALLOW_SHORT_ENTRIES", "").lower() in ("true", "1", "yes"):
        report.add(
            ValidationIssue(
                code=CODE_UNSAFE_OVERRIDE_SHORT_ENTRIES,
                severity="WARNING",
                setting="AUTO_TRADE_ALLOW_SHORT_ENTRIES",
                message="unsafe override attempted but ignored/clamped: short entries remain disabled",
            )
        )
    if os.environ.get("AUTO_TRADE_HARD_ALLOW_POSITION_ADDONS", "").lower() in ("true", "1", "yes"):
        report.add(
            ValidationIssue(
                code=CODE_UNSAFE_OVERRIDE_POSITION_ADDONS,
                severity="WARNING",
                setting="AUTO_TRADE_HARD_ALLOW_POSITION_ADDONS",
                message="unsafe override attempted but ignored/clamped: position add-ons remain disabled",
            )
        )
    if os.environ.get("AUTO_TRADE_LLM_SHADOW_MODE", "").lower() in ("false", "0", "no"):
        report.add(
            ValidationIssue(
                code=CODE_UNSAFE_OVERRIDE_LLM_SHADOW,
                severity="WARNING",
                setting="AUTO_TRADE_LLM_SHADOW_MODE",
                message="unsafe override attempted but ignored/clamped: LLM shadow mode remains enabled",
            )
        )

    # --- Misnamed LLM API keys (by NAME only) ---------------------------------
    if os.environ.get("AUTO_TRADE_DEEPSEEK_API_KEY"):
        report.add(
            ValidationIssue(
                code=CODE_MISNAMED_DEEPSEEK_KEY,
                severity="WARNING",
                setting="AUTO_TRADE_DEEPSEEK_API_KEY",
                message="misnamed env var ignored; use DEEPSEEK_API_KEY (no AUTO_TRADE_ prefix)",
            )
        )
    if os.environ.get("AUTO_TRADE_MINIMAX_API_KEY"):
        report.add(
            ValidationIssue(
                code=CODE_MISNAMED_MINIMAX_KEY,
                severity="WARNING",
                setting="AUTO_TRADE_MINIMAX_API_KEY",
                message="misnamed env var ignored; use MINIMAX_API_KEY (no AUTO_TRADE_ prefix)",
            )
        )

    return report


def format_text(report: ValidationReport) -> str:
    """Render the report as human-readable text. No secrets are emitted."""
    lines: list[str] = []
    error_count = len(report.errors())
    warning_count = len(report.warnings())
    if not report.issues:
        lines.append("configuration validation: OK (no issues)")
        return "\n".join(lines)

    lines.append(
        f"configuration validation: {error_count} error(s), {warning_count} warning(s)"
    )
    for issue in report.issues:
        setting_part = f" [{issue.setting}]" if issue.setting else ""
        lines.append(f"  {issue.severity} {issue.code}{setting_part}: {issue.message}")
    return "\n".join(lines)


def format_json(report: ValidationReport) -> str:
    """Render the report as stable JSON. No secrets are emitted."""
    return json.dumps(
        {
            "has_errors": report.has_errors,
            "has_warnings": report.has_warnings,
            "error_count": len(report.errors()),
            "warning_count": len(report.warnings()),
            "issues": [asdict(i) for i in report.issues],
        },
        sort_keys=True,
        indent=2,
    )


def load_for_validation() -> tuple[ValidationReport | None, Any | None]:
    """Load ``Settings`` for validation without filesystem side effects.

    Returns ``(report, settings)``. When the import or Settings construction
    fails (Pydantic ValidationError or import-time configuration error),
    ``settings`` is ``None`` and ``report`` contains a single sanitized ERROR
    describing the failure without a traceback or secret values. When load
    succeeds, ``report`` is ``None`` and ``settings`` is the instance.

    Imports ``app.config`` (which no longer creates directories at import time)
    but deliberately does NOT import ``app.database``, so no data directory or
    engine is created. No ambient environment guard is mutated.
    """
    try:
        from app.config import Settings  # noqa: PLC0415 — deferred import is intentional

        settings = Settings()
    except Exception:
        # Sanitize: do not surface the exception message verbatim (it may
        # contain env values) and never print a traceback. Report a generic
        # ERROR by code/setting only.
        return (
            ValidationReport(
                issues=[
                    ValidationIssue(
                        code=CODE_CONFIG_LOAD_FAILED,
                        severity="ERROR",
                        setting="settings",
                        message="configuration failed to load; check env vars and settings for invalid values",
                    )
                ]
            ),
            None,
        )

    return None, settings


def run_validation() -> tuple[ValidationReport, int]:
    """Run the full validation flow and return ``(report, exit_code)``.

    Exit code is 0 when no ERROR issues exist (warnings allowed) and 1
    otherwise. Import-time failures produce a single ERROR report.
    """
    load_report, settings = load_for_validation()
    if load_report is not None:
        return load_report, 1
    report = validate_settings(settings)
    return report, 1 if report.has_errors else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the current configuration without importing the database "
            "or runner and without exposing secrets. Reports issues by safe "
            "code/severity/setting only. Exit code is 0 when no ERROR issues "
            "are present (warnings allowed) and 1 otherwise."
        ),
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit a stable JSON report instead of text",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    report, exit_code = run_validation()
    out = format_json(report) if args.as_json else format_text(report)
    print(out)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())