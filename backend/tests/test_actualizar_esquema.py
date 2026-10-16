"""Pruebas del actualizador de esquema para bases existentes (JHO-107).

El caso que protegen: una base de desarrollo creada ANTES de que existiera el
rol tecnico. Tiene que quedar al dia, con los datos intactos y las claves
foraneas de las demas tablas apuntando a ``usuarios`` (no a la tabla temporal
que usa la reconstruccion).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from license_manager.tools import actualizar_esquema

# Esquema viejo: sin tecnico_id y con el CHECK de roles anterior.
_USUARIOS_VIEJO = """
CREATE TABLE usuarios (
    id CHAR(32) NOT NULL PRIMARY KEY,
    nombre_completo VARCHAR(120) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    rol VARCHAR(16) NOT NULL,
    activo BOOLEAN NOT NULL,
    creado_at DATETIME NOT NULL,
    creado_por_id CHAR(32),
    desactivado_at DATETIME,
    ultimo_acceso_at DATETIME,
    CONSTRAINT ck_usuarios_rol CHECK (rol IN ('owner','coowner','admin')),
    FOREIGN KEY(creado_por_id) REFERENCES usuarios (id)
)
"""

_VERDULERIAS_VIEJO = """
CREATE TABLE verdulerias (
    id CHAR(32) NOT NULL PRIMARY KEY,
    nombre VARCHAR(160) NOT NULL,
    creado_por_id CHAR(32),
    FOREIGN KEY(creado_por_id) REFERENCES usuarios (id)
)
"""

_SESIONES_VIEJO = """
CREATE TABLE sesiones (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    usuario_id CHAR(32) NOT NULL,
    FOREIGN KEY(usuario_id) REFERENCES usuarios (id)
)
"""


@pytest.fixture
def base_vieja(tmp_path) -> Path:
    ruta = tmp_path / "vieja.db"
    conexion = sqlite3.connect(str(ruta))
    try:
        conexion.executescript(_USUARIOS_VIEJO + ";" + _VERDULERIAS_VIEJO + ";" + _SESIONES_VIEJO + ";")
        conexion.execute(
            "INSERT INTO usuarios (id, nombre_completo, email, password_hash, rol, activo, creado_at) "
            "VALUES ('u1', 'Jhoan Rivera', 'titular@frubber.local', 'hash', 'owner', 1, '2026-10-15')"
        )
        conexion.execute(
            "INSERT INTO verdulerias (id, nombre, creado_por_id) "
            "VALUES ('v1', 'Verduleria La Huerta', 'u1')"
        )
        conexion.execute("INSERT INTO sesiones (id, usuario_id) VALUES ('s1', 'u1')")
        conexion.commit()
    finally:
        conexion.close()
    return ruta


def _columnas(ruta: Path, tabla: str) -> set[str]:
    conexion = sqlite3.connect(str(ruta))
    try:
        return {fila[1] for fila in conexion.execute(f"PRAGMA table_info({tabla})")}
    finally:
        conexion.close()


def _padre_de(ruta: Path, tabla: str) -> set[str]:
    conexion = sqlite3.connect(str(ruta))
    try:
        return {fila[2] for fila in conexion.execute(f"PRAGMA foreign_key_list({tabla})")}
    finally:
        conexion.close()


def test_actualiza_una_base_vieja_sin_perder_datos(base_vieja):
    avisos = actualizar_esquema.actualizar(base_vieja)

    assert any("tecnico_id" in aviso for aviso in avisos)
    assert any("reconstruida" in aviso for aviso in avisos)

    # Los datos siguen ahi.
    conexion = sqlite3.connect(str(base_vieja))
    try:
        assert conexion.execute("SELECT nombre_completo FROM usuarios").fetchone()[0] == "Jhoan Rivera"
        assert conexion.execute("SELECT nombre FROM verdulerias").fetchone()[0] == "Verduleria La Huerta"
        assert conexion.execute("SELECT COUNT(*) FROM sesiones").fetchone()[0] == 1
    finally:
        conexion.close()

    assert "tecnico_id" in _columnas(base_vieja, "usuarios")


def test_la_reconstruccion_no_rompe_las_claves_foraneas(base_vieja):
    actualizar_esquema.actualizar(base_vieja)

    conexion = sqlite3.connect(str(base_vieja))
    try:
        assert conexion.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conexion.close()

    # Las otras tablas deben seguir apuntando a ``usuarios``, no a la temporal.
    assert _padre_de(base_vieja, "verdulerias") == {"usuarios"}
    assert _padre_de(base_vieja, "sesiones") == {"usuarios"}


def test_despues_se_puede_crear_un_tecnico(base_vieja):
    actualizar_esquema.actualizar(base_vieja)

    conexion = sqlite3.connect(str(base_vieja))
    try:
        conexion.execute(
            "INSERT INTO usuarios (id, nombre_completo, email, password_hash, rol, activo, creado_at) "
            "VALUES ('u2', 'Carlos Balanzas', 'carlos@balanzas.co', 'hash', 'tecnico', 1, '2026-10-16')"
        )
        fila = conexion.execute("SELECT rol FROM usuarios WHERE id = 'u2'").fetchone()
        assert fila[0] == "tecnico"
    finally:
        conexion.close()


def test_sigue_rechazando_un_rol_inventado(base_vieja):
    actualizar_esquema.actualizar(base_vieja)

    conexion = sqlite3.connect(str(base_vieja))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conexion.execute(
                "INSERT INTO usuarios (id, nombre_completo, email, password_hash, rol, activo, creado_at) "
                "VALUES ('u3', 'X', 'x@y.co', 'hash', 'superadmin', 1, '2026-10-16 10:00:00')"
            )
    finally:
        conexion.close()


def test_es_idempotente(base_vieja):
    actualizar_esquema.actualizar(base_vieja)

    avisos = actualizar_esquema.actualizar(base_vieja)

    assert avisos == ["el esquema ya estaba al dia"]
    conexion = sqlite3.connect(str(base_vieja))
    try:
        assert conexion.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 1
    finally:
        conexion.close()


def test_una_base_inexistente_da_un_error_claro(tmp_path):
    with pytest.raises(actualizar_esquema.EsquemaError):
        actualizar_esquema.actualizar(tmp_path / "no-existe.db")


def test_la_herramienta_de_linea_de_comandos(base_vieja, capsys):
    codigo = actualizar_esquema.main(["--db", str(base_vieja)])

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "reconstruida" in salida
