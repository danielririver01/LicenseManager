"""Gestion de cuentas de usuario (JHO-99, UI-09).

Reglas de negocio que este modulo materializa, todas tomadas de UI-09:

* Solo los titulares (Owner y Co-Titular) gestionan cuentas. El bloqueo 403 es
  estricto y se aplica en ``deps.require_titular``.
* La cuenta del Titular Principal es inmutable: no se desactiva ni se borra.
  Solo puede actualizar su propia contrasena, y eso exige la clave anterior.
* El Co-Titular se crea con acceso total (rol ``coowner``).
* Desactivar es reversible y NO borra: la fila se conserva con ``activo=False``
  y ``desactivado_at``. Asi el historial de auditoria y las licencias emitidas
  por ese operador siguen atribuidas a su nombre para siempre.
* Desactivar revoca de inmediato todas las sesiones abiertas del usuario.
* Un usuario no se puede desactivar a si mismo ni desactivar al Titular.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from license_manager.models import License, Role, User, utcnow
from license_manager.security.passwords import (
    PasswordTooWeak,
    hash_password,
    validate_password_strength,
    verify_password,
)
from license_manager.security.sessions import SessionManager


class UserManagementError(ValueError):
    """Rechazo de negocio con un mensaje pensado para mostrarse en pantalla."""


@dataclass(frozen=True)
class OperatorRow:
    """Fila preparada para la tabla de UI-09."""

    id: str
    nombre: str
    email: str
    rol: str
    activo: bool
    licencias_emitidas: int
    creado_at: object
    es_titular: bool
    puede_desactivarse: bool


_ROL_ETIQUETA = {
    Role.OWNER.value: "Titular Principal",
    Role.COOWNER.value: "Co-Titular",
    Role.ADMIN.value: "Administrador",
}


def rol_etiqueta(rol: str) -> str:
    return _ROL_ETIQUETA.get(rol, rol)


def list_operators(db: Session) -> list[OperatorRow]:
    """Todos los usuarios con su conteo de licencias emitidas.

    Se hace LEFT JOIN con ``licencias`` para que un titular sin emisiones
    aparezca con 0 y no desaparezca de la lista.
    """
    conteos = (
        select(License.creado_por_id, func.count(License.id).label("total"))
        .group_by(License.creado_por_id)
        .subquery()
    )
    rows = db.execute(
        select(User, func.coalesce(conteos.c.total, 0)).outerjoin(
            conteos, conteos.c.creado_por_id == User.id
        )
    ).all()

    resultado: list[OperatorRow] = []
    for user, total in rows:
        resultado.append(
            OperatorRow(
                id=user.id,
                nombre=user.nombre_completo,
                email=user.email,
                rol=user.rol,
                activo=user.activo,
                licencias_emitidas=int(total),
                creado_at=user.creado_at,
                es_titular=Role(user.rol).is_titular,
                # Titulares inmutables y la propia cuenta nunca se desactivan
                # desde esta pantalla.
                puede_desactivarse=Role(user.rol) != Role.OWNER,
            )
        )

    # El Titular Principal primero, despues Co-Titular y por ultimo admins.
    orden = {Role.OWNER.value: 0, Role.COOWNER.value: 1, Role.ADMIN.value: 2}
    resultado.sort(key=lambda r: (orden.get(r.rol, 9), r.nombre.lower()))
    return resultado


def count_active_titulares(db: Session, excluding_user_id: str | None = None) -> int:
    """Cuantos titulares activos quedan. Protege contra dejar el sistema sin dueno."""
    query = select(func.count(User.id)).where(
        User.rol.in_([Role.OWNER.value, Role.COOWNER.value]), User.activo.is_(True)
    )
    if excluding_user_id:
        query = query.where(User.id != excluding_user_id)
    return int(db.execute(query).scalar_one())


def create_user(
    db: Session,
    *,
    actor: User,
    nombre: str,
    email: str,
    password: str,
    rol: str,
) -> User:
    """Alta de Co-Titular o Administrador Delegado."""
    if rol not in (Role.COOWNER.value, Role.ADMIN.value):
        raise UserManagementError("Rol no valido: solo se puede crear Co-Titular o Administrador.")

    # Regla de escalada de privilegios: un Co-Titular no puede fabricar otro
    # Co-Titular. Si lo permitiera, un socio podria multiplicarse a si mismo y
    # anadir copropietarios que el Titular Principal nunca autorizo. El alta de
    # socio es exclusiva del Titular Principal.
    if rol == Role.COOWNER.value and Role(actor.rol) != Role.OWNER:
        raise UserManagementError(
            "Solo el Titular Principal puede dar de alta a un Co-Titular."
        )

    nombre = (nombre or "").strip()
    email = (email or "").strip().lower()

    if not nombre:
        raise UserManagementError("El nombre completo es obligatorio.")
    if not email:
        raise UserManagementError("El correo de acceso es obligatorio.")

    try:
        validate_password_strength(password)
    except PasswordTooWeak as exc:
        raise UserManagementError(str(exc)) from exc

    existente = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existente is not None:
        # Mismo criterio que el login: no se revela si el correo ya estaba en uso
        # ante cualquiera que no sea el propio titular, pero aqui ya es titular.
        raise UserManagementError("Ya existe una cuenta con ese correo de acceso.")

    user = User(
        nombre_completo=nombre,
        email=email,
        password_hash=hash_password(password),
        rol=rol,
        activo=True,
        creado_por_id=actor.id,
    )
    db.add(user)
    db.flush()
    return user


def set_user_active(
    db: Session,
    sessions: SessionManager,
    *,
    actor: User,
    target: User,
    active: bool,
) -> int:
    """Activa o desactiva una cuenta y revoca sus sesiones al instante.

    Devuelve el numero de sesiones revocadas para poder reportarlo.
    """
    if Role(target.rol) == Role.OWNER:
        raise UserManagementError("La cuenta del Titular Principal es inmutable y no se puede desactivar.")

    if actor.id == target.id and not active:
        raise UserManagementError("No puede desactivar su propia cuenta.")

    if (
        not active
        and Role(target.rol).is_titular
        and count_active_titulares(db, excluding_user_id=target.id) == 0
    ):
        raise UserManagementError(
            "No puede desactivar al ultimo titular activo: quedaria sin dueno el sistema."
        )

    revocadas = 0
    if not active:
        revocadas = sessions.revoke_all_for_user(db, target.id)

    target.activo = active
    target.desactivado_at = None if active else utcnow()
    db.flush()
    return revocadas


def reset_password(
    db: Session,
    sessions: SessionManager,
    *,
    actor: User,
    target: User,
    new_password: str,
) -> int:
    """Restablece la clave de un operador y cierra sus sesiones abiertas.

    El actor nunca puede restablecer la clave del Titular Principal ni la
    propia: cambiar la clave propia exige verificar la anterior (UI-09).
    """
    if Role(target.rol) == Role.OWNER:
        raise UserManagementError(
            "La contrasena del Titular Principal solo puede cambiarla el propio Titular, "
            "verificando su contrasena actual."
        )
    if actor.id == target.id:
        raise UserManagementError("Para cambiar su propia contrasena use el panel de seguridad.")

    try:
        validate_password_strength(new_password)
    except PasswordTooWeak as exc:
        raise UserManagementError(str(exc)) from exc

    target.password_hash = hash_password(new_password)
    revocadas = sessions.revoke_all_for_user(db, target.id)
    db.flush()
    return revocadas


def change_own_password(
    db: Session,
    sessions: SessionManager,
    *,
    user: User,
    current_password: str,
    new_password: str,
    new_password_confirm: str,
    keep_cookie: str | None = None,
) -> str | None:
    """Cambio de clave del propio usuario, siempre verificando la clave anterior.

    Devuelve la cookie nueva si el llamador quiere mantener la sesion actual
    viva, o None si se deben cerrar todas. Todas las demas sesiones se cierran.
    """
    if not verify_password(user.password_hash, current_password):
        raise UserManagementError("La contrasena actual no es correcta.")
    if new_password != new_password_confirm:
        raise UserManagementError("La confirmacion de la nueva contrasena no coincide.")
    if new_password == current_password:
        raise UserManagementError("La nueva contrasena debe ser distinta a la actual.")
    try:
        validate_password_strength(new_password)
    except PasswordTooWeak as exc:
        raise UserManagementError(str(exc)) from exc

    user.password_hash = hash_password(new_password)
    # Se cierra TODO y se emite una sesion nueva: asi cualquier sesion abierta
    # en otro dispositivo muere al instante, que es lo que exige UI-09.
    sessions.revoke_all_for_user(db, user.id)
    nueva = sessions.issue(db, user)
    db.flush()
    return nueva
