from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    indicator_connected: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_prefix="")


settings = Settings()
