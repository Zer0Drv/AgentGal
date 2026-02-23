"""FastAPI 配置"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    api_prefix: str = "/api/v1"
    app_name: str = "AgentGal FastAPI"
    app_version: str = "0.1.0"
    database_url: str = "postgresql+asyncpg://seki:postgres@localhost:5432/agentgal"

    # 控制角色并发上限，避免瞬时过载
    max_concurrent_agents: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
