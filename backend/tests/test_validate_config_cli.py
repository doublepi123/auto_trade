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


# Backend root derived from this test file's location so the suite is portable
# across checkout paths (no hard-coded workstation paths).
_BACKEND_ROOT = Path(__file__).resolve().parents[1]


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

    def test_sqlite_pysqlite_driver_no_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", "sqlite+pysqlite:///./data/auto_trade.db")
        s = Settings()
        report = validate_settings(s)
        assert not any(i.code == "NON_SQLITE_DATABASE" for i in report.issues)

    @pytest.mark.parametrize(
        "good_url",
        [
            "sqlite://",
            "sqlite:///:memory:",
            "sqlite:///relative.db",
            "sqlite:////absolute.db",
            "sqlite+pysqlite:///:memory:",
            "sqlite+pysqlite:///./data/auto_trade.db",
        ],
    )
    def test_valid_sqlite_forms_accepted(self, monkeypatch, tmp_path, good_url) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", good_url)
        s = Settings()
        report = validate_settings(s)
        assert not any(i.code == "NON_SQLITE_DATABASE" for i in report.issues)

    @pytest.mark.parametrize(
        "bad_authority_url",
        [
            "sqlite://host/db",  # host component SQLite cannot accept
            "sqlite://user:very-secret@host/db",  # username + password + host
            "sqlite://:5432/db",  # port component
            "sqlite+pysqlite://host/db",  # pysqlite with host
            "sqlite+pysqlite://user:secret@host/db",  # pysqlite with credentials
        ],
    )
    def test_sqlite_authority_components_are_error(
        self, monkeypatch, tmp_path, bad_authority_url
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", bad_authority_url)
        s = Settings()
        report = validate_settings(s)
        assert any(
            i.code == "NON_SQLITE_DATABASE" and i.severity == "ERROR"
            for i in report.issues
        )
        # No hostname/username/password/path leakage in any issue message.
        for i in report.issues:
            assert "host" not in i.message
            assert "very-secret" not in i.message
            assert "secret" not in i.message
            assert "user" not in i.message
            assert "/db" not in i.message
            assert bad_authority_url not in i.message

    @pytest.mark.parametrize(
        "bad_url",
        [
            "sqliteevil://x/y",  # lookalike dialect
            "sqlite+evil:///./data.db",  # unsupported driver
            "sqlite",  # bare dialect, no URL
            "sqlitefoo",  # prefix lookalike
            "sqlite:/relative.db",  # malformed (single slash)
            "postgresql://user:pass@host/db",  # different dialect
            "mysql+pymysql://user:pass@host/db",  # different dialect
            "sqlite://host/db",  # SQLite with host authority (invalid)
            "sqlite://user:very-secret@host/db",  # SQLite with credentials (invalid)
            "",  # empty
        ],
    )
    def test_non_sqlite_lookalikes_are_error(self, monkeypatch, tmp_path, bad_url) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUTO_TRADE_DATABASE_URL", bad_url)
        s = Settings()
        report = validate_settings(s)
        assert any(i.code == "NON_SQLITE_DATABASE" and i.severity == "ERROR" for i in report.issues)
        # The URL value must never appear in any issue message (skip the empty
        # case since the empty string is trivially "in" any message).
        if bad_url:
            for i in report.issues:
                assert bad_url not in i.message

    def test_non_sqlite_lookalike_subprocess_exit_one(self, tmp_path) -> None:
        for bad_url in ("sqliteevil://x/y", "sqlite+evil:///./data.db", "sqlite", "sqlitefoo"):
            code, out, _err = _run_cli_subprocess(
                {"AUTO_TRADE_DATABASE_URL": bad_url},
                cwd=tmp_path,
            )
            assert code == 1, f"expected exit 1 for {bad_url}, got {code}: {out}"
            assert "NON_SQLITE_DATABASE" in out
            assert bad_url not in out

    def test_sqlite_authority_url_subprocess_exit_one_no_leak(self, tmp_path) -> None:
        # Fresh subprocess: ``sqlite://host/db`` must ERROR, exit 1, and not
        # leak the hostname/path in text or JSON output.
        bad_url = "sqlite://host/db"
        # Text output.
        code, out, err = _run_cli_subprocess(
            {"AUTO_TRADE_DATABASE_URL": bad_url},
            cwd=tmp_path,
        )
        assert code == 1, f"expected exit 1 for {bad_url}, got {code}: {out}"
        assert "NON_SQLITE_DATABASE" in out
        combined = out + err
        assert "host" not in combined
        assert "/db" not in combined
        assert bad_url not in combined
        # JSON output.
        code_j, out_j, err_j = _run_cli_subprocess(
            {"AUTO_TRADE_DATABASE_URL": bad_url},
            as_json=True,
            cwd=tmp_path,
        )
        assert code_j == 1
        data = json.loads(out_j)
        assert data["has_errors"] is True
        assert any(i["code"] == "NON_SQLITE_DATABASE" for i in data["issues"])
        combined_j = out_j + err_j
        assert "host" not in combined_j
        assert "/db" not in combined_j
        assert bad_url not in combined_j

    def test_sqlite_credential_url_subprocess_exit_one_no_leak(self, tmp_path) -> None:
        # Fresh subprocess: a credential-bearing SQLite URL must ERROR, exit 1,
        # and not leak the username/password/hostname/path in text or JSON.
        bad_url = "sqlite://user:very-secret@host/db"
        # Text output.
        code, out, err = _run_cli_subprocess(
            {"AUTO_TRADE_DATABASE_URL": bad_url},
            cwd=tmp_path,
        )
        assert code == 1, f"expected exit 1 for credential url, got {code}: {out}"
        assert "NON_SQLITE_DATABASE" in out
        combined = out + err
        assert "very-secret" not in combined
        assert "secret" not in combined
        assert "user" not in combined
        assert "host" not in combined
        assert "/db" not in combined
        assert bad_url not in combined
        # JSON output.
        code_j, out_j, err_j = _run_cli_subprocess(
            {"AUTO_TRADE_DATABASE_URL": bad_url},
            as_json=True,
            cwd=tmp_path,
        )
        assert code_j == 1
        data = json.loads(out_j)
        assert data["has_errors"] is True
        assert any(i["code"] == "NON_SQLITE_DATABASE" for i in data["issues"])
        combined_j = out_j + err_j
        assert "very-secret" not in combined_j
        assert "secret" not in combined_j
        assert "user" not in combined_j
        assert "host" not in combined_j
        assert "/db" not in combined_j
        assert bad_url not in combined_j

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


def _run_cli_subprocess(
    env: dict[str, str],
    as_json: bool = False,
    *,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """Run the CLI as a subprocess with the given environment overrides.

    Uses a clean temp cwd (so the developer .env is not picked up) and sets
    PYTHONPATH to the backend root derived from this test file's location so
    the suite is portable across checkout paths. The caller may pass an
    explicit ``cwd`` (e.g. a pytest ``tmp_path``) when directory side effects
    matter.
    """
    if cwd is None:
        cwd = Path(__file__).resolve().parent / "_cli_subprocess_tmp"
        cwd.mkdir(parents=True, exist_ok=True)
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
    # The old validation-mode env name must not affect behavior; ensure it is
    # unset so tests prove the guard is gone.
    full_env.pop("AUTO_TRADE_CONFIG_VALIDATION_MODE", None)
    full_env.update(env)
    full_env["PYTHONPATH"] = str(_BACKEND_ROOT)
    cmd = [sys.executable, "-m", "app.cli.validate_config"]
    if as_json:
        cmd.append("--json")
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


class TestSubprocessAndImportFailures:
    def test_invalid_prod_config_no_traceback(self, tmp_path) -> None:
        code, out, err = _run_cli_subprocess(
            {"AUTO_TRADE_ENV": "prod", "AUTO_TRADE_API_KEY": ""},
            cwd=tmp_path,
        )
        assert code == 1
        # No traceback in stdout or stderr.
        assert "Traceback" not in out
        assert "Traceback" not in err
        assert "CONFIG_LOAD_FAILED" in out
        assert "settings" in out

    def test_invalid_prod_config_json_no_traceback(self, tmp_path) -> None:
        code, out, err = _run_cli_subprocess(
            {"AUTO_TRADE_ENV": "prod", "AUTO_TRADE_API_KEY": ""},
            as_json=True,
            cwd=tmp_path,
        )
        assert code == 1
        assert "Traceback" not in out
        assert "Traceback" not in err
        data = json.loads(out)
        assert data["has_errors"] is True
        assert any(i["code"] == "CONFIG_LOAD_FAILED" for i in data["issues"])

    def test_valid_config_subprocess_exit_zero(self, tmp_path) -> None:
        code, out, _err = _run_cli_subprocess(
            {"AUTO_TRADE_ENV": "dev", "AUTO_TRADE_API_KEY": ""},
            cwd=tmp_path,
        )
        assert code == 0
        assert "OK" in out or "0 error" in out

    def test_non_sqlite_error_subprocess_exit_one(self, tmp_path) -> None:
        code, out, _err = _run_cli_subprocess(
            {"AUTO_TRADE_DATABASE_URL": "postgresql://user:pass@host/db"},
            cwd=tmp_path,
        )
        assert code == 1
        assert "NON_SQLITE_DATABASE" in out
        # The URL must not appear in output.
        assert "postgresql://user:pass@host/db" not in out

    def test_unsafe_override_warning_subprocess(self, tmp_path) -> None:
        code, out, _err = _run_cli_subprocess(
            {
                "AUTO_TRADE_ALLOW_SHORT_ENTRIES": "true",
                "AUTO_TRADE_LLM_SHADOW_MODE": "false",
            },
            cwd=tmp_path,
        )
        # Warnings only -> exit 0.
        assert code == 0
        assert "UNSAFE_OVERRIDE_SHORT_ENTRIES_IGNORED" in out
        assert "UNSAFE_OVERRIDE_LLM_SHADOW_IGNORED" in out

    def test_cli_does_not_create_data_dir(self, tmp_path) -> None:
        # Running the CLI in a fresh cwd must not create a data directory.
        code, _out, _err = _run_cli_subprocess(
            {"AUTO_TRADE_ENV": "dev", "AUTO_TRADE_API_KEY": ""},
            cwd=tmp_path,
        )
        assert code == 0
        assert not (tmp_path / "data").exists()

    def test_cli_valid_json_exit_zero(self, tmp_path) -> None:
        code, out, _err = _run_cli_subprocess(
            {"AUTO_TRADE_ENV": "dev", "AUTO_TRADE_API_KEY": ""},
            as_json=True,
            cwd=tmp_path,
        )
        assert code == 0
        data = json.loads(out)
        assert data["has_errors"] is False

    def test_cli_no_secret_or_path_leakage(self, tmp_path) -> None:
        # Even with secrets in env, the CLI output must not echo them or the
        # backend path.
        code, out, err = _run_cli_subprocess(
            {
                "AUTO_TRADE_ENV": "dev",
                "AUTO_TRADE_API_KEY": "",
                "AUTO_TRADE_DATABASE_URL": "postgresql://user:supersecret@host/db",
                "DEEPSEEK_API_KEY": "sk-leak-test",
            },
            cwd=tmp_path,
        )
        assert code == 1  # non-SQLite is an ERROR
        combined = out + err
        assert "supersecret" not in combined
        assert "sk-leak-test" not in combined
        assert str(_BACKEND_ROOT) not in combined


# ---------------------------------------------------------------------------
# No data-dir / runtime mutation
# ---------------------------------------------------------------------------


class TestNoDataDirMutation:
    def test_config_import_does_not_create_data_dir(self, tmp_path) -> None:
        # Importing app.config alone in a clean cwd must not create data/.
        # Use a subprocess so the already-cached app.config in this test
        # process does not mask the result.
        code, out, err = _run_python_subprocess(
            tmp_path,
            "import app.config; import pathlib; "
            "print('DATA_EXISTS=' + str(pathlib.Path('data').exists()))",
        )
        assert code == 0, err
        assert "DATA_EXISTS=False" in out

    def test_database_import_creates_data_dir(self, tmp_path) -> None:
        # Importing app.database in normal mode must create data/.
        code, out, err = _run_python_subprocess(
            tmp_path,
            "import app.database; import pathlib; "
            "print('DATA_EXISTS=' + str(pathlib.Path('data').exists()))",
        )
        assert code == 0, err
        assert "DATA_EXISTS=True" in out

    def test_old_validation_env_cannot_suppress_database_init(self, tmp_path) -> None:
        # Externally setting the old AUTO_TRADE_CONFIG_VALIDATION_MODE name must
        # NOT suppress normal database initialization (the guard is gone).
        code, out, err = _run_python_subprocess(
            tmp_path,
            "import app.database; import pathlib; "
            "print('DATA_EXISTS=' + str(pathlib.Path('data').exists()))",
            extra_env={"AUTO_TRADE_CONFIG_VALIDATION_MODE": "1"},
        )
        assert code == 0, err
        assert "DATA_EXISTS=True" in out

    def test_run_validation_does_not_create_data_dir(self, monkeypatch, tmp_path) -> None:
        # In-process: run_validation imports app.config (not app.database) and
        # must not create data/.
        monkeypatch.chdir(tmp_path)
        from app.cli.validate_config import run_validation

        report, _code = run_validation()
        assert not (tmp_path / "data").exists()

    def test_load_for_validation_does_not_mutate_env(self, monkeypatch, tmp_path) -> None:
        monkeypatch.chdir(tmp_path)
        from app.cli.validate_config import load_for_validation

        assert "AUTO_TRADE_CONFIG_VALIDATION_MODE" not in os.environ
        load_for_validation()
        # No ambient guard is set or cleared anymore.
        assert "AUTO_TRADE_CONFIG_VALIDATION_MODE" not in os.environ

    def test_normal_config_still_creates_data_dir(self, monkeypatch, tmp_path) -> None:
        # Confirm normal (non-validation) behavior is unchanged.
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AUTO_TRADE_CONFIG_VALIDATION_MODE", raising=False)
        s = Settings()
        s.ensure_data_dir()
        assert (tmp_path / "data").exists()


def _run_python_subprocess(
    cwd: Path,
    code: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a Python one-liner in a clean subprocess with backend on PYTHONPATH."""
    full_env = os.environ.copy()
    for k in (
        "AUTO_TRADE_API_KEY",
        "DEEPSEEK_API_KEY",
        "MINIMAX_API_KEY",
        "AUTO_TRADE_DEEPSEEK_API_KEY",
        "AUTO_TRADE_MINIMAX_API_KEY",
    ):
        full_env.pop(k, None)
    full_env.pop("AUTO_TRADE_CONFIG_VALIDATION_MODE", None)
    full_env["PYTHONPATH"] = str(_BACKEND_ROOT)
    if extra_env:
        full_env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr