from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    indicator_connected: bool = False
    debug: bool = False
    dev_mode: bool = False
    print_network_enabled: bool = False
    print_template_override_dir: str = "/config/print_templates"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="")


settings = Settings()
