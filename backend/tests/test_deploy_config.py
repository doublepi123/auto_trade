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


def test_deploy_files_expose_llm_storage_retention_controls() -> None:
    keys = {
        "AUTO_TRADE_LLM_INTERACTION_RETENTION_DAYS",
        "AUTO_TRADE_LLM_NO_ACTION_RETENTION_DAYS",
        "AUTO_TRADE_LLM_CONTEXT_SNAPSHOT_MAX_BYTES",
        "AUTO_TRADE_LLM_STORAGE_MAINTENANCE_INTERVAL_MINUTES",
        "AUTO_TRADE_LLM_STORAGE_MAINTENANCE_BATCH_SIZE",
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


def test_deploy_files_expose_universe_and_live_regime_controls() -> None:
    keys = {
        "AUTO_TRADE_UNIVERSE_SELECTION_ENABLED",
        "AUTO_TRADE_UNIVERSE_SELECTION_APPLY_TO_WATCHLIST",
        "AUTO_TRADE_UNIVERSE_SELECTION_ENABLE_SHADOW",
        "AUTO_TRADE_UNIVERSE_SELECTION_INTERVAL_MINUTES",
        "AUTO_TRADE_UNIVERSE_SELECTION_MAX_SYMBOLS",
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
    }
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for filename in ("docker-compose.yaml", "docker-compose.dockerhub.yaml"):
        compose = (ROOT / filename).read_text(encoding="utf-8")
        for key in keys:
            assert f"{key}=" in compose
            assert f"{key}=" in env_example


def test_compose_healthchecks_use_strict_readiness_endpoint() -> None:
    for filename in ("docker-compose.yaml", "docker-compose.dockerhub.yaml"):
        compose = (ROOT / filename).read_text(encoding="utf-8")
        healthcheck = compose.split("healthcheck:", maxsplit=1)[1].split(
            "restart:", maxsplit=1
        )[0]
        assert "/api/ready" in healthcheck
        assert "/api/health" not in healthcheck


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
