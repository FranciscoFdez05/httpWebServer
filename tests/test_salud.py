"""/api/health y la configuración: lo que docker-update.sh da por hecho.

Si algo de aquí deja de cumplirse, la actualización automática dejará de
funcionar (o peor: volverá atrás una versión que estaba bien).
"""

import os
import re

from conftest import crear_usuario


def test_health_responde_200_y_la_version(modulo, cliente):
    respuesta = cliente.get("/api/health")
    assert respuesta.status_code == 200
    datos = respuesta.get_json()
    assert datos["status"] == "ok"
    assert datos["version"] == modulo.__version__
    assert datos["checks"]["database"] == "ok"
    assert datos["checks"]["uploads"] == "ok"


def test_health_no_exige_configuracion_inicial(crear_app):
    """Sin admin creado, todo redirige a /setup. Health no puede: si no,
    docker-update.sh daría por muerta una instalación recién levantada."""
    modulo = crear_app(sin_admin=True)
    cliente = modulo.app.test_client()
    assert cliente.get("/").status_code == 302
    assert cliente.get("/api/health").status_code == 200


def test_health_no_exige_sesion(modulo, cliente):
    crear_usuario(modulo, "jefe", "contrasena-larga", admin=True)
    assert cliente.get("/api/health").status_code == 200


def test_health_devuelve_503_si_la_base_de_datos_falla(modulo, cliente):
    with open(modulo.DB_PATH, "wb") as f:
        f.write(b"esto no es una base de datos sqlite")

    respuesta = cliente.get("/api/health")
    assert respuesta.status_code == 503
    assert respuesta.get_json()["status"] == "error"


def test_health_devuelve_503_si_no_se_puede_escribir(modulo, cliente, monkeypatch):
    monkeypatch.setattr(os, "access", lambda *a, **k: False)
    respuesta = cliente.get("/api/health")
    assert respuesta.status_code == 503
    assert "uploads" in respuesta.get_json()["checks"]


def test_la_version_es_la_que_leen_los_scripts():
    """docker-update.sh y docker-up.sh sacan la versión con este sed:
       sed -n 's/^__version__ = "\\(.*\\)"/\\1/p' app.py
    Si el formato de la línea cambia, las imágenes dejan de etiquetarse y el
    rollback deja de existir, sin que nada avise."""
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "app.py"), encoding="utf-8") as f:
        contenido = f.read()
    encontrado = re.findall(r'^__version__ = "(.*)"$', contenido, re.MULTILINE)
    assert len(encontrado) == 1, "debe haber exactamente una línea __version__"
    assert re.match(r"^\d+\.\d+\.\d+$", encontrado[0])


def test_el_changelog_empieza_por_la_version_actual(modulo):
    """docker-update.sh imprime el primer bloque '## [' del CHANGELOG como
    'novedades de la versión nueva'."""
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "CHANGELOG.md"), encoding="utf-8") as f:
        contenido = f.read()
    primera = re.search(r"^## \[([^\]]+)\]", contenido, re.MULTILINE)
    assert primera, "el CHANGELOG debe tener al menos una versión"
    assert primera.group(1) == modulo.__version__


def test_el_puerto_del_entorno_manda_sobre_config_ini(crear_app, monkeypatch):
    monkeypatch.setenv("PORT", "9123")
    modulo = crear_app()
    assert modulo.SERVER_PORT == 9123


def test_un_ajuste_no_numerico_no_tumba_el_arranque(crear_app, monkeypatch):
    monkeypatch.setenv("MIN_FREE_DISK_MB", "esto-no-es-un-numero")
    modulo = crear_app()
    assert modulo.MIN_FREE_DISK_MB == 1024


def test_sqlite_en_modo_wal(modulo):
    """Sin WAL, un escritor bloquea a todos los lectores y con 2 workers de
    gunicorn basta una subida para tumbar otra petición."""
    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        modo = db.execute("PRAGMA journal_mode").fetchone()[0]
        db.close()
    assert modo.lower() == "wal"
