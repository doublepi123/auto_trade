"""Configuration validation CLI — focused tests.

Covers valid/warning/error reports, invalid prod config without traceback,
SQLite error, unsafe override warnings, secret redaction, JSON/text formats,
exit codes, and no data-dir/runtime mutation. Subprocess tests are used for
import-time failure paths and the module entrypoint.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app.cli.validate_config import (
    ValidationIssue,
    ValidationReport,
    format_json,
    format_text,
    validate_settings,
)
from app.config import Settings


# ---------------------------------------------------------------------------
# Pure helper tests (no subprocess)
# ---------------------------------------------------------------------------


def _issue(severity: str = "WARNING", code: str = "X", setting: str = "s", message: str = "m") -> ValidationIssue:
    return ValidationIssue(code=code, severity=severity, setting=setting, message=message)  # type: ignore[arg-type]


class TestValidationReportHelpers:
    def test_empty_report_has_no_errors_or_warnings(self) -> None:
        r = ValidationReport()
        assert r.has_errors is False
        assert r.has_warnings is False
        assert r.errors() == []
        assert r.warnings() == []

    def test_has_errors_detects_error_severity(self) -> None:
        r = ValidationReport(issues=[_issue("WARNING"), _issue("ERROR")])
        assert r.has_errors is True
        assert r.has_warnings is True
        assert len(r.errors()) == 1
        assert len(r.warnings()) == 1

    def test_add_appends_issue(self) -> None:
        r = ValidationReport()
        r.add(_issue())
        assert len(r.issues) == 1


class TestFormatText:
    def test_ok_when_no_issues(self) -> None:
        text = format_text(ValidationReport())
        assert "OK" in text
        assert "no issues" in text

    def test_counts_and_lines(self) -> None:
        r = ValidationReport(issues=[_issue("ERROR", "CODE_A", "setting_a", "msg a"), _issue("WARNING", "CODE_B", "setting_b", "msg b")])
        text = format_text(r)
        assert "1 error(s)" in text
        assert "1 warning(s)" in text
        assert "ERROR CODE_A [setting_a]: msg a" in text
        assert "WARNING CODE_B [setting_b]: msg b" in text

    def test_issue_without_setting_omits_brackets(self) -> None:
        r = ValidationReport(issues=[_issue("ERROR", "CODE", "", "msg")])
        text = format_text(r)
        assert "ERROR CODE: msg" in text
        assert "[]" not in text


class TestFormatJson:
    def test_stable_json_shape_and_sort(self) -> None:
        r = ValidationReport(issues=[_issue("ERROR", "CODE_A", "setting_a", "msg a")])
        data = json.loads(format_json(r))
        assert data["has_errors"] is True
        assert data["has_warnings"] is False
        assert data["error_count"] == 1
        assert data["warning_count"] == 0
        assert data["issues"] == [
            {"code": "CODE_A", "severity": "ERROR", "setting": "setting_a", "message": "msg a"}
        ]

    def test_json_is_stable_across_calls(self) -> None:
        r = ValidationReport(issues=[_issue("ERROR", "B", "s2", "m2"), _issue("WARNING", "A", "s1", "m1")])
        a = format_json(r)
        b = format_json(r)
        assert a == b


class TestValidateSettings:
    def test_valid_default_settings_no_issues(self, monkeypatch, tmp_path) -> None:
        # Use code defaults, not developer .env.
        monkeypatch.chdir(tmp_path)
        s = Settings()
        report = validate_settings(s)
        # Default dev settings: empty api_key in dev -> WARNING is expected.
        codes = {i.code for i in report.issues}
        assert "EMPTY_API_KEY_DEV_TEST" in codes
        assert not any(i.severity == "ERROR" for i in report.issues)

    def test_non_sqlite_database_is_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", "postgresql://user:pass@host/db")
        s = Settings()
        report = validate_settings(s)
        errors = [i for i in report.issues if i.severity == "ERROR"]
        assert any(i.code == "NON_SQLITE_DATABASE" for i in errors)
        # The URL value must never appear in any issue message.
        for i in report.issues:
            assert "postgresql://user:pass@host/db" not in i.message
            assert "host/db" not in i.message

    def test_sqlite_database_no_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", "sqlite:///./data/auto_trade.db")
        s = Settings()
        report = validate_settings(s)
        assert not any(i.code == "NON_SQLITE_DATABASE" for i in report.issues)

    def test_p0_short_entries_violation_is_error(self, monkeypatch, tmp_path) -> None:
        # Settings clamps allow_short_entries to False, so we patch the
        # attribute to simulate a hypothetical invariant breach.
        monkeypatch.chdir(tmp_path)
        s = Settings()
        with mock.patch.object(s, "allow_short_entries", True):
            report = validate_settings(s)
        assert any(i.code == "P0_SHORT_ENTRIES_NOT_FAIL_CLOSED" and i.severity == "ERROR" for i in report.issues)

    def test_p0_position_addons_violation_is_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        s = Settings()
        with mock.patch.object(s, "hard_allow_position_addons", True):
            report = validate_settings(s)
        assert any(i.code == "P0_POSITION_ADDONS_NOT_FAIL_CLOSED" and i.severity == "ERROR" for i in report.issues)

    def test_p0_llm_shadow_violation_is_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        s = Settings()
        with mock.patch.object(s, "llm_shadow_mode", False):
            report = validate_settings(s)
        assert any(i.code == "P0_LLM_SHADOW_NOT_FAIL_CLOSED" and i.severity == "ERROR" for i in report.issues)

    def test_empty_api_key_dev_is_warning(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_ENV", "dev")
        monkeypatch.setenv("AUTO_TRADE_API_KEY", "")
        s = Settings()
        report = validate_settings(s)
        assert any(i.code == "EMPTY_API_KEY_DEV_TEST" and i.severity == "WARNING" for i in report.issues)

    def test_empty_api_key_test_is_warning(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_ENV", "test")
        monkeypatch.setenv("AUTO_TRADE_API_KEY", "")
        s = Settings()
        report = validate_settings(s)
        assert any(i.code == "EMPTY_API_KEY_DEV_TEST" and i.severity == "WARNING" for i in report.issues)

    def test_full_buying_power_is_warning(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_FULL_BUYING_POWER_USAGE_ENABLED", "true")
        s = Settings()
        report = validate_settings(s)
        assert any(i.code == "FULL_BUYING_POWER_ENABLED" and i.severity == "WARNING" for i in report.issues)

    def test_unsafe_override_short_entries_warning(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_ALLOW_SHORT_ENTRIES", "true")
        s = Settings()
        report = validate_settings(s)
        assert any(i.code == "UNSAFE_OVERRIDE_SHORT_ENTRIES_IGNORED" and i.severity == "WARNING" for i in report.issues)
        # The attempted value must not appear.
        for i in report.issues:
            assert "true" not in i.message

    def test_unsafe_override_position_addons_warning(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_HARD_ALLOW_POSITION_ADDONS", "true")
        s = Settings()
        report = validate_settings(s)
        assert any(i.code == "UNSAFE_OVERRIDE_POSITION_ADDONS_IGNORED" for i in report.issues)

    def test_unsafe_override_llm_shadow_warning(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_LLM_SHADOW_MODE", "false")
        s = Settings()
        report = validate_settings(s)
        assert any(i.code == "UNSAFE_OVERRIDE_LLM_SHADOW_IGNORED" for i in report.issues)

    def test_misnamed_deepseek_key_warning(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_DEEPSEEK_API_KEY", "sk-fake-value")
        s = Settings()
        report = validate_settings(s)
        matches = [i for i in report.issues if i.code == "MISNAMED_DEEPSEEK_API_KEY"]
        assert matches
        # The fake key value must never appear in the message.
        assert "sk-fake-value" not in matches[0].message

    def test_misnamed_minimax_key_warning(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_MINIMAX_API_KEY", "mm-fake-value")
        s = Settings()
        report = validate_settings(s)
        matches = [i for i in report.issues if i.code == "MISNAMED_MINIMAX_API_KEY"]
        assert matches
        assert "mm-fake-value" not in matches[0].message

    def test_secret_redaction_in_text_output(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", "postgresql://user:secret@host/db")
        monkeypatch.setenv("AUTO_TRADE_DEEPSEEK_API_KEY", "sk-super-secret")
        s = Settings()
        report = validate_settings(s)
        text = format_text(report)
        assert "secret" not in text
        assert "sk-super-secret" not in text

    def test_secret_redaction_in_json_output(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", "postgresql://user:secret@host/db")
        monkeypatch.setenv("AUTO_TRADE_DEEPSEEK_API_KEY", "sk-super-secret")
        s = Settings()
        report = validate_settings(s)
        out = format_json(report)
        assert "secret" not in out
        assert "sk-super-secret" not in out


# ---------------------------------------------------------------------------
# run_validation / exit codes
# ---------------------------------------------------------------------------


class TestRunValidation:
    def test_valid_config_exit_zero(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        from app.cli.validate_config import run_validation

        report, exit_code = run_validation()
        assert exit_code == 0
        assert not report.has_errors

    def test_error_config_exit_one(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", "postgresql://user:pass@host/db")
        from app.cli.validate_config import run_validation

        report, exit_code = run_validation()
        assert exit_code == 1
        assert report.has_errors

    def test_warning_only_config_exit_zero(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_FULL_BUYING_POWER_USAGE_ENABLED", "true")
        from app.cli.validate_config import run_validation

        report, exit_code = run_validation()
        assert exit_code == 0
        assert report.has_warnings
        assert not report.has_errors


# ---------------------------------------------------------------------------
# Import-time failure (subprocess) — invalid prod config must not traceback
# ---------------------------------------------------------------------------


def _run_cli_subprocess(env: dict[str, str], as_json: bool = False) -> tuple[int, str, str]:
    """Run the CLI as a subprocess with the given environment overrides.

    Uses a clean temp cwd so the developer .env is not picked up, and sets
    PYTHONPATH to the backend dir so ``app`` is importable.
    """
    cwd = tempfile.mkdtemp()
    full_env = os.environ.copy()
    # Strip developer-local secrets so they don't leak into the subprocess.
    for k in (
        "AUTO_TRADE_API_KEY",
        "DEEPSEEK_API_KEY",
        "MINIMAX_API_KEY",
        "AUTO_TRADE_DEEPSEEK_API_KEY",
        "AUTO_TRADE_MINIMAX_API_KEY",
    ):
        full_env.pop(k, None)
    full_env.update(env)
    cmd = [sys.executable, "-m", "app.cli.validate_config"]
    if as_json:
        cmd.append("--json")
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        # The CLI sets validation mode itself, but be explicit for the subprocess.
    )
    return proc.returncode, proc.stdout, proc.stderr


class TestSubprocessAndImportFailures:
    def test_invalid_prod_config_no_traceback(self) -> None:
        code, out, err = _run_cli_subprocess(
            {"AUTO_TRADE_ENV": "prod", "AUTO_TRADE_API_KEY": "", "PYTHONPATH": "/Users/lcy/code/auto_trade/backend"}
        )
        assert code == 1
        # No traceback in stdout or stderr.
        assert "Traceback" not in out
        assert "Traceback" not in err
        assert "CONFIG_LOAD_FAILED" in out
        assert "settings" in out

    def test_invalid_prod_config_json_no_traceback(self) -> None:
        code, out, err = _run_cli_subprocess(
            {"AUTO_TRADE_ENV": "prod", "AUTO_TRADE_API_KEY": "", "PYTHONPATH": "/Users/lcy/code/auto_trade/backend"},
            as_json=True,
        )
        assert code == 1
        assert "Traceback" not in out
        assert "Traceback" not in err
        data = json.loads(out)
        assert data["has_errors"] is True
        assert any(i["code"] == "CONFIG_LOAD_FAILED" for i in data["issues"])

    def test_valid_config_subprocess_exit_zero(self) -> None:
        code, out, _err = _run_cli_subprocess(
            {"AUTO_TRADE_ENV": "dev", "AUTO_TRADE_API_KEY": "", "PYTHONPATH": "/Users/lcy/code/auto_trade/backend"}
        )
        assert code == 0
        assert "OK" in out or "0 error" in out

    def test_non_sqlite_error_subprocess_exit_one(self) -> None:
        code, out, _err = _run_cli_subprocess(
            {
                "AUTO_TRADE_DATABASE_URL": "postgresql://user:pass@host/db",
                "PYTHONPATH": "/Users/lcy/code/auto_trade/backend",
            }
        )
        assert code == 1
        assert "NON_SQLITE_DATABASE" in out
        # The URL must not appear in output.
        assert "postgresql://user:pass@host/db" not in out

    def test_unsafe_override_warning_subprocess(self) -> None:
        code, out, _err = _run_cli_subprocess(
            {
                "AUTO_TRADE_ALLOW_SHORT_ENTRIES": "true",
                "AUTO_TRADE_LLM_SHADOW_MODE": "false",
                "PYTHONPATH": "/Users/lcy/code/auto_trade/backend",
            }
        )
        # Warnings only -> exit 0.
        assert code == 0
        assert "UNSAFE_OVERRIDE_SHORT_ENTRIES_IGNORED" in out
        assert "UNSAFE_OVERRIDE_LLM_SHADOW_IGNORED" in out


# ---------------------------------------------------------------------------
# No data-dir / runtime mutation
# ---------------------------------------------------------------------------


class TestNoDataDirMutation:
    def test_validation_mode_does_not_create_data_dir(self, monkeypatch, tmp_path) -> None:
        # Run from a fresh tmp dir; validation mode must not create ./data.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_CONFIG_VALIDATION_MODE", "1")
        from app.cli.validate_config import run_validation

        report, _code = run_validation()
        # No data directory should have been created in tmp_path.
        assert not (tmp_path / "data").exists()

    def test_load_for_validation_sets_and_clears_guard(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        from app.cli.validate_config import load_for_validation

        assert "AUTO_TRADE_CONFIG_VALIDATION_MODE" not in os.environ
        load_for_validation()
        # The guard must be cleared after loading.
        assert "AUTO_TRADE_CONFIG_VALIDATION_MODE" not in os.environ

    def test_normal_config_still_creates_data_dir(self, monkeypatch, tmp_path) -> None:
        # Confirm normal (non-validation) behavior is unchanged.
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AUTO_TRADE_CONFIG_VALIDATION_MODE", raising=False)
        s = Settings()
        s.ensure_data_dir()
        assert (tmp_path / "data").exists()