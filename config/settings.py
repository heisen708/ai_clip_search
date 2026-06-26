from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    telegram_bot_token: str = ""
    youtube_api_key: str = ""
    openrouter_api_key: str = ""

    class Config:
        env_file = ".env"

settings = Settings()
