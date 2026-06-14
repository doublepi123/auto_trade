from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_docker_compose_passes_api_key_to_backend() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "AUTO_TRADE_API_KEY=" in compose


def test_docker_compose_passes_api_key_to_frontend_build() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "VITE_AUTO_TRADE_API_KEY:" in compose
    assert "${AUTO_TRADE_API_KEY" in compose


def test_frontend_dockerfile_accepts_api_key_build_arg() -> None:
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG VITE_AUTO_TRADE_API_KEY" in dockerfile


def test_frontend_docker_entrypoint_writes_runtime_api_key() -> None:
    entrypoint = (ROOT / "frontend" / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "runtime-config.js" in entrypoint
    assert "AUTO_TRADE_API_KEY" in entrypoint


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


def test_env_example_documents_deepseek_key() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY=" in env_example


def test_docker_compose_publishes_frontend_publicly_and_keeps_backend_private() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    backend_block = compose.split("\n  frontend:", maxsplit=1)[0]

    assert "\n    ports:" not in backend_block
    assert "127.0.0.1:8000:8000" not in compose
    # BIND is env-driven; default is 0.0.0.0 so the rendered value exposes the UI on
    # the LAN. Operators can override AUTO_TRADE_FRONTEND_BIND=127.0.0.1 for
    # loopback-only access.
    assert "${AUTO_TRADE_FRONTEND_BIND:-0.0.0.0}:${AUTO_TRADE_FRONTEND_PORT:-8080}:80" in compose


def test_frontend_healthcheck_uses_ipv4_loopback() -> None:
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "http://127.0.0.1/" in dockerfile
    assert "http://localhost/" not in dockerfile
