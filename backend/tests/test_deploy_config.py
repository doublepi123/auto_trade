from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_docker_compose_passes_api_key_to_backend() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "AUTO_TRADE_API_KEY=" in compose


def test_docker_compose_binds_published_ports_to_localhost() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")

    assert "127.0.0.1:8000:8000" in compose
    assert "127.0.0.1:${AUTO_TRADE_FRONTEND_PORT:-8080}:80" in compose
