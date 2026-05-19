from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_docker_compose_passes_api_key_to_backend() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "AUTO_TRADE_API_KEY=" in compose


def test_docker_compose_passes_deepseek_key_to_backend() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY=" in compose


def test_env_example_documents_deepseek_key() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY=" in env_example


def test_docker_compose_binds_published_ports_to_localhost() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "127.0.0.1:8000:8000" in compose
    assert "127.0.0.1:${AUTO_TRADE_FRONTEND_PORT:-8080}:80" in compose


def test_frontend_healthcheck_uses_ipv4_loopback() -> None:
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "http://127.0.0.1/" in dockerfile
    assert "http://localhost/" not in dockerfile
