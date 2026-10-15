"""Cabeceras de seguridad del sitio (JHO-97).

Requisitos del issue: HSTS, CSP y cabeceras de proteccion del navegador. Se
aplican a TODA respuesta, incluidos los errores.

HSTS solo se emite cuando el sitio corre efectivamente tras TLS; emitirlo en
HTTP plano dejaria el dominio inaccesible hasta que expire el max-age.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from license_manager.config import Settings

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# La app usa plantillas server-side y un poco de JS inline de la propia UI.
_CSP_TEMPLATE = (
    "default-src {default}; "
    "script-src {default}; "
    "style-src {default} 'unsafe-inline'; "
    "img-src {default} data:; "
    "font-src {default}; "
    "connect-src {default}; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)


def build_csp(settings: Settings) -> str:
    return _CSP_TEMPLATE.format(default=settings.csp_default_src)


def build_security_headers(settings: Settings, request: Request) -> dict[str, str]:
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"

    headers = {
        "Content-Security-Policy": build_csp(settings),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }

    if settings.enable_hsts and is_https:
        headers["Strict-Transport-Security"] = f"max-age={settings.hsts_max_age_seconds}; includeSubDomains"

    return headers


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Aplica las cabeceras y evita cualquier cache de paginas autenticadas."""

    def __init__(self, app, settings: Settings):  # noqa: ANN001 - el padre define la firma
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        response: Response = await call_next(request)
        for name, value in build_security_headers(self.settings, request).items():
            response.headers[name] = value

        # Ninguna pagina con datos del negocio puede quedar en cache del navegador.
        # Un sitio privado de administracion no tiene nada que cachear: solo se
        # exceptuan los assets estaticos, que no llevan datos.
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["Pragma"] = "no-cache"

        return response
