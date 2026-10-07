from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_docker_compose_passes_api_key_to_backend() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "AUTO_TRADE_API_KEY=" in compose


def test_docker_compose_does_not_pass_api_key_to_frontend_build() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "VITE_AUTO_TRADE_API_KEY:" not in compose


def test_frontend_dockerfile_does_not_accept_api_key_build_arg() -> None:
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG VITE_AUTO_TRADE_API_KEY" not in dockerfile


def test_frontend_docker_entrypoint_injects_proxy_api_key() -> None:
    entrypoint = (ROOT / "frontend" / "docker-entrypoint.sh").read_text(encoding="utf-8")
    nginx_conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "runtime-config.js" not in entrypoint
    assert "AUTO_TRADE_API_KEY" in entrypoint
    assert "__AUTO_TRADE_PROXY_API_KEY__" in nginx_conf
    assert 'proxy_set_header X-API-Key "__AUTO_TRADE_PROXY_API_KEY__";' in nginx_conf


def test_docker_compose_passes_api_key_to_frontend_runtime() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    frontend_block = compose.split("\n  frontend:", maxsplit=1)[1]

    assert "AUTO_TRADE_API_KEY=" in frontend_block


def test_dockerhub_compose_passes_api_key_to_frontend_runtime() -> None:
    compose = (ROOT / "docker-compose.dockerhub.yaml").read_text(encoding="utf-8")
    frontend_block = compose.split("\n  frontend:", maxsplit=1)[1]

    assert "AUTO_TRADE_API_KEY=" in frontend_block


def test_docker_compose_passes_deepseek_key_to_backend() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY=" in compose


def test_docker_compose_passes_minimax_key_and_provider_to_backend() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub_compose = (ROOT / "docker-compose.dockerhub.yaml").read_text(encoding="utf-8")

    assert "AUTO_TRADE_LLM_PROVIDER=" in compose
    assert "MINIMAX_BASE_URL=" in compose
    assert "MINIMAX_API_KEY=" in compose
    assert "MINIMAX_MODEL=" in compose
    assert "MINIMAX_THINKING_TYPE=" in compose
    assert "MINIMAX_MAX_COMPLETION_TOKENS=" in compose
    assert "AUTO_TRADE_LLM_PROVIDER=" in dockerhub_compose
    assert "MINIMAX_BASE_URL=" in dockerhub_compose
    assert "MINIMAX_API_KEY=" in dockerhub_compose
    assert "MINIMAX_MODEL=" in dockerhub_compose
    assert "MINIMAX_THINKING_TYPE=" in dockerhub_compose
    assert "MINIMAX_MAX_COMPLETION_TOKENS=" in dockerhub_compose


def test_env_example_documents_llm_provider_keys() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "AUTO_TRADE_LLM_PROVIDER=" in env_example
    assert "DEEPSEEK_API_KEY=" in env_example
    assert "MINIMAX_BASE_URL=" in env_example
    assert "MINIMAX_API_KEY=" in env_example


def test_deploy_files_expose_storage_retention_controls() -> None:
    keys = {
        "AUTO_TRADE_LLM_INTERACTION_RETENTION_DAYS",
        "AUTO_TRADE_LLM_NO_ACTION_RETENTION_DAYS",
        "AUTO_TRADE_LLM_CONTEXT_SNAPSHOT_MAX_BYTES",
        "AUTO_TRADE_LLM_STORAGE_MAINTENANCE_INTERVAL_MINUTES",
        "AUTO_TRADE_LLM_STORAGE_MAINTENANCE_BATCH_SIZE",
        "AUTO_TRADE_STRATEGY_V2_WAIT_RETENTION_DAYS",
        "AUTO_TRADE_STRATEGY_V2_WAIT_MAINTENANCE_BATCH_SIZE",
        "AUTO_TRADE_STRATEGY_V2_DIAGNOSTIC_WAIT_RETENTION_DAYS",
        "AUTO_TRADE_STRATEGY_V2_DIAGNOSTIC_WAIT_MAINTENANCE_BATCH_SIZE",
        "AUTO_TRADE_STRATEGY_V2_FORWARD_REPLAY_ARTIFACT_RETENTION_DAYS",
        "AUTO_TRADE_STRATEGY_V2_FORWARD_REPLAY_ARTIFACT_MAINTENANCE_BATCH_SIZE",
    }
    for filename in ("docker-compose.yaml", "docker-compose.dockerhub.yaml"):
        compose = (ROOT / filename).read_text(encoding="utf-8")
        for key in keys:
            assert f"{key}=" in compose

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in keys:
        assert f"{key}=" in env_example


def test_compose_trusts_only_explicit_docker_proxy_network_for_audit_ip() -> None:
    for filename in ("docker-compose.yaml", "docker-compose.dockerhub.yaml"):
        compose = (ROOT / filename).read_text(encoding="utf-8")
        assert "AUTO_TRADE_AUDIT_TRUSTED_PROXY_CIDRS=" in compose
        assert "172.16.0.0/12" in compose

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "AUTO_TRADE_AUDIT_TRUSTED_PROXY_CIDRS=" in env_example


def test_deploy_files_expose_p0_hard_safety_controls() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub = (ROOT / "docker-compose.dockerhub.yaml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    keys = {
        "AUTO_TRADE_LLM_SHADOW_MODE",
        "AUTO_TRADE_LLM_MAX_ORDER_PRICE_DEVIATION_PCT",
        "AUTO_TRADE_LLM_MAX_INTERVAL_BOUND_DEVIATION_PCT",
        "AUTO_TRADE_HARD_ALLOW_POSITION_ADDONS",
        "AUTO_TRADE_HARD_MAX_POSITION_QUANTITY",
        "AUTO_TRADE_HARD_MAX_POSITION_NOTIONAL",
        "AUTO_TRADE_HARD_MAX_RISK_PER_TRADE",
        "AUTO_TRADE_FULL_BUYING_POWER_USAGE_ENABLED",
        "AUTO_TRADE_ENTRY_ROUND_TRIP_SLIPPAGE_BPS",
        "AUTO_TRADE_MIN_ENTRY_EDGE_COST_RATIO",
        "AUTO_TRADE_MIN_ENTRY_REWARD_RISK_RATIO",
        "AUTO_TRADE_TRADING_OPEN_WARMUP_MINUTES",
        "AUTO_TRADE_HARD_STOP_LOSS_PCT",
        "AUTO_TRADE_HARD_MAX_HOLDING_MINUTES",
        "AUTO_TRADE_HARD_ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE",
        "AUTO_TRADE_HARD_FLATTEN_MINUTES_BEFORE_CLOSE",
    }
    for key in keys:
        assert f"{key}=" in compose
        assert f"{key}=" in dockerhub
        assert f"{key}=" in env_example

    full_buying_power_default = (
        "AUTO_TRADE_FULL_BUYING_POWER_USAGE_ENABLED="
        "${AUTO_TRADE_FULL_BUYING_POWER_USAGE_ENABLED:-false}"
    )
    assert full_buying_power_default in compose
    assert full_buying_power_default in dockerhub

    # The backend keeps a false-by-default defence-in-depth flag, but P0
    # schema and migration policy unconditionally disable short entries. Do
    # not advertise this internal flag as an operator-supported bypass.
    assert "AUTO_TRADE_ALLOW_SHORT_ENTRIES=" in compose
    assert "AUTO_TRADE_ALLOW_SHORT_ENTRIES=" in dockerhub
    assert "AUTO_TRADE_ALLOW_SHORT_ENTRIES=" not in env_example


def test_deploy_files_expose_degraded_exit_pricing_controls() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub = (ROOT / "docker-compose.dockerhub.yaml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    keys = {
        "AUTO_TRADE_DEGRADED_EXIT_MAX_ADVERSE_DEVIATION_PCT",
        "AUTO_TRADE_DEGRADED_EXIT_REFERENCE_MAX_AGE_SECONDS",
    }
    for key in keys:
        assert f"{key}=" in compose
        assert f"{key}=" in dockerhub
        assert f"{key}=" in env_example

    from app.config import Settings

    compose_deviation_default = (
        "AUTO_TRADE_DEGRADED_EXIT_MAX_ADVERSE_DEVIATION_PCT="
        "${AUTO_TRADE_DEGRADED_EXIT_MAX_ADVERSE_DEVIATION_PCT:-"
        f"{Settings().degraded_exit_max_adverse_deviation_pct}}}"
    )
    assert compose_deviation_default in compose
    assert compose_deviation_default in dockerhub

    compose_age_default = (
        "AUTO_TRADE_DEGRADED_EXIT_REFERENCE_MAX_AGE_SECONDS="
        "${AUTO_TRADE_DEGRADED_EXIT_REFERENCE_MAX_AGE_SECONDS:-"
        f"{Settings().degraded_exit_reference_max_age_seconds}}}"
    )
    assert compose_age_default in compose
    assert compose_age_default in dockerhub


def test_deploy_files_expose_universe_and_live_regime_controls() -> None:
    keys = {
        "AUTO_TRADE_UNIVERSE_SELECTION_ENABLED",
        "AUTO_TRADE_UNIVERSE_SELECTION_APPLY_TO_WATCHLIST",
        "AUTO_TRADE_UNIVERSE_SELECTION_ENABLE_SHADOW",
        "AUTO_TRADE_STRATEGY_V2_PORTFOLIO_SHADOW_ENABLED",
        "AUTO_TRADE_LIVE_EXIT_CHALLENGER_ENABLED",
        "AUTO_TRADE_UNIVERSE_SELECTION_INTERVAL_MINUTES",
        "AUTO_TRADE_WATCHLIST_QUANT_AUTO_SCORE_ENABLED",
        "AUTO_TRADE_WATCHLIST_QUANT_INTERVAL_MINUTES",
        "AUTO_TRADE_WATCHLIST_QUANT_SCORE_TTL_MINUTES",
        "AUTO_TRADE_WATCHLIST_QUANT_BATCH_SIZE",
        "AUTO_TRADE_UNIVERSE_SELECTION_MAX_SYMBOLS",
        "AUTO_TRADE_UNIVERSE_SELECTION_EXPLORATION_MAX_SYMBOLS",
        "AUTO_TRADE_UNIVERSE_SELECTION_EXPLORATION_TOP_SCORE_CHALLENGERS",
        "AUTO_TRADE_UNIVERSE_SELECTION_MAX_PER_SECTOR",
        "AUTO_TRADE_UNIVERSE_SELECTION_MIN_EVALUABLE_RATIO",
        "AUTO_TRADE_UNIVERSE_SELECTION_MIN_RESIDENCY_DAYS",
        "AUTO_TRADE_UNIVERSE_SELECTION_MIN_PRICE",
        "AUTO_TRADE_UNIVERSE_SELECTION_MIN_AVG_DOLLAR_VOLUME",
        "AUTO_TRADE_UNIVERSE_SELECTION_MAX_SPREAD_BPS",
        "AUTO_TRADE_UNIVERSE_SELECTION_MIN_REALIZED_VOL",
        "AUTO_TRADE_UNIVERSE_SELECTION_MAX_REALIZED_VOL",
        "AUTO_TRADE_UNIVERSE_SELECTION_MIN_ATR_PCT",
        "AUTO_TRADE_UNIVERSE_SELECTION_MAX_ATR_PCT",
        "AUTO_TRADE_LIVE_REGIME_GATE_ENABLED",
        "AUTO_TRADE_LIVE_REGIME_MAX_DATA_AGE_SECONDS",
        "AUTO_TRADE_LIVE_MAX_ENTRIES_PER_SYMBOL_PER_DAY",
        "AUTO_TRADE_LIVE_ENTRY_CROSSING_REQUIRED",
        "AUTO_TRADE_LIVE_ENTRY_CROSSING_MAX_AGE_SECONDS",
        "AUTO_TRADE_LIVE_ENTRY_CROSSING_SETTLE_SECONDS",
        "AUTO_TRADE_PROFIT_LOCK_ACTIVATION_PCT",
        "AUTO_TRADE_PROFIT_LOCK_LOCK_PCT",
    }
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for filename in ("docker-compose.yaml", "docker-compose.dockerhub.yaml"):
        compose = (ROOT / filename).read_text(encoding="utf-8")
        for key in keys:
            assert f"{key}=" in compose
            assert f"{key}=" in env_example


def test_deploy_files_expose_interval_recenter_half_width_control() -> None:
    """Compose must forward the recenter half-width override.

    A Settings field absent from compose is silently ignored at runtime: the
    container keeps the field default regardless of what the operator sets in
    .env (recorded auto-primary-switch incident). The ``:-`` fallback must be
    empty so an unset variable keeps the Settings default (None = fall back to
    llm_interval_volatility_threshold_pct), never a silently narrower/wider
    band.
    """
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub_compose = (
        ROOT / "docker-compose.dockerhub.yaml"
    ).read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    expected = (
        "AUTO_TRADE_INTERVAL_RECENTER_HALF_WIDTH_PCT="
        "${AUTO_TRADE_INTERVAL_RECENTER_HALF_WIDTH_PCT:-}"
    )
    assert expected in compose
    assert expected in dockerhub_compose
    assert "AUTO_TRADE_INTERVAL_RECENTER_HALF_WIDTH_PCT=" in env_example


def test_deploy_files_expose_paper_sizing_exception_controls() -> None:
    """Compose must forward every paper-sizing exception variable.

    A Settings field absent from compose is silently ignored at runtime: the
    container keeps the field default regardless of what the operator sets
    in .env (recorded auto-primary-switch incident). The ``:-0`` fallback
    equals the Settings default (0 = no request) so an unset variable ships
    fail-closed with the funded caps.
    """
    from app.config import Settings

    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub = (
        ROOT / "docker-compose.dockerhub.yaml"
    ).read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    keys = (
        "AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED",
        "AUTO_TRADE_PAPER_MAX_POSITION_NOTIONAL",
        "AUTO_TRADE_PAPER_MAX_POSITION_QUANTITY",
        "AUTO_TRADE_PAPER_MAX_RISK_PER_TRADE",
    )
    for key in keys:
        assert f"{key}=" in compose, key
        assert f"{key}=" in dockerhub, key
        assert f"{key}=" in env_example, key

    for amount_key in keys[1:]:
        expected = f"{amount_key}=${{{amount_key}:-0}}"
        assert expected in compose, expected
        assert expected in dockerhub, expected

    # The Settings defaults must stay "no request" so the compose fallback
    # never silently arms a relaxation.
    assert Settings().paper_max_position_notional == 0.0
    assert Settings().paper_max_position_quantity == 0
    assert Settings().paper_max_risk_per_trade == 0.0
    assert Settings().paper_account_confirmed is False


def test_compose_healthchecks_use_strict_readiness_endpoint() -> None:
    for filename in ("docker-compose.yaml", "docker-compose.dockerhub.yaml"):
        compose = (ROOT / filename).read_text(encoding="utf-8")
        healthcheck = compose.split("healthcheck:", maxsplit=1)[1].split(
            "restart:", maxsplit=1
        )[0]
        assert "/api/ready" in healthcheck
        assert "/api/health" not in healthcheck


def test_compose_caps_backend_and_frontend_json_logs() -> None:
    for filename in ("docker-compose.yaml", "docker-compose.dockerhub.yaml"):
        compose = (ROOT / filename).read_text(encoding="utf-8")
        backend_block, frontend_block = compose.split(
            "\n  frontend:",
            maxsplit=1,
        )
        for service_block in (backend_block, frontend_block):
            assert "driver: json-file" in service_block
            assert 'max-size: "10m"' in service_block
            assert 'max-file: "3"' in service_block


def test_env_example_defaults_deployments_to_production_mode() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert env_example.startswith("AUTO_TRADE_ENV=prod\n")


def test_docker_compose_publishes_frontend_loopback_by_default_and_keeps_backend_private() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    backend_block = compose.split("\n  frontend:", maxsplit=1)[0]

    assert "\n    ports:" not in backend_block
    assert "127.0.0.1:8000:8000" not in compose
    # BIND is env-driven; default is loopback-only. Operators can explicitly
    # override AUTO_TRADE_FRONTEND_BIND=0.0.0.0 for LAN access.
    assert "${AUTO_TRADE_FRONTEND_BIND:-127.0.0.1}:${AUTO_TRADE_FRONTEND_PORT:-8080}:80" in compose


def test_frontend_healthcheck_uses_ipv4_loopback() -> None:
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "http://127.0.0.1/" in dockerfile
    assert "http://localhost/" not in dockerfile


def test_backend_image_includes_index_membership_snapshot() -> None:
    dockerignore = (ROOT / "backend" / ".dockerignore").read_text(
        encoding="utf-8"
    )
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    snapshot = (
        ROOT
        / "backend"
        / "app"
        / "domain"
        / "universe_selection"
        / "data"
        / "index_membership_history.json"
    )

    assert "!app/domain/universe_selection/data/" in dockerignore
    assert (
        "!app/domain/universe_selection/data/"
        "index_membership_history.json"
    ) in dockerignore
    assert "COPY --chown=appuser:appuser app/ ./app/" in dockerfile
    assert "chmod -R a+rX /app" in dockerfile
    assert snapshot.is_file()


def test_docker_compose_passes_auto_primary_switch_settings() -> None:
    """A settings flag absent from compose is silently ignored at runtime.

    The switch was first deployed with the flag set in .env but missing here,
    so the container kept the default and never evaluated a switch.
    """
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub_compose = (
        ROOT / "docker-compose.dockerhub.yaml"
    ).read_text(encoding="utf-8")

    keys = (
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_ENABLED=",
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_INTERVAL_MINUTES=",
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_LOOKBACK_DAYS=",
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_MIN_SAMPLES=",
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_INCUMBENT_TREND_PCT=",
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_CANDIDATE_TREND_PCT=",
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_REACH_LOOKBACK_DAYS=",
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_MIN_REACH_RATE_PCT=",
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_MIN_CLOSED_TRADES=",
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_MAX_PRICE_AGE_SECONDS=",
    )
    for key in keys:
        assert key in compose, key
        assert key in dockerhub_compose, key


def test_auto_primary_switch_defaults_to_disabled_in_compose() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert (
        "AUTO_TRADE_AUTO_PRIMARY_SWITCH_ENABLED="
        "${AUTO_TRADE_AUTO_PRIMARY_SWITCH_ENABLED:-false}"
    ) in compose


def test_retired_quant_v6_controls_are_absent_from_deploy_files() -> None:
    """A retired Settings field must not linger in compose or .env.example.

    Compose only forwards variables it declares. Leaving the quant-v6 knobs
    documented would look operator-supported after the writer, cron, and
    reader are gone.
    """
    retired = (
        "AUTO_TRADE_WATCHLIST_QUANT_V6_ARTIFACT_RETENTION_DAYS",
        "AUTO_TRADE_WATCHLIST_QUANT_V6_ARTIFACT_MAINTENANCE_BATCH_SIZE",
        "AUTO_TRADE_WATCHLIST_QUANT_V6_DB_SIZE_BUDGET_MB",
        "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_ENABLED",
        "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_INTERVAL_MINUTES",
        "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_RETRY_INTERVAL_MINUTES",
        "AUTO_TRADE_WATCHLIST_QUANT_V6_EVALUATION_TIMEOUT_SECONDS",
        "AUTO_TRADE_WATCHLIST_QUANT_V6_PROVIDER_PAGE_TIMEOUT_SECONDS",
        "AUTO_TRADE_WATCHLIST_QUANT_V6_COMPUTE_WORKERS",
        "AUTO_TRADE_WATCHLIST_QUANT_V6_PIPELINE_MEMORY_LIMIT_MIB",
    )
    files = (
        ROOT / "docker-compose.yaml",
        ROOT / "docker-compose.dockerhub.yaml",
        ROOT / ".env.example",
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        for key in retired:
            assert key not in text, f"{path.name} still documents {key}"


def test_deploy_files_expose_extended_hours_protective_exit_control() -> None:
    """Compose must forward the extended-hours protective-exit opt-in.

    A Settings field absent from compose is silently ignored at runtime: the
    container keeps the field default regardless of what the operator sets in
    .env (recorded auto-primary-switch incident). The ``:-`` fallback must
    equal the Settings default so an unset variable ships fail-closed.
    """
    from app.config import Settings

    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub_compose = (
        ROOT / "docker-compose.dockerhub.yaml"
    ).read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    # Content assertions come first so a missing deployment entry fails as an
    # AssertionError on file content, not as a missing Settings attribute.
    assert "AUTO_TRADE_EXTENDED_HOURS_PROTECTIVE_EXITS_ENABLED=" in compose, (
        "docker-compose.yaml must forward the extended-hours protective-exit flag"
    )
    assert (
        "AUTO_TRADE_EXTENDED_HOURS_PROTECTIVE_EXITS_ENABLED="
        in dockerhub_compose
    ), "docker-compose.dockerhub.yaml must forward the extended-hours protective-exit flag"
    assert (
        "AUTO_TRADE_EXTENDED_HOURS_PROTECTIVE_EXITS_ENABLED=" in env_example
    ), ".env.example must document the extended-hours protective-exit flag"

    expected_default = (
        "AUTO_TRADE_EXTENDED_HOURS_PROTECTIVE_EXITS_ENABLED="
        "${AUTO_TRADE_EXTENDED_HOURS_PROTECTIVE_EXITS_ENABLED:-"
        f"{str(Settings().extended_hours_protective_exits_enabled).lower()}"
        "}"
    )
    assert expected_default in compose
    assert expected_default in dockerhub_compose


def test_deploy_files_expose_extended_hours_trading_control() -> None:
    """Compose must forward the extended-hours trading opt-in.

    Same incident class as the protective-exit flag: a Settings field absent
    from compose is silently ignored at runtime. The ``:-`` fallback must
    equal the Settings default (false) so an unset variable ships fail-closed,
    and the paper-account caveat must be documented in .env.example.
    """
    from app.config import Settings

    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub_compose = (
        ROOT / "docker-compose.dockerhub.yaml"
    ).read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED=" in compose, (
        "docker-compose.yaml must forward the extended-hours trading flag"
    )
    assert (
        "AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED=" in dockerhub_compose
    ), "docker-compose.dockerhub.yaml must forward the extended-hours trading flag"
    assert (
        "AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED=" in env_example
    ), ".env.example must document the extended-hours trading flag"

    expected_default = (
        "AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED="
        "${AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED:-"
        f"{str(Settings().extended_hours_trading_enabled).lower()}"
        "}"
    )
    assert expected_default in compose
    assert expected_default in dockerhub_compose
    assert Settings().extended_hours_trading_enabled is False


def test_deploy_files_expose_overnight_trading_controls() -> None:
    """Compose must forward the overnight flag and the SDK quote switch.

    Same incident class as the extended-hours flag: a Settings field absent
    from compose is silently ignored at runtime. Both variables default false
    so an unset environment ships fail-closed.
    """
    from app.config import Settings

    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub_compose = (
        ROOT / "docker-compose.dockerhub.yaml"
    ).read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    for key in (
        "AUTO_TRADE_OVERNIGHT_TRADING_ENABLED",
        "LONGPORT_ENABLE_OVERNIGHT",
    ):
        assert f"{key}=" in compose, key
        assert f"{key}=" in dockerhub_compose, key
        assert f"{key}=" in env_example, key
        expected = f"{key}=${{{key}:-false}}"
        assert expected in compose
        assert expected in dockerhub_compose

    assert Settings().overnight_trading_enabled is False


def test_deploy_files_expose_funded_margin_exception_controls() -> None:
    """Compose must forward every funded-margin exception variable.

    A Settings field absent from compose is silently ignored at runtime:
    the container keeps the field default regardless of what the operator
    sets in .env (recorded auto-primary-switch incident). The ``:-``
    fallbacks must equal the Settings defaults (off / empty / 0) so an
    unset variable ships fail-closed with the funded caps.
    """
    from app.config import Settings

    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub = (
        ROOT / "docker-compose.dockerhub.yaml"
    ).read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    keys = (
        "AUTO_TRADE_FUNDED_MARGIN_ENABLED",
        "AUTO_TRADE_FUNDED_MARGIN_ACCOUNT_FINGERPRINT",
        "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_QUANTITY",
        "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_NOTIONAL",
        "AUTO_TRADE_FUNDED_MARGIN_MAX_RISK_PER_TRADE",
    )
    for key in keys:
        assert f"{key}=" in compose, key
        assert f"{key}=" in dockerhub, key
        assert f"{key}=" in env_example, key

    expected_defaults = {
        "AUTO_TRADE_FUNDED_MARGIN_ENABLED": (
            "${AUTO_TRADE_FUNDED_MARGIN_ENABLED:-false}"
        ),
        "AUTO_TRADE_FUNDED_MARGIN_ACCOUNT_FINGERPRINT": (
            "${AUTO_TRADE_FUNDED_MARGIN_ACCOUNT_FINGERPRINT:-}"
        ),
        "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_QUANTITY": (
            "${AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_QUANTITY:-0}"
        ),
        "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_NOTIONAL": (
            "${AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_NOTIONAL:-0}"
        ),
        "AUTO_TRADE_FUNDED_MARGIN_MAX_RISK_PER_TRADE": (
            "${AUTO_TRADE_FUNDED_MARGIN_MAX_RISK_PER_TRADE:-0}"
        ),
    }
    for key, expected in expected_defaults.items():
        line = f"{key}={expected}"
        assert line in compose, line
        assert line in dockerhub, line

    # The Settings defaults must stay fail-closed so the compose fallback
    # never silently arms the exception.
    assert Settings().funded_margin_enabled is False
    assert Settings().funded_margin_account_fingerprint == ""
    assert Settings().funded_margin_max_position_quantity == 0
    assert Settings().funded_margin_max_position_notional == 0.0
    assert Settings().funded_margin_max_risk_per_trade == 0.0


def test_deploy_files_expose_ledger_epoch() -> None:
    """Compose must forward the ledger epoch or the container keeps None.

    A Settings field absent from compose is silently ignored at runtime: the
    container keeps the field default regardless of what the operator sets in
    .env (recorded auto-primary-switch incident). The ``:-`` fallback must be
    empty so an unset variable disables the filter.
    """
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    dockerhub = (ROOT / "docker-compose.dockerhub.yaml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    expected = "AUTO_TRADE_LEDGER_EPOCH=${AUTO_TRADE_LEDGER_EPOCH:-}"
    assert expected in compose
    assert expected in dockerhub
    assert "AUTO_TRADE_LEDGER_EPOCH=" in env_example
