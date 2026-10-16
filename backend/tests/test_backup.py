"""Pruebas del respaldo automatizado (JHO-106).

Lo que se verifica aqui:

* El respaldo de SQLite es consistente, se comprime y lleva su sha256.
* La verificacion abre la copia, corre ``integrity_check`` y recomputa la
  CADENA DE AUDITORIA dentro del respaldo: un respaldo que no se puede abrir o
  que trae la cadena rota no cuenta como respaldo.
* La copia restaurada es una base funcional: mismas tablas, mismos datos.
* La rotacion conserva los ultimos N y borra los viejos con su firma.
* Un archivo alterado o corrupto se detecta antes de confiar en el.
* Sin ``pg_dump`` el respaldo de PostgreSQL falla con un mensaje claro en vez
  de generar un archivo vacio.
"""

from __future__ import annotations

import gzip
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import crear_titular, iniciar_sesion
from sqlalchemy import func, select

from license_manager.config import Settings
from license_manager.models import AuditEvent, User
from license_manager.services.audit import verify_chain
from license_manager.tools import backup


def _preparar_datos(client, db_session, eventos: int = 3) -> None:
    """Deja una cuenta y varios eventos de auditoria en la base viva."""
    crear_titular(db_session)
    iniciar_sesion(client, "titular@frubber.co", "clave-segura-123")
    for _ in range(eventos):
        client.get("/", follow_redirects=False)


def _respaldar(settings: Settings, destino: Path, *, cuando: datetime | None = None) -> Path:
    return backup.respaldar(settings, destino, cuando)


# --------------------------------------------------------------------------- #
# Respaldo + verificacion
# --------------------------------------------------------------------------- #


def test_el_respaldo_se_crea_con_su_firma(client, db_session, settings, tmp_path):
    _preparar_datos(client, db_session)
    destino = tmp_path / "respaldos"

    archivo = _respaldar(settings, destino)

    assert archivo.is_file()
    assert archivo.name.startswith(backup.PREFIJO)
    assert archivo.name.endswith(backup.SUFIJO_SQLITE)
    firma = archivo.with_name(f"{archivo.name}.sha256")
    assert firma.is_file()
    assert firma.read_text(encoding="utf-8").strip() == backup.sha256_de(archivo)
    assert backup.hash_coincide(archivo)


def test_la_verificacion_comprueba_integridad_y_cadena(client, db_session, settings, tmp_path):
    _preparar_datos(client, db_session)
    archivo = _respaldar(settings, tmp_path / "respaldos")

    ok, detalle = backup.verificar(archivo)

    assert ok, detalle
    vivos = db_session.execute(select(func.count(AuditEvent.id))).scalar_one()
    assert f"cadena con {vivos} evento(s)" in detalle


def test_la_copia_restaurada_es_una_base_funcional(client, db_session, settings, tmp_path):
    _preparar_datos(client, db_session)
    archivo = _respaldar(settings, tmp_path / "respaldos")
    vivos_usuarios = db_session.execute(select(func.count(User.id))).scalar_one()
    vivos_eventos = db_session.execute(select(func.count(AuditEvent.id))).scalar_one()

    restaurada = tmp_path / "restaurada.db"
    with gzip.open(archivo, "rb") as entrada, restaurada.open("wb") as salida:
        salida.write(entrada.read())

    conexion = sqlite3.connect(str(restaurada))
    try:
        assert conexion.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tablas = {
            fila[0]
            for fila in conexion.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        usuarios = conexion.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        eventos = conexion.execute("SELECT COUNT(*) FROM auditoria").fetchone()[0]
    finally:
        conexion.close()

    assert {"usuarios", "auditoria", "licencias"} <= tablas
    assert usuarios == vivos_usuarios
    assert eventos == vivos_eventos


def test_el_respaldo_no_altera_la_base_viva(client, db_session, settings, tmp_path):
    _preparar_datos(client, db_session)
    antes = db_session.execute(select(func.count(AuditEvent.id))).scalar_one()

    _respaldar(settings, tmp_path / "respaldos")

    assert db_session.execute(select(func.count(AuditEvent.id))).scalar_one() == antes
    assert verify_chain(db_session).ok


# --------------------------------------------------------------------------- #
# Rotacion
# --------------------------------------------------------------------------- #


def test_la_rotacion_conserva_solo_los_ultimos(client, db_session, settings, tmp_path):
    _preparar_datos(client, db_session)
    destino = tmp_path / "respaldos"
    base = datetime(2026, 10, 1, 3, 0, 0, tzinfo=UTC)
    for indice in range(4):
        backup.respaldar(settings, destino, base + timedelta(days=indice))

    eliminados = backup.rotar(destino, 2)

    restantes = backup.respaldos_en(destino)
    assert len(restantes) == 2
    assert len(eliminados) == 2
    # Los que quedan son los dos mas nuevos (el nombre lleva la fecha).
    assert restantes[-1].name == f"{backup.PREFIJO}20261004-030000{backup.SUFIJO_SQLITE}"
    # Cada respaldo viejo se fue con su firma: no quedan .sha256 huerfanos.
    assert not any(eliminado.exists() for eliminado in eliminados)
    for eliminado in eliminados:
        assert not eliminado.with_name(f"{eliminado.name}.sha256").exists()
    for restante in restantes:
        assert restante.with_name(f"{restante.name}.sha256").is_file()


def test_la_rotacion_sin_tope_no_borra_nada(client, db_session, settings, tmp_path):
    _preparar_datos(client, db_session)
    destino = tmp_path / "respaldos"
    _respaldar(settings, destino)

    assert backup.rotar(destino, 0) == []
    assert len(backup.respaldos_en(destino)) == 1


# --------------------------------------------------------------------------- #
# Un respaldo que no sirve se delata
# --------------------------------------------------------------------------- #


def test_un_respaldo_corrupto_no_pasa_la_verificacion(client, db_session, settings, tmp_path):
    _preparar_datos(client, db_session)
    archivo = _respaldar(settings, tmp_path / "respaldos")

    archivo.write_bytes(b"esto no es un gzip valido")

    ok, detalle = backup.verificar(archivo)
    assert not ok
    assert detalle


def test_la_firma_detecta_una_copia_alterada(client, db_session, settings, tmp_path):
    _preparar_datos(client, db_session)
    archivo = _respaldar(settings, tmp_path / "respaldos")
    assert backup.hash_coincide(archivo)

    with archivo.open("ab") as manejador:
        manejador.write(b"basura")

    assert not backup.hash_coincide(archivo)


def test_una_cadena_rota_dentro_del_respaldo_no_pasa(client, db_session, settings, tmp_path):
    """El respaldo de una base manipulada se detecta al verificar."""
    _preparar_datos(client, db_session)
    archivo = _respaldar(settings, tmp_path / "respaldos")

    # Se rompe la copia comprimida: se descomprime, se altera y se recomprime.
    datos = gzip.decompress(archivo.read_bytes())
    minado = tmp_path / "minado.db"
    minado.write_bytes(datos)
    with sqlite3.connect(str(minado)) as conexion:
        conexion.execute("UPDATE auditoria SET detalle = ? WHERE id = 1", ('{"x":1}',))
        conexion.commit()
    archivo.write_bytes(gzip.compress(minado.read_bytes()))

    ok, detalle = backup.verificar(archivo)

    assert not ok
    assert "cadena de auditoria esta rota" in detalle


# --------------------------------------------------------------------------- #
# PostgreSQL: comando y ausencia de pg_dump
# --------------------------------------------------------------------------- #


def test_la_base_en_memoria_no_se_puede_respaldar():
    with pytest.raises(backup.BackupError):
        backup.ruta_sqlite("sqlite:///:memory:")


def test_el_comando_de_pg_dump_es_restaurable(tmp_path):
    destino = tmp_path / "volcado.dump"

    comando = backup.comando_pg_dump(
        "postgresql+psycopg://usuario:clave@localhost:5432/license_manager", destino
    )

    assert comando[0] == "pg_dump"
    assert "--format=custom" in comando
    assert str(destino) in comando
    # pg_dump no entiende el sufijo de driver de SQLAlchemy.
    url = comando[-1]
    assert url.startswith("postgresql://")
    assert "psycopg" not in url
    assert url.endswith("/license_manager")


def test_sin_pg_dump_el_respaldo_postgres_avisa(monkeypatch, tmp_path):
    monkeypatch.setattr(backup.shutil, "which", lambda _: None)

    with pytest.raises(backup.BackupError, match="pg_dump"):
        backup.respaldar_postgres(
            "postgresql+psycopg://usuario:clave@localhost:5432/license_manager",
            tmp_path,
            datetime.now(UTC),
        )


# --------------------------------------------------------------------------- #
# Herramienta de linea de comandos
# --------------------------------------------------------------------------- #


def test_la_herramienta_de_linea_de_comandos_respalda_y_verifica(
    client, db_session, settings, tmp_path, monkeypatch
):
    _preparar_datos(client, db_session)
    monkeypatch.setattr(backup, "get_settings", lambda: settings)
    destino = tmp_path / "respaldos-cron"

    codigo = backup.main(["--dest", str(destino), "--keep", "1", "--verify"])

    assert codigo == 0
    archivos = backup.respaldos_en(destino)
    assert len(archivos) == 1
    assert backup.hash_coincide(archivos[0])


def test_la_herramienta_toma_los_valores_de_la_configuracion(
    client, db_session, settings, tmp_path, monkeypatch
):
    """Sin banderas, el respaldo usa LM_BACKUP_DIR / LM_BACKUP_KEEP."""
    _preparar_datos(client, db_session)
    destino = tmp_path / "desde-entorno"
    configurada = Settings(
        _env_file=None,
        environment="test",
        database_url=settings.database_url,
        backup_dir=str(destino),
        backup_keep=3,
    )
    monkeypatch.setattr(backup, "get_settings", lambda: configurada)

    assert backup.main([]) == 0

    assert destino.is_dir()
    assert len(backup.respaldos_en(destino)) == 1


def test_la_herramienta_de_linea_de_comandos_falla_si_la_base_no_existe(
    settings, tmp_path, monkeypatch
):
    rota = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'no-existe.db'}",
    )
    monkeypatch.setattr(backup, "get_settings", lambda: rota)

    codigo = backup.main(["--dest", str(tmp_path / "respaldos")])

    assert codigo == 1
