from pathlib import Path

from pydantic import field_validator
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
    uploads_dir: str = "/data/uploads"
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
    base_domain: str = ""
    allowed_hosts: str = ""
    platform_subdomain: str = "admin"
    marketing_subdomain: str = "software"
    demo_tenant_subdomain: str = "demo"
    default_tenant_subdomain: str = ""
    reserved_subdomains: str = "admin,www,api,static"
    trust_forwarded_host: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_prefix="")

    @field_validator(
        "indicator_connected",
        "debug",
        "dev_mode",
        "print_network_enabled",
        "receipts_wip_enabled",
        "enable_credit_limit_enforcement",
        "enable_vat_calculation",
        "enable_cash_account_rules",
        "enable_invoice_pdf_emailing",
        "enable_invoice_pdf_printing",
        "smtp_use_tls",
        "smtp_use_ssl",
        "trust_forwarded_host",
        mode="before",
    )
    @classmethod
    def _coerce_boolean_values(cls, value):
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "on", "debug", "dev", "development"}:
            return True
        if normalized in {"0", "false", "no", "off", "release", "prod", "production", ""}:
            return False
        return bool(value)

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
        return "/data/uploads"

    @property
    def effective_company_logo_upload_dir(self) -> str:
        explicit = str(self.company_logo_upload_dir or "").strip()
        legacy_defaults = {
            "app/static/uploads/company",
            "/app/static/uploads/company",
            "/app/app/static/uploads/company",
        }
        if explicit and explicit not in legacy_defaults:
            return explicit
        return str((Path(self.effective_uploads_dir) / "company"))

    @property
    def effective_base_domain(self) -> str:
        return str(self.base_domain or "").strip().lower()

    @property
    def effective_platform_subdomain(self) -> str:
        return str(self.platform_subdomain or "admin").strip().lower() or "admin"

    @property
    def effective_marketing_subdomain(self) -> str:
        return str(self.marketing_subdomain or "software").strip().lower() or "software"

    @property
    def effective_demo_tenant_subdomain(self) -> str:
        explicit = str(self.demo_tenant_subdomain or "").strip().lower()
        legacy = str(self.default_tenant_subdomain or "").strip().lower()
        candidate = explicit or legacy
        if candidate in {"", "default"}:
            return "demo"
        return candidate

    @property
    def effective_default_tenant_subdomain(self) -> str:
        return self.effective_demo_tenant_subdomain

    @property
    def effective_reserved_subdomains(self) -> set[str]:
        configured = {
            item.strip().lower()
            for item in str(self.reserved_subdomains or "").split(",")
            if item.strip()
        }
        configured.add(self.effective_platform_subdomain)
        configured.add(self.effective_marketing_subdomain)
        configured.add(self.effective_demo_tenant_subdomain)
        return configured

    @property
    def effective_allowed_hosts(self) -> set[str]:
        return {
            item.strip().lower()
            for item in str(self.allowed_hosts or "").split(",")
            if item.strip()
        }

    @property
    def effective_trust_forwarded_host(self) -> bool:
        return bool(self.trust_forwarded_host)


settings = Settings()
