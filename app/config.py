from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    indicator_connected: bool = False
    debug: bool = False
    dev_mode: bool = False
    print_network_enabled: bool = False
    print_template_override_dir: str = "/config/print_templates"
    media_root: str = "app/media"
    company_logo_upload_dir: str = "app/static/uploads/company"
    app_public_base_url: str = ""
    receipts_wip_enabled: bool = False
    # Future hook flags (default OFF): ticket completion / invoice generation will
    # reference these when enforcement rules are implemented.
    enable_credit_limit_enforcement: bool = False
    enable_vat_calculation: bool = False
    enable_cash_account_rules: bool = False
    enable_invoice_pdf_emailing: bool = False
    enable_invoice_pdf_printing: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_prefix="")


settings = Settings()
