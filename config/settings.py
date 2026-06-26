from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    telegram_bot_token: str = ""
    youtube_api_key: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-latest"

    class Config:
        env_file = ".env"

settings = Settings()
