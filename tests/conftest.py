"""Arranque de los tests.

Cada test recibe una instalación nueva: directorio de datos propio en un
temporal y base de datos vacía. app.py resuelve DATA_DIR y todos los ajustes
en tiempo de importación, así que las variables de entorno se fijan ANTES de
importarlo y el módulo se recarga por test. Es más lento que compartirlo, pero
es lo único que permite probar un ajuste distinto en cada caso.
"""

import importlib
import os
import sys

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)


def _cargar_app(datadir, **ajustes):
    os.environ["DATA_DIR"] = str(datadir)
    os.environ["SECRET_KEY"] = "clave-de-pruebas-fija-para-que-la-sesion-persista"
    # Un mínimo bajo para que los tests no tengan que inventar contraseñas
    # largas; la política en sí se prueba aparte, fijando el valor real.
    os.environ.setdefault("MIN_PASSWORD_LENGTH", "8")
    for clave, valor in ajustes.items():
        os.environ[clave] = str(valor)

    import app as modulo
    modulo = importlib.reload(modulo)
    modulo.app.config["TESTING"] = True
    # Sin esto habría que sacar el token de cada formulario; el CSRF se prueba
    # aparte, activándolo a propósito.
    modulo.app.config["WTF_CSRF_ENABLED"] = False
    return modulo


def crear_usuario(modulo, username, password, admin=False):
    """Inserta un usuario ya activo (sin obligación de cambiar contraseña)."""
    from werkzeug.security import generate_password_hash

    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        cur = db.execute(
            "INSERT INTO users (username, password_hash, is_admin, must_change_password) "
            "VALUES (?, ?, ?, 0)",
            (username, generate_password_hash(password), 1 if admin else 0),
        )
        db.commit()
        uid = cur.lastrowid
        db.close()
    return uid


AJUSTES_POR_TEST = (
    "MAX_UPLOAD_MB", "USER_QUOTA_MB", "MIN_FREE_DISK_MB", "LAN_SIN_LIMITE",
    "LOGIN_MAX_ATTEMPTS", "LOGIN_WINDOW_MINUTES",
    "MIN_PASSWORD_LENGTH", "BEHIND_PROXY", "SECURE_COOKIES",
)


@pytest.fixture
def crear_app(tmp_path, monkeypatch):
    """Fábrica: permite pedir la app con ajustes concretos dentro del test.

    Por defecto siembra una cuenta de administrador. Sin ninguna,
    require_initial_setup manda TODAS las peticiones a /setup y el test acaba
    comprobando la página de configuración inicial en vez de lo que pretendía.
    Los pocos casos que necesitan una instalación virgen piden sin_admin=True.
    """
    creadas = []

    def fabrica(sin_admin=False, **ajustes):
        for clave in AJUSTES_POR_TEST:
            monkeypatch.delenv(clave, raising=False)
        modulo = _cargar_app(tmp_path / f"datos{len(creadas)}", **ajustes)
        creadas.append(modulo)
        if not sin_admin:
            crear_usuario(modulo, "root", "contrasena-de-instalacion", admin=True)
        return modulo

    yield fabrica


@pytest.fixture
def modulo(crear_app):
    return crear_app()


@pytest.fixture
def cliente(modulo):
    return modulo.app.test_client()


def entrar(cliente, username, password):
    return cliente.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def subir(cliente, nombre, contenido=b"contenido", visibility="private_me",
          compartir=(), desde=None):
    """`desde` fija la IP de origen: el límite de tamaño solo se aplica fuera
    de la LAN, y el cliente de pruebas es 127.0.0.1 (o sea, LAN)."""
    import io as _io

    datos = {
        "files": (_io.BytesIO(contenido), nombre),
        "visibility": visibility,
    }
    if compartir:
        datos["shared_users"] = [str(u) for u in compartir]
    extra = {"environ_base": {"REMOTE_ADDR": desde}} if desde else {}
    return cliente.post(
        "/upload", data=datos, content_type="multipart/form-data",
        follow_redirects=False, **extra
    )
