"""Configuracion central de License Manager.

Todas las variables de entorno usan el prefijo ``LM_``. La llave privada ECDSA
nunca se persiste aqui: solo se lee del entorno del servidor (JHO-97).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LM_",
        extra="ignore",
    )

    app_name: str = "License Manager Frubber"
    environment: str = "development"
    debug: bool = False

    # Base de datos. En produccion: postgresql+psycopg://...
    database_url: str = "sqlite:///./license_manager.db"

    # --- Custodia criptografica (JHO-97) -------------------------------------
    # La llave privada ECDSA P-256 se entrega por entorno, nunca por codigo.
    ecdsa_private_key_pem: str | None = None
    ecdsa_private_key_file: str | None = None
    # keyId que el POS reconoce. Debe coincidir con LicensePublicKeys.CurrentKeyId.
    expected_key_id: str = "lic-v1-2026-09-29"

    # --- Sesiones (JHO-98) ---------------------------------------------------
    session_secret: str = "development-only-secret-change-me"
    session_cookie_name: str = "lm_session"
    session_max_age_seconds: int = 12 * 60 * 60
    # En produccion siempre True: la cookie solo viaja por HTTPS.
    cookie_secure: bool = False
    cookie_samesite: str = "lax"

    # --- Cabeceras de seguridad (JHO-97) -------------------------------------
    # Solo emite HSTS cuando el sitio corre tras TLS.
    enable_hsts: bool = False
    hsts_max_age_seconds: int = 31536000
    csp_default_src: str = "'self'"
    trusted_hosts: str = "*"

    # --- Negocio (doc 04 v2.0 y LM-RF-008) -----------------------------------
    tarifa_licencia_cop: int = 450_000
    dias_prueba: int = 30
    dias_gracia: int = 3
    # Dia 31 cobro pendiente + gracia hasta el dia 34 vencida.

    # --- Respaldos automatizados (JHO-106) -----------------------------------
    # Valores por defecto de la herramienta de respaldo; las banderas de la
    # linea de comandos los sobrescriben.
    backup_dir: str = "./backups"
    backup_keep: int = 14
    backup_upload: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Solo para pruebas que cambian variables de entorno entre casos."""
    get_settings.cache_clear()
