"""Recarga de paquetes con pasarela de pago (JHO-108, Mercado Pago).

Reglas de seguridad que este modulo materializa (el "de forma segura" del
issue). Todas estan probadas con un simulador de la pasarela:

1. **La firma del webhook se valida siempre** (HMAC-SHA256 del secreto de la
   aplicacion sobre la plantilla oficial
   ``id:[data.id];request-id:[x-request-id];ts:[ts];``). Sin firma valida no se
   procesa nada: es lo que impide que cualquiera invente un "pago aprobado".
2. **La notificacion NO dice cuanto se pago**: solo trae ``data.id``. El estado
   y el importe se piden a la pasarela con el token del servidor. Nunca se
   acredita con lo que venga en el cuerpo del aviso.
3. **Se comprueba el importe exacto** del paquete y que el pago no se haya usado
   antes (unicos ``mp_payment_id``/``mp_preference_id``).
4. **Idempotencia**: la pasarela reintenta el aviso; acreditar dos veces es
   imposible por estado + indices unicos.
5. **El token de acceso es secreto del servidor**: vive en el entorno, como la
   llave privada, y nunca se expone ni se registra.
6. **Modo**: en produccion no se acredita un pago de pruebas, ni al reves.
7. Todo queda en la auditoria encadenada.
8. Si no esta configurada, la funcion se oculta y el sitio sigue igual.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from license_manager.config import Settings
from license_manager.models import (
    EstadoPago,
    PagoMayorista,
    Technician,
    utcnow,
)
from license_manager.services.directory import format_cop
from license_manager.services.mayorista import (
    TIPOS_PAQUETE,
    MayoristaError,
    registrar_paquete,
    tipo_paquete_por,
)

NOMBRE_PASARELA = "Mercado Pago"


class PagoError(ValueError):
    """Rechazo de pago con un mensaje pensado para mostrarse o registrarse."""


# --------------------------------------------------------------------------- #
# Configuracion
# --------------------------------------------------------------------------- #


def pasarela_configurada(settings: Settings) -> bool:
    """Sin token y secreto no se ofrece la recarga: queda el registro manual."""
    return bool(settings.mp_access_token and settings.mp_webhook_secret)


def base_publica(settings: Settings) -> str:
    return (settings.mp_base_url or "").rstrip("/")


# --------------------------------------------------------------------------- #
# Firma del webhook
# --------------------------------------------------------------------------- #


def _partes_firma(x_signature: str | None) -> tuple[str | None, str | None]:
    """Separa ``ts`` y ``v1`` del encabezado. None si viene malformado."""
    if not x_signature:
        return None, None
    valores: dict[str, str] = {}
    for trozo in x_signature.split(","):
        if "=" in trozo:
            clave, _, valor = trozo.partition("=")
            valores[clave.strip()] = valor.strip()
    return valores.get("ts"), valores.get("v1")


def calcular_firma(
    *, data_id: str, x_request_id: str, ts: str, secret: str
) -> str:
    """HMAC-SHA256 en hexadecimal de la plantilla oficial de Mercado Pago.

    Los valores ausentes se omiten, tal como indica la documentacion. El
    ``data.id`` va en minusculas cuando es alfanumerico.
    """
    plantilla = ""
    if data_id:
        plantilla += f"id:{data_id.lower()};"
    if x_request_id:
        plantilla += f"request-id:{x_request_id};"
    if ts:
        plantilla += f"ts:{ts};"
    return hmac.new(
        secret.encode("utf-8"), plantilla.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def firma_valida(
    *,
    x_signature: str | None,
    x_request_id: str | None,
    data_id: str | None,
    secret: str | None,
) -> bool:
    """True solo si la firma corresponde. Comparacion en tiempo constante."""
    if not secret:
        return False
    ts, recibida = _partes_firma(x_signature)
    if not ts or not recibida or not data_id:
        return False
    esperada = calcular_firma(
        data_id=str(data_id),
        x_request_id=x_request_id or "",
        ts=ts,
        secret=secret,
    )
    return hmac.compare_digest(esperada, recibida)


# --------------------------------------------------------------------------- #
# Cliente de la pasarela
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RespuestaPasarela:
    ok: bool
    datos: dict
    error: str | None = None


class MercadoPago:
    """Cliente minimo de Checkout Pro. Se puede sustituir por un simulador."""

    def __init__(self, *, token: str, api_url: str, timeout: int = 15) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout

    def _pedir(self, ruta: str, *, metodo: str = "GET", cuerpo: dict | None = None) -> RespuestaPasarela:
        datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
        peticion = Request(  # noqa: S310 - URL de la pasarela, no del usuario
            f"{self._api_url}{ruta}",
            data=datos,
            method=metodo,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(peticion, timeout=self._timeout) as respuesta:  # noqa: S310
                return RespuestaPasarela(True, json.loads(respuesta.read().decode("utf-8")))
        except HTTPError as exc:
            detalle = exc.read().decode("utf-8", "replace")[:300]
            return RespuestaPasarela(False, {}, f"HTTP {exc.code}: {detalle}")
        except (URLError, TimeoutError, ValueError) as exc:
            # Nunca se registra el token: el error no lo incluye.
            return RespuestaPasarela(False, {}, f"sin respuesta de la pasarela ({exc.__class__.__name__})")

    def crear_preferencia(self, payload: dict) -> RespuestaPasarela:
        return self._pedir("/checkout/preferences", metodo="POST", cuerpo=payload)

    def obtener_pago(self, payment_id: str) -> RespuestaPasarela:
        return self._pedir(f"/v1/payments/{payment_id}")

    def buscar_pagos(self, referencia: str) -> RespuestaPasarela:
        """Conciliacion: pagos de una orden, por si el aviso no llego."""
        return self._pedir(f"/v1/payments/search?external_reference={referencia}")


# --------------------------------------------------------------------------- #
# Ordenes
# --------------------------------------------------------------------------- #


def crear_orden(db: Session, *, tecnico: Technician, tipo: str) -> PagoMayorista:
    """Crea la orden pendiente y devuelve su referencia a la pasarela."""
    seleccion = tipo_paquete_por(tipo)
    if seleccion is None:
        raise PagoError("Elige un paquete valido para recargar.")
    if not tecnico.activo:
        raise PagoError(
            f"La ficha de {tecnico.nombre} esta desactivada: no se puede recargar."
        )

    orden = PagoMayorista(
        tecnico_id=tecnico.id,
        tipo_paquete=seleccion.valor,
        monto_cop=seleccion.valor_total_cop,
        estado=EstadoPago.PENDING.value,
    )
    db.add(orden)
    db.flush()
    return orden


def payload_preferencia(orden: PagoMayorista, *, base_url: str) -> dict:
    """Cuerpo de la preferencia de Checkout Pro.

    ``external_reference`` es obligatorio: la pasarela no respeta la cabecera de
    idempotencia en este endpoint, asi que la referencia es lo unico que evita
    duplicados si hay que reintentar.
    """
    seleccion = tipo_paquete_por(orden.tipo_paquete)
    etiqueta = seleccion.etiqueta if seleccion else orden.tipo_paquete
    base = base_url.rstrip("/")
    return {
        "items": [
            {
                "title": f"{etiqueta} · Frubber POS",
                "quantity": 1,
                "unit_price": float(orden.monto_cop),
                "currency_id": "COP",
            }
        ],
        "external_reference": orden.referencia,
        "statement_descriptor": "FRUBBER",
        "back_urls": {
            "success": f"{base}/mayorista/recarga/{orden.id}",
            "pending": f"{base}/mayorista/recarga/{orden.id}",
            "failure": f"{base}/mayorista/recarga/{orden.id}",
        },
        "auto_return": "approved",
        # Solo webhooks (y no IPN) al confirmarse el pago.
        "notification_url": f"{base}/webhooks/mercadopago?source_news=webhooks",
        "metadata": {"orden_id": orden.id, "tecnico_id": orden.tecnico_id},
    }


def orden_por_referencia(db: Session, referencia: str | None) -> PagoMayorista | None:
    if not referencia:
        return None
    return db.execute(
        select(PagoMayorista).where(PagoMayorista.referencia == referencia)
    ).scalar_one_or_none()


def orden_de(db: Session, orden_id: str) -> PagoMayorista | None:
    return db.get(PagoMayorista, orden_id) if orden_id else None


def ordenes_de(db: Session, tecnico_id: str, limite: int = 10) -> list[PagoMayorista]:
    return list(
        db.execute(
            select(PagoMayorista)
            .where(PagoMayorista.tecnico_id == tecnico_id)
            .order_by(PagoMayorista.creado_at.desc())
            .limit(limite)
        ).scalars()
    )


def etiqueta_estado(estado: str) -> tuple[str, str]:
    """(texto, tono del badge) del estado de una orden."""
    tabla = {
        EstadoPago.PENDING.value: ("Pendiente de pago", "ambar"),
        EstadoPago.APPROVED.value: ("Pagado · cupos acreditados", "verde"),
        EstadoPago.REJECTED.value: ("Rechazado", "rojo"),
        EstadoPago.CANCELED.value: ("Cancelado", "gris"),
        EstadoPago.REVIEW.value: ("Pagado · por revisar", "rojo"),
    }
    return tabla.get(estado, (estado, "gris"))


def monto_entero(valor: object) -> int:
    """El COP es entero: la pasarela manda decimales y se redondea al peso."""
    try:
        return int(round(float(valor)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1


def _modo_coincide(pago: dict, *, produccion: bool) -> bool:
    """live_mode false es un pago de pruebas: no puede acreditarse en produccion."""
    live = bool(pago.get("live_mode"))
    return live is produccion


# --------------------------------------------------------------------------- #
# Confirmacion (acreditacion)
# --------------------------------------------------------------------------- #


def confirmar_pago(
    db: Session, *, orden: PagoMayorista, pago: dict, produccion: bool
) -> PagoMayorista:
    """Acredita el paquete a partir del pago CONSULTADO a la pasarela.

    Idempotente: si la orden ya esta aprobada no hace nada. Todos los rechazos
    dejan la orden con su motivo, nunca acreditan de menos ni de mas.
    """
    if orden.estado == EstadoPago.APPROVED.value:
        return orden

    estado = str(pago.get("status") or "")
    orden.detalle_pasarela = str(pago.get("status_detail") or "")[:64] or None
    orden.mp_payment_id = str(pago.get("id") or "")[:64] or orden.mp_payment_id

    if estado in {"rejected"}:
        orden.estado = EstadoPago.REJECTED.value
        db.flush()
        return orden
    if estado in {"cancelled", "refunded", "charged_back"}:
        orden.estado = EstadoPago.CANCELED.value
        orden.nota = f"Pago {estado} en la pasarela."
        db.flush()
        return orden
    if estado != "approved":
        # pending / in_process: la orden sigue pendiente y se espera el aviso.
        db.flush()
        return orden

    if not _modo_coincide(pago, produccion=produccion):
        raise PagoError(
            "El pago no es del modo configurado (pruebas contra produccion): "
            "no se acredita. Revisa LM_MP_PRODUCTION y el token."
        )

    importe = monto_entero(pago.get("transaction_amount"))
    if importe != orden.monto_cop:
        raise PagoError(
            f"El importe pagado ({format_cop(max(importe, 0))}) no coincide con el "
            f"del paquete ({format_cop(orden.monto_cop)}): no se acredita."
        )

    try:
        paquete, _ingreso = registrar_paquete(
            db,
            actor_id=None,
            tecnico_id=orden.tecnico_id,
            tipo=orden.tipo_paquete,
            metodo_pago="otro",
            comprobante=f"{NOMBRE_PASARELA} pago #{orden.mp_payment_id}",
        )
    except MayoristaError as exc:
        # El dinero llego pero no se puede acreditar solo (p.ej. ficha
        # desactivada). Se deja para que el Titular lo resuelva: nunca se
        # pierde el rastro del pago.
        orden.estado = EstadoPago.REVIEW.value
        orden.nota = f"Pago recibido pero no acreditado automaticamente: {exc}"
        db.flush()
        return orden

    orden.estado = EstadoPago.APPROVED.value
    orden.paquete_id = paquete.id
    orden.pagado_at = utcnow()
    orden.nota = f"{NOMBRE_PASARELA} pago #{orden.mp_payment_id}"
    db.flush()
    return orden


# --------------------------------------------------------------------------- #
# Webhook
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResultadoWebhook:
    """Lo que se registra y lo que se responde a la pasarela."""

    aceptado: bool
    etiqueta: str
    detalle: str
    orden: PagoMayorista | None = None


def procesar_webhook(
    db: Session,
    *,
    data_id: str | None,
    x_signature: str | None,
    x_request_id: str | None,
    cliente: MercadoPago,
    settings: Settings,
) -> ResultadoWebhook:
    """Valida la firma, consulta el pago y acredita si corresponde.

    Devuelve un resultado en vez de lanzar: la ruta responde 200 a todo lo que
    YA se decidio (para que la pasarela deje de reintentar) y 401 solo cuando la
    firma no es valida.
    """
    if not pasarela_configurada(settings):
        return ResultadoWebhook(False, "sin_configurar", "La pasarela no esta configurada.")

    if not firma_valida(
        x_signature=x_signature,
        x_request_id=x_request_id,
        data_id=data_id,
        secret=settings.mp_webhook_secret,
    ):
        return ResultadoWebhook(False, "firma_invalida", "La firma de la notificacion no es valida.")

    if not data_id:
        return ResultadoWebhook(True, "sin_id", "Notificacion sin data.id: nada que hacer.")

    respuesta = cliente.obtener_pago(str(data_id))
    if not respuesta.ok:
        # Fallo temporal de la pasarela: se responde aceptado para que reintente.
        return ResultadoWebhook(False, "consulta_fallida", respuesta.error or "sin detalle")

    pago = respuesta.datos
    orden = orden_por_referencia(db, str(pago.get("external_reference") or ""))
    if orden is None:
        return ResultadoWebhook(
            True,
            "orden_desconocida",
            f"Ningun pedido con la referencia {pago.get('external_reference')!r}.",
        )

    # Un pago ya usado en OTRA orden jamas se reutiliza.
    pagado = str(pago.get("id") or "")
    if pagado and orden.mp_payment_id and orden.mp_payment_id != pagado:
        return ResultadoWebhook(
            True, "pago_repetido", f"La orden {orden.referencia} ya tiene otro pago registrado."
        )
    if pagado:
        ya_usado = db.execute(
            select(PagoMayorista).where(
                PagoMayorista.mp_payment_id == pagado, PagoMayorista.id != orden.id
            )
        ).scalar_one_or_none()
        if ya_usado is not None:
            return ResultadoWebhook(
                True, "pago_repetido", f"El pago #{pagado} ya acredito otra orden."
            )

    try:
        confirmar_pago(
            db, orden=orden, pago=pago, produccion=settings.mp_production
        )
    except PagoError as exc:
        db.rollback()
        return ResultadoWebhook(True, "rechazado", str(exc), orden)

    return ResultadoWebhook(
        True, f"orden_{orden.estado}", f"Orden {orden.referencia}: {orden.estado}.", orden
    )


def conciliar_orden(
    db: Session, *, orden: PagoMayorista, cliente: MercadoPago, settings: Settings
) -> ResultadoWebhook:
    """Consulta los pagos de la orden cuando el aviso no llego (red de seguridad).

    La usa la pantalla de retorno: si el pago se aprobo y el webhook se perdio,
    el tecnico igual queda acreditado.
    """
    respuesta = cliente.buscar_pagos(orden.referencia)
    if not respuesta.ok:
        return ResultadoWebhook(False, "consulta_fallida", respuesta.error or "sin detalle")

    resultados = respuesta.datos.get("results") or []
    aprobados = [pago for pago in resultados if pago.get("status") == "approved"]
    if not aprobados:
        return ResultadoWebhook(True, "sin_pago_aprobado", "Todavia no hay un pago aprobado.")

    aprobado = sorted(aprobados, key=lambda p: str(p.get("date_approved") or ""))[-1]
    try:
        confirmar_pago(db, orden=orden, pago=aprobado, produccion=settings.mp_production)
    except PagoError as exc:
        db.rollback()
        return ResultadoWebhook(True, "rechazado", str(exc), orden)

    return ResultadoWebhook(
        True, f"orden_{orden.estado}", f"Conciliado: {orden.estado}.", orden
    )


__all__ = [
    "NOMBRE_PASARELA",
    "EstadoPago",
    "MercadoPago",
    "PagoError",
    "RespuestaPasarela",
    "ResultadoWebhook",
    "base_publica",
    "calcular_firma",
    "confirmar_pago",
    "conciliar_orden",
    "crear_orden",
    "etiqueta_estado",
    "firma_valida",
    "monto_entero",
    "orden_de",
    "orden_por_referencia",
    "ordenes_de",
    "pasarela_configurada",
    "payload_preferencia",
    "procesar_webhook",
    "TIPOS_PAQUETE",
]
