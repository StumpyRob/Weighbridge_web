from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    secret_key: str = ""
    app_secret_key: str = ""
    indicator_connected: bool = False
    debug: bool = False
    dev_mode: bool = False
    print_network_enabled: bool = False
    print_template_override_dir: str = "/config/print_templates"
    media_root: str = "app/media"
    uploads_dir: str = "app/static/uploads"
    company_logo_upload_dir: str = ""
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

    @property
    def effective_secret_key(self) -> str:
        candidate = str(self.app_secret_key or "").strip()
        if candidate:
            return candidate
        return str(self.secret_key or "").strip()

    @property
    def effective_uploads_dir(self) -> str:
        candidate = str(self.uploads_dir or "").strip()
        if candidate:
            return candidate
        return "app/static/uploads"

    @property
    def effective_company_logo_upload_dir(self) -> str:
        explicit = str(self.company_logo_upload_dir or "").strip()
        default_candidates = {
            "app/static/uploads/company",
            "/app/static/uploads/company",
        }
        if explicit and explicit not in default_candidates:
            return explicit
        return str((Path(self.effective_uploads_dir) / "company"))


settings = Settings()
