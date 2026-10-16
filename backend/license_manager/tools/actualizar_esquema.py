"""Pone al dia el esquema de una base que YA existe (SQLite de desarrollo).

Este proyecto crea el esquema con ``create_all`` y todavia no tiene Alembic: una
base nueva nace correcta, pero una que ya estaba creada no recibe los cambios.
Esta herramienta cubre ese hueco para las bases de desarrollo, SIN perder datos:

* Agrega las columnas que falten (comparando el modelo con ``PRAGMA table_info``).
* Reconstruye ``usuarios`` si su CHECK de roles no admite los roles actuales
  (SQLite no permite modificar un CHECK sin recrear la tabla).

Es idempotente: correrla dos veces no hace nada la segunda.

Uso::

    python -m license_manager.tools.actualizar_esquema
    python -m license_manager.tools.actualizar_esquema --db C:\\ruta\\dev.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.dialects import sqlite as sqlite_dialect_module

from license_manager.config import Settings, get_settings
from license_manager.db import Base
from license_manager.models import Role  # noqa: F401 - registra las tablas en metadata


class EsquemaError(RuntimeError):
    """No se pudo leer o actualizar el esquema."""


def sqlite_dialect():
    return sqlite_dialect_module.dialect()


def ruta_de_sqlite(url: str) -> Path:
    from sqlalchemy.engine import make_url

    base = make_url(url).database or ""
    if not base or base == ":memory:":
        raise EsquemaError("La base es en memoria: no hay archivo que actualizar.")
    return Path(base)


def columnas_reales(conexion: sqlite3.Connection, tabla: str) -> set[str]:
    return {fila[1] for fila in conexion.execute(f"PRAGMA table_info({tabla})")}


def tabla_del_modelo(nombre: str):
    return Base.metadata.tables[nombre]


def _ddl_del_modelo(tabla: str) -> str:
    """CREATE TABLE tal como lo generaria el modelo, leido de una base nueva.

    Se extrae del propio modelo (no se escribe a mano) para que nunca se
    despegue del esquema real: si manana cambia una columna, esto la sigue.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as temporal:
        motor = create_engine(f"sqlite:///{Path(temporal) / 'patron.db'}")
        tabla_del_modelo(tabla).create(motor)
        with motor.connect() as conexion:
            ddl = conexion.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tabla,)
            ).scalar_one()
        motor.dispose()
    return ddl


def agregar_columnas_faltantes(conexion: sqlite3.Connection, avisos: list[str]) -> int:
    dialecto = sqlite_dialect()
    agregadas = 0
    for tabla in Base.metadata.sorted_tables:
        existentes = columnas_reales(conexion, tabla.name)
        if not existentes:
            continue  # la tabla no existe: create_all la creara entera
        for columna in tabla.columns:
            if columna.name in existentes:
                continue
            tipo = columna.type.compile(dialect=dialecto)
            conexion.execute(f'ALTER TABLE {tabla.name} ADD COLUMN "{columna.name}" {tipo}')
            avisos.append(f"columna agregada: {tabla.name}.{columna.name} ({tipo})")
            agregadas += 1
    return agregadas


def admite_el_rol(conexion: sqlite3.Connection, rol: str) -> bool:
    """Prueba real: intenta guardar y deshace, sin tocar datos."""
    try:
        conexion.execute(
            "INSERT INTO usuarios (id, nombre_completo, email, password_hash, rol, activo, creado_at) "
            "VALUES ('__prueba__', 'prueba', '__prueba__', 'x', ?, 1, '2000-01-01 00:00:00')",
            (rol,),
        )
        conexion.execute("DELETE FROM usuarios WHERE id = '__prueba__'")
        return True
    except sqlite3.IntegrityError:
        return False


def reconstruir_usuarios(conexion: sqlite3.Connection, avisos: list[str]) -> None:
    """Recrea ``usuarios`` con el DDL del modelo, conservando las filas.

    Orden canonico de SQLite: crear la tabla nueva con OTRO nombre, copiar,
    borrar la vieja y recien ahi renombrar. Nunca se renombra la tabla que otros
    referencian: si se hiciera, SQLite reescribiria las claves foraneas de las
    demas tablas y quedarian apuntando a una tabla que ya no existe.
    """
    ddl = _ddl_del_modelo("usuarios")
    comunes = columnas_reales(conexion, "usuarios") & {
        columna.name for columna in tabla_del_modelo("usuarios").columns
    }
    destino = ", ".join(f'"{nombre}"' for nombre in sorted(comunes))

    conexion.execute("PRAGMA foreign_keys=OFF")
    conexion.execute("PRAGMA legacy_alter_table=ON")
    conexion.execute("BEGIN")
    conexion.execute(ddl.replace("CREATE TABLE usuarios", "CREATE TABLE usuarios_nueva", 1))
    conexion.execute(
        f"INSERT INTO usuarios_nueva ({destino}) SELECT {destino} FROM usuarios"
    )
    conexion.execute("DROP TABLE usuarios")
    conexion.execute("ALTER TABLE usuarios_nueva RENAME TO usuarios")
    conexion.execute("COMMIT")
    conexion.execute("PRAGMA legacy_alter_table=OFF")
    conexion.execute("PRAGMA foreign_keys=ON")
    avisos.append("tabla usuarios reconstruida con el CHECK de roles al dia")

    pendientes = conexion.execute("PRAGMA foreign_key_check").fetchall()
    if pendientes:
        raise EsquemaError(f"Quedaron claves foraneas rotas: {pendientes}")

    # Las otras tablas deben seguir apuntando a ``usuarios``.
    for tabla in Base.metadata.sorted_tables:
        if tabla.name == "usuarios":
            continue
        for fk in conexion.execute(f"PRAGMA foreign_key_list({tabla.name})").fetchall():
            if fk[2] != "usuarios" and fk[2] in {"usuarios_nueva", "usuarios_anterior"}:
                raise EsquemaError(
                    f"{tabla.name} quedo apuntando a {fk[2]} en vez de a usuarios."
                )


def actualizar(ruta: Path) -> list[str]:
    if not ruta.is_file():
        raise EsquemaError(f"No existe la base {ruta}.")

    avisos: list[str] = []
    # isolation_level=None (autocommit): los PRAGMA de SQLite se ignoran dentro
    # de una transaccion, y la reconstruccion maneja su propio BEGIN/COMMIT.
    conexion = sqlite3.connect(str(ruta), isolation_level=None)
    try:
        agregadas = agregar_columnas_faltantes(conexion, avisos)

        if not admite_el_rol(conexion, Role.TECNICO.value):
            reconstruir_usuarios(conexion, avisos)
    finally:
        conexion.close()

    if not agregadas and len(avisos) <= 1:
        avisos.append("el esquema ya estaba al dia")
    return avisos


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="license_manager.tools.actualizar_esquema",
        description="Pone al dia el esquema de una base SQLite existente sin perder datos.",
    )
    parser.add_argument("--db", default=None, help="Ruta del archivo (por defecto: LM_DATABASE_URL).")
    return parser.parse_args(argv)


def _ajustes_con(db: str | None) -> Settings:
    if db is None:
        return get_settings()
    return Settings(_env_file=None, database_url=f"sqlite:///{db}")


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)
    try:
        ruta = ruta_de_sqlite(_ajustes_con(args.db).database_url)
        avisos = actualizar(ruta)
    except EsquemaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Base: {ruta}")
    for aviso in avisos:
        print(f"  - {aviso}")
    print("Listo.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entrada de consola
    raise SystemExit(main())
