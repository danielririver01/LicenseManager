"""Respaldo automatizado de la base de datos (JHO-106).

Un respaldo que nadie prueba no es un respaldo. Esta herramienta hace las tres
cosas que pide el issue: copia, comprime y rota; y ademas VERIFICA que la copia
se pueda abrir y que la cadena de auditoria siga sana dentro de ella.

Uso tipico (cron / systemd timer, una vez al dia):

    python -m license_manager.tools.backup --dest /var/backups/license-manager --keep 14 --verify

Con subida a almacenamiento de objetos (Backblaze B2 / S3 via rclone):

    python -m license_manager.tools.backup --verify --upload "rclone copy {file} b2:frubber-backups"

Reglas:

* SQLite: usa la API de respaldo ``sqlite3`` -> copia CONSISTENTE aunque el
  sitio este en uso (no se copia el archivo a mano mientras se escribe).
* PostgreSQL: ``pg_dump`` en formato custom (comprimible y restaurable tabla a
  tabla con ``pg_restore``).
* Cada respaldo lleva su ``.sha256`` al lado: si el archivo cambia en el
  almacenamiento, se nota.
* La rotacion conserva los ultimos N y borra el resto con su hash.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from license_manager.config import Settings, get_settings
from license_manager.db import create_session_factory
from license_manager.services.audit import verify_chain

PREFIJO = "lm-backup-"
SUFIJO_SQLITE = ".db.gz"
SUFIJO_POSTGRES = ".dump.gz"


class BackupError(RuntimeError):
    """El respaldo no se pudo completar o no paso la verificacion."""


# --------------------------------------------------------------------------- #
# Nombres y utilidades
# --------------------------------------------------------------------------- #


def momento_utc() -> datetime:
    return datetime.now(UTC)


def nombre_respaldo(momento: datetime, sufijo: str) -> str:
    return f"{PREFIJO}{momento:%Y%m%d-%H%M%S}{sufijo}"


def sha256_de(archivo: Path) -> str:
    digest = hashlib.sha256()
    with archivo.open("rb") as manejador:
        for bloque in iter(lambda: manejador.read(65536), b""):
            digest.update(bloque)
    return digest.hexdigest()


def _comprimir(origen: Path, destino: Path) -> None:
    with origen.open("rb") as entrada, gzip.open(destino, "wb", compresslevel=6) as salida:
        shutil.copyfileobj(entrada, salida)


def _descomprimir(origen: Path, destino: Path) -> None:
    with gzip.open(origen, "rb") as entrada, destino.open("wb") as salida:
        shutil.copyfileobj(entrada, salida)


def _es_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def ruta_sqlite(url: str) -> Path:
    """Ruta del archivo SQLite. Rechaza la base en memoria (no hay nada que copiar)."""
    base = make_url(url).database or ""
    if not base or base == ":memory:":
        raise BackupError(
            "La base SQLite esta en memoria: no hay archivo que respaldar. "
            "Configura LM_DATABASE_URL con una ruta en disco."
        )
    return Path(base)


def url_postgres(url: str) -> str:
    """URL lista para pg_dump: sin el sufijo de driver de SQLAlchemy."""
    base = make_url(url).set(drivername="postgresql")
    return base.render_as_string(hide_password=False)


def comando_pg_dump(url: str, destino: Path, *, pg_dump: str = "pg_dump") -> list[str]:
    """Argumentos de pg_dump (separados para poder probarlos sin servidor)."""
    return [
        pg_dump,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(destino),
        url_postgres(url),
    ]


# --------------------------------------------------------------------------- #
# Respaldos
# --------------------------------------------------------------------------- #


def respaldar_sqlite(origen: Path, destino: Path, momento: datetime) -> Path:
    """Copia consistente con la API de respaldo y la comprime."""
    if not origen.is_file():
        raise BackupError(f"No existe la base de datos en {origen}.")

    archivo = destino / nombre_respaldo(momento, SUFIJO_SQLITE)
    with tempfile.TemporaryDirectory() as temporal:
        crudo = Path(temporal) / "copia.db"
        try:
            # mode=ro: el respaldo lee la base viva sin poder tocarla.
            fuente = sqlite3.connect(f"file:{origen.as_posix()}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            fuente = sqlite3.connect(str(origen))
        copia = sqlite3.connect(str(crudo))
        try:
            fuente.backup(copia)
        finally:
            # sqlite3.connect no cierra con el `with`: hay que cerrar a mano o
            # Windows no deja borrar el archivo temporal.
            copia.close()
            fuente.close()
        _comprimir(crudo, archivo)
    return archivo


def respaldar_postgres(url: str, destino: Path, momento: datetime) -> Path:
    """Vuelca la base con pg_dump y comprime el volcado."""
    if shutil.which("pg_dump") is None:
        raise BackupError(
            "No se encontro pg_dump en el PATH. Instala el cliente de "
            "PostgreSQL (apt install postgresql-client) en el servidor."
        )

    archivo = destino / nombre_respaldo(momento, SUFIJO_POSTGRES)
    with tempfile.TemporaryDirectory() as temporal:
        crudo = Path(temporal) / "volcado.dump"
        resultado = subprocess.run(  # noqa: S603 - argumentos fijos, sin shell
            comando_pg_dump(url, crudo),
            capture_output=True,
            text=True,
            check=False,
        )
        if resultado.returncode != 0:
            raise BackupError(f"pg_dump fallo: {resultado.stderr.strip()}")
        _comprimir(crudo, archivo)
    return archivo


def respaldar(settings: Settings, destino: Path, momento: datetime | None = None) -> Path:
    """Elige el motor segun la configuracion y devuelve el archivo creado."""
    destino.mkdir(parents=True, exist_ok=True)
    momento = momento or momento_utc()
    if _es_sqlite(settings.database_url):
        archivo = respaldar_sqlite(ruta_sqlite(settings.database_url), destino, momento)
    else:
        archivo = respaldar_postgres(settings.database_url, destino, momento)

    (destino / f"{archivo.name}.sha256").write_text(sha256_de(archivo) + "\n", encoding="utf-8")
    return archivo


# --------------------------------------------------------------------------- #
# Verificacion
# --------------------------------------------------------------------------- #


def verificar_sqlite(archivo: Path) -> tuple[bool, str]:
    """Abre el respaldo, revisa la integridad y recomputa la cadena de auditoria."""
    with tempfile.TemporaryDirectory() as temporal:
        copia = Path(temporal) / "copia.db"
        _descomprimir(archivo, copia)

        conexion = sqlite3.connect(str(copia))
        try:
            integridad = conexion.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conexion.close()
        if integridad != "ok":
            return False, f"integrity_check devolvio: {integridad}"

        engine = create_engine(f"sqlite:///{copia.as_posix()}")
        sesion = create_session_factory(engine)()
        try:
            cadena = verify_chain(sesion)
        finally:
            sesion.close()
            engine.dispose()

        if not cadena.ok:
            return False, (
                f"la cadena de auditoria esta rota en el evento #{cadena.broken_at}: "
                f"{cadena.reason}"
            )
        return True, f"integridad ok · cadena con {cadena.total_events} evento(s)"


def verificar_postgres(archivo: Path) -> tuple[bool, str]:
    """Confirma que pg_restore puede leer el volcado."""
    if shutil.which("pg_restore") is None:
        return False, "No se encontro pg_restore para validar el volcado."
    with tempfile.TemporaryDirectory() as temporal:
        volcado = Path(temporal) / "volcado.dump"
        _descomprimir(archivo, volcado)
        resultado = subprocess.run(  # noqa: S603 - argumentos fijos, sin shell
            ["pg_restore", "--list", str(volcado)],
            capture_output=True,
            text=True,
            check=False,
        )
    if resultado.returncode != 0:
        return False, f"pg_restore no pudo leer el volcado: {resultado.stderr.strip()}"
    tablas = sum(1 for linea in resultado.stdout.splitlines() if " TABLE " in linea)
    return True, f"volcado legible · {tablas} tabla(s)"


def verificar(archivo: Path) -> tuple[bool, str]:
    if not archivo.is_file():
        return False, f"No existe el respaldo {archivo}."
    try:
        if archivo.name.endswith(SUFIJO_SQLITE):
            return verificar_sqlite(archivo)
        return verificar_postgres(archivo)
    except (OSError, sqlite3.DatabaseError) as exc:
        # Un archivo truncado o que no es un respaldo no puede tumbar el cron:
        # se reporta como verificacion fallida.
        return False, f"el respaldo no se pudo abrir: {exc}"


def hash_coincide(archivo: Path) -> bool:
    """El .sha256 al lado debe coincidir: detecta una copia corrupta o cambiada."""
    firma = archivo.with_name(f"{archivo.name}.sha256")
    if not firma.is_file():
        return True  # sin firma no se puede afirmar nada; la verificacion real es la cadena
    return firma.read_text(encoding="utf-8").strip() == sha256_de(archivo)


# --------------------------------------------------------------------------- #
# Rotacion y subida
# --------------------------------------------------------------------------- #


def respaldos_en(destino: Path) -> list[Path]:
    """Respaldos ordenados del mas viejo al mas nuevo (el nombre lleva la fecha)."""
    if not destino.is_dir():
        return []
    return sorted(
        archivo
        for archivo in destino.iterdir()
        if archivo.is_file()
        and archivo.name.startswith(PREFIJO)
        and not archivo.name.endswith(".sha256")
    )


def rotar(destino: Path, conservar: int) -> list[Path]:
    """Borra los respaldos mas viejos y devuelve los eliminados."""
    if conservar < 1:
        return []
    archivos = respaldos_en(destino)
    sobran = archivos[:-conservar] if len(archivos) > conservar else []
    for archivo in sobran:
        archivo.unlink(missing_ok=True)
        archivo.with_name(f"{archivo.name}.sha256").unlink(missing_ok=True)
    return sobran


def subir(archivo: Path, plantilla: str) -> None:
    """Ejecuta el comando de subida del operador (sin shell). ``{file}`` se sustituye."""
    partes = shlex.split(plantilla.replace("{file}", str(archivo)))
    if not partes:
        raise BackupError("El comando de subida esta vacio.")
    resultado = subprocess.run(  # noqa: S603 - comando del operador, sin shell
        partes, capture_output=True, text=True, check=False
    )
    if resultado.returncode != 0:
        raise BackupError(f"La subida fallo: {resultado.stderr.strip()}")
    # La firma viaja con el respaldo.
    firma = archivo.with_name(f"{archivo.name}.sha256")
    if firma.is_file():
        partes_firma = shlex.split(plantilla.replace("{file}", str(firma)))
        subprocess.run(partes_firma, capture_output=True, text=True, check=False)  # noqa: S603


# --------------------------------------------------------------------------- #
# Punto de entrada
# --------------------------------------------------------------------------- #


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="license_manager.tools.backup",
        description="Respalda, comprime, verifica y rota la base de License Manager.",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="Carpeta destino de los respaldos (por defecto: LM_BACKUP_DIR).",
    )
    parser.add_argument(
        "--keep", type=int, default=None, help="Cuantos respaldos conservar (LM_BACKUP_KEEP)."
    )
    parser.add_argument(
        "--verify", action="store_true", help="Abre el respaldo y comprueba integridad + cadena."
    )
    parser.add_argument(
        "--upload",
        default=None,
        help='Comando de subida con {file}, p.ej. "rclone copy {file} b2:frubber-backups".',
    )
    parser.add_argument("--quiet", action="store_true", help="Solo imprime la linea final.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)
    settings = get_settings()
    destino = Path(args.dest or settings.backup_dir)
    conservar = args.keep if args.keep is not None else settings.backup_keep
    subida = args.upload or settings.backup_upload

    try:
        archivo = respaldar(settings, destino)
    except BackupError as exc:
        print(f"RESPALDO FALLIDO: {exc}", file=sys.stderr)
        return 1

    linea = f"respaldo {archivo.name} · {archivo.stat().st_size / 1024:.0f} KiB"

    if args.verify:
        try:
            ok, detalle = verificar(archivo)
        except (BackupError, OSError, sqlite3.DatabaseError) as exc:
            print(f"VERIFICACION FALLIDA: {exc}", file=sys.stderr)
            return 1
        if not ok:
            print(f"VERIFICACION FALLIDA: {detalle}", file=sys.stderr)
            return 1
        if not hash_coincide(archivo):
            print("VERIFICACION FALLIDA: el sha256 no coincide.", file=sys.stderr)
            return 1
        linea = f"{linea} · verificado ({detalle})"

    eliminados = rotar(destino, conservar)
    if eliminados and not args.quiet:
        print(f"rotacion: {len(eliminados)} respaldo(s) viejo(s) eliminado(s)")

    if subida:
        try:
            subir(archivo, subida)
        except BackupError as exc:
            print(f"SUBIDA FALLIDA: {exc}", file=sys.stderr)
            return 1
        linea = f"{linea} · subido"

    print(linea)
    return 0


if __name__ == "__main__":  # pragma: no cover - entrada de consola
    raise SystemExit(main())
