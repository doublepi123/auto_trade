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


def test_env_example_documents_deepseek_key() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY=" in env_example


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
