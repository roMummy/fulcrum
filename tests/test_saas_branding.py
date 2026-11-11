import asyncio
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "saas-branding"

# 品牌测试会导入应用入口；必须先把全局 Settings 固定到测试数据库，避免误连开发数据。
os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'test.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.settings import Settings
from app.main import app, healthz


def test_application_uses_saas_branding() -> None:
    """应用元数据和健康检查必须对外使用新的 SaaS 服务标识。"""

    assert Settings(_env_file=None).app_name == "Fulcrum"
    assert app.title == "Fulcrum"
    assert asyncio.run(healthz()) == {"ok": True, "service": "fulcrum"}


def test_compose_uses_isolated_saas_runtime() -> None:
    """新项目的容器、端口、数据库和卷不能与旧服务发生名称冲突。"""

    compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "container_name: multi-bot-saas-app" in compose_text
    assert "container_name: multi-bot-saas-mariadb" in compose_text
    assert '"8280:8280"' in compose_text
    assert "MARIADB_DATABASE: multi_bot_saas" in compose_text
    assert "multi_bot_saas_data:" in compose_text
    assert "rootpass" not in compose_text


def test_test_client_dependency_is_declared() -> None:
    """全新虚拟环境也应能直接运行 FastAPI TestClient 测试。"""

    development_requirements = (PROJECT_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in development_requirements
    assert "pytest" in development_requirements
    assert "httpx2" in development_requirements
