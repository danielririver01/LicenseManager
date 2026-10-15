"""Sesiones por cookie firmada, HttpOnly (JHO-98).

Decision de diseno: la cookie solo transporta el id de la sesion, firmado con
``itsdangerous`` para que no se pueda falsificar, y el estado real vive en la
tabla ``sesiones``. Asi el titular puede revocar una sesion concreta de
inmediato (cuenta desactivada, cambio de clave) sin esperar a que expire.

La cookie nunca contiene datos personales ni la contraseña. ``HttpOnly`` impide
que JavaScript la lea; ``Secure`` impide que viaje en claro y solo se configura
para produccion, nunca para desarrollo local.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from license_manager.config import Settings
from license_manager.models import User, UserSession, to_utc

_SALT = "lm-session"


class SessionManager:
    def __init__(self, settings: Settings):
        self._serializer = URLSafeSerializer(settings.session_secret, salt=_SALT)
        self._max_age = settings.session_max_age_seconds
        self._cookie_name = settings.session_cookie_name

    @property
    def cookie_name(self) -> str:
        return self._cookie_name

    @property
    def max_age(self) -> int:
        return self._max_age

    def issue(self, session: Session, user: User, user_agent: str | None = None) -> str:
        """Crea la sesion en base de datos y devuelve el valor de la cookie."""
        now = datetime.now(UTC)
        row = UserSession(
            id=uuid4().hex,
            usuario_id=user.id,
            creada_at=now,
            expira_at=now + timedelta(seconds=self._max_age),
            ultimo_uso_at=now,
            user_agent=(user_agent or "")[:255] or None,
        )
        session.add(row)
        session.flush()
        return self._serializer.dumps(row.id)

    def resolve(self, session: Session, cookie_value: str | None) -> User | None:
        """Devuelve el usuario autenticado o None. Nunca lanza."""
        if not cookie_value:
            return None
        try:
            session_id = self._serializer.loads(cookie_value)
        except BadSignature:
            return None

        row = session.get(UserSession, session_id)
        if row is None or row.cerrada_at is not None:
            return None
        if to_utc(row.expira_at) <= datetime.now(UTC):
            return None

        user = session.get(User, row.usuario_id)
        if user is None or not user.activo:
            # Cuenta desactivada: la sesion deja de servir de inmediato.
            return None

        row.ultimo_uso_at = datetime.now(UTC)
        return user

    def revoke(self, session: Session, cookie_value: str | None) -> None:
        if not cookie_value:
            return
        try:
            session_id = self._serializer.loads(cookie_value)
        except BadSignature:
            return
        row = session.get(UserSession, session_id)
        if row is not None and row.cerrada_at is None:
            row.cerrada_at = datetime.now(UTC)

    def revoke_all_for_user(self, session: Session, user_id: str) -> int:
        now = datetime.now(UTC)
        rows = (
            session.execute(
                select(UserSession).where(UserSession.usuario_id == user_id, UserSession.cerrada_at.is_(None))
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.cerrada_at = now
        return len(rows)

    def purge_expired(self, session: Session) -> int:
        """Limpieza opcional de sesiones vencidas."""
        rows = (
            session.execute(select(UserSession).where(UserSession.expira_at < datetime.now(UTC)))
            .scalars()
            .all()
        )
        for row in rows:
            session.delete(row)
        return len(rows)
