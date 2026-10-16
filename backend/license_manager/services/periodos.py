"""Periodos mensuales compartidos por UI-07B (cierres) y UI-11 (liquidaciones).

Los dos modulos financieros de JHO-103 trabajan por MES CALENDARIO y el
operador nunca digita una fecha: elige el mes con las flechas de la barra o
recibe el mes en curso. Aqui vive toda la aritmetica de mes para que el
cierre y la liquidacion trazen exactamente el mismo limite.

Convencion de rango: un mes se representa por su primer dia y se consulta como
``inicio <= periodo < fin``, donde ``fin`` es el primer dia del mes siguiente.
Asi un movimiento pertenece a UN solo mes y no hay que discutir si el ultimo
dia se cuenta o no.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, date

from license_manager.models import utcnow

MESES = (
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)


class PeriodoError(ValueError):
    """Periodo mensual invalido, con un mensaje pensado para mostrarse."""


def mes_actual() -> date:
    """Primer dia del mes en curso. La hora la manda el servidor."""
    hoy = utcnow().astimezone(UTC).date()
    return date(hoy.year, hoy.month, 1)


def etiqueta_periodo(periodo: date) -> str:
    """``Octubre 2026`` a partir del primer dia del mes."""
    return f"{MESES[periodo.month - 1]} {periodo.year}"


def clave_periodo(periodo: date) -> str:
    """``2026-10``: la forma que viaja en la cadena de consulta y en el CSV."""
    return f"{periodo.year:04d}-{periodo.month:02d}"


def parse_periodo(texto: str) -> date:
    """Convierte ``YYYY-MM`` al primer dia del mes."""
    limpio = (texto or "").strip()
    partes = limpio.split("-")
    if len(partes) != 2:
        raise PeriodoError("El periodo debe indicarse como AAAA-MM, por ejemplo 2026-10.")
    try:
        anio = int(partes[0])
        mes = int(partes[1])
    except ValueError as exc:
        raise PeriodoError("El periodo debe indicarse como AAAA-MM, por ejemplo 2026-10.") from exc
    if len(partes[0]) != 4 or not 1 <= mes <= 12:
        raise PeriodoError("Ese mes no existe. Usa un periodo entre 01 y 12, como 2026-10.")
    if not 2000 <= anio <= 2099:
        raise PeriodoError("Ese ano esta fuera del rango admitido (2000 a 2099).")
    return date(anio, mes, 1)


def periodo_anterior(periodo: date) -> date:
    if periodo.month == 1:
        return date(periodo.year - 1, 12, 1)
    return date(periodo.year, periodo.month - 1, 1)


def periodo_siguiente(periodo: date) -> date:
    if periodo.month == 12:
        return date(periodo.year + 1, 1, 1)
    return date(periodo.year, periodo.month + 1, 1)


def fin_del_periodo(periodo: date) -> date:
    """Primer dia DESPUES del mes: limite exclusivo de las consultas."""
    return periodo_siguiente(periodo)


def dias_del_mes(periodo: date) -> int:
    return monthrange(periodo.year, periodo.month)[1]


def dias_transcurridos(periodo: date, hoy: date | None = None) -> int:
    """Dias del mes que ya pasaron: hasta hoy si es el mes en curso, si no el mes completo.

    El promedio diario del cierre usa este divisor para no hundir el promedio
    de un mes que todavia no termino.
    """
    dia_hoy = hoy or utcnow().astimezone(UTC).date()
    if (dia_hoy.year, dia_hoy.month) == (periodo.year, periodo.month):
        return max(1, dia_hoy.day)
    return dias_del_mes(periodo)
