"""Cuotas, disco, borrado y metadatos: lo que toca ficheros en disco."""

import io
import os

from PIL import Image

from conftest import crear_usuario, entrar, subir


# ── Cuota y disco ─────────────────────────────────────────────────────────────

def test_cuota_por_usuario(crear_app):
    modulo = crear_app(USER_QUOTA_MB=1)
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    entrar(cliente, "ana", "contrasena-larga")

    respuesta = subir(cliente, "grande.bin", b"x" * (2 * 1024 * 1024))
    assert respuesta.status_code == 302

    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        total = db.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        db.close()
    assert total == 0, "el archivo no debería haberse registrado"
    # Y no puede quedarse ocupando disco sin fila que lo mencione.
    assert os.listdir(modulo.UPLOAD_DIR) == []


def test_sin_cuota_se_admite_cualquier_tamano(crear_app):
    modulo = crear_app(USER_QUOTA_MB=0)
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    entrar(cliente, "ana", "contrasena-larga")

    subir(cliente, "grande.bin", b"x" * (3 * 1024 * 1024))
    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        total = db.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        db.close()
    assert total == 1


def test_reserva_de_disco(crear_app, monkeypatch):
    """Un umbral imposible de cumplir simula el disco lleno."""
    modulo = crear_app(MIN_FREE_DISK_MB=999_999_999)
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    entrar(cliente, "ana", "contrasena-larga")

    respuesta = subir(cliente, "algo.txt", b"hola")
    assert respuesta.headers["Location"].endswith("/upload")
    assert os.listdir(modulo.UPLOAD_DIR) == []


def test_limite_de_subida_devuelve_413(crear_app):
    modulo = crear_app(MAX_UPLOAD_MB=1)
    assert modulo.app.config["MAX_CONTENT_LENGTH"] == 1024 * 1024

    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    entrar(cliente, "ana", "contrasena-larga")
    respuesta = subir(cliente, "grande.bin", b"x" * (2 * 1024 * 1024))
    assert respuesta.status_code == 413


# ── Borrado ───────────────────────────────────────────────────────────────────

def test_borrar_usuario_borra_sus_ficheros_del_disco(modulo):
    """El ON DELETE CASCADE se lleva las filas, pero no los ficheros: sin este
    borrado explícito se quedaban en /data/uploads para siempre."""
    crear_usuario(modulo, "jefe", "contrasena-larga", admin=True)
    id_ana = crear_usuario(modulo, "ana", "contrasena-larga")

    de_ana = modulo.app.test_client()
    entrar(de_ana, "ana", "contrasena-larga")
    subir(de_ana, "uno.txt", b"hola")
    subir(de_ana, "dos.txt", b"adios")
    assert len(os.listdir(modulo.UPLOAD_DIR)) == 2

    de_jefe = modulo.app.test_client()
    entrar(de_jefe, "jefe", "contrasena-larga")
    de_jefe.post("/admin", data={"action": "delete_user", "user_id": str(id_ana)})

    assert os.listdir(modulo.UPLOAD_DIR) == [], "quedaron ficheros huérfanos"


def test_borrar_usuario_con_id_no_numerico_no_revienta(modulo):
    """`int(user_id)` a pelo daba un 500 con cualquier valor no numérico."""
    crear_usuario(modulo, "jefe", "contrasena-larga", admin=True)
    cliente = modulo.app.test_client()
    entrar(cliente, "jefe", "contrasena-larga")

    respuesta = cliente.post(
        "/admin", data={"action": "delete_user", "user_id": "../../etc/passwd"}
    )
    assert respuesta.status_code == 302


def test_no_se_puede_borrar_a_un_administrador(modulo):
    crear_usuario(modulo, "jefe", "contrasena-larga", admin=True)
    id_otro = crear_usuario(modulo, "jefa2", "contrasena-larga", admin=True)

    cliente = modulo.app.test_client()
    entrar(cliente, "jefe", "contrasena-larga")
    cliente.post("/admin", data={"action": "delete_user", "user_id": str(id_otro)})

    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        sigue = db.execute("SELECT 1 FROM users WHERE id = ?", (id_otro,)).fetchone()
        db.close()
    assert sigue is not None


def test_borrar_archivo_lo_quita_del_disco(modulo):
    crear_usuario(modulo, "jefe", "contrasena-larga", admin=True)
    cliente = modulo.app.test_client()
    entrar(cliente, "jefe", "contrasena-larga")
    subir(cliente, "x.txt", b"hola")

    cliente.post("/delete/1")
    assert os.listdir(modulo.UPLOAD_DIR) == []


# ── Metadatos ─────────────────────────────────────────────────────────────────

def _png_con_metadatos():
    buf = io.BytesIO()
    imagen = Image.new("RGB", (8, 8), (120, 60, 30))
    imagen.save(buf, format="PNG")
    return buf.getvalue()


def test_quitar_metadatos_conserva_la_imagen(modulo):
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    entrar(cliente, "ana", "contrasena-larga")
    subir(cliente, "foto.png", _png_con_metadatos())

    cliente.post("/strip_metadata/1")

    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        fila = db.execute("SELECT stored_name FROM files WHERE id = 1").fetchone()
        db.close()
    ruta = os.path.join(modulo.UPLOAD_DIR, fila["stored_name"])
    with Image.open(ruta) as limpia:
        # El formato debe conservarse: al escribir en un temporal sin extensión,
        # Pillow no puede deducirlo y hay que pasárselo explícitamente.
        assert limpia.format == "PNG"
        assert limpia.size == (8, 8)


def test_un_fallo_al_limpiar_no_destruye_el_original(modulo, monkeypatch):
    """Antes se reescribía el fichero en el sitio: si Pillow fallaba a media
    escritura, el archivo del usuario quedaba corrupto sin remedio."""
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    entrar(cliente, "ana", "contrasena-larga")
    original = _png_con_metadatos()
    subir(cliente, "foto.png", original)

    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        fila = db.execute("SELECT stored_name FROM files WHERE id = 1").fetchone()
        db.close()
    ruta = os.path.join(modulo.UPLOAD_DIR, fila["stored_name"])

    def explota(*args, **kwargs):
        raise OSError("disco lleno a media escritura")

    monkeypatch.setattr(Image.Image, "save", explota)
    respuesta = cliente.post("/strip_metadata/1", follow_redirects=True)

    assert b"no se ha modificado" in respuesta.data.lower() or \
           "no se ha modificado" in respuesta.data.decode(errors="ignore")
    with open(ruta, "rb") as f:
        assert f.read() == original, "el original debería estar intacto"
    # Y el temporal no puede quedarse por ahí.
    assert not any(n.startswith(".limpiando_") for n in os.listdir(modulo.UPLOAD_DIR))
