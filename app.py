import configparser
import io
import logging
import mimetypes
import os
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import (
    Flask, request, redirect, url_for, render_template,
    send_from_directory, send_file, abort, flash, g, jsonify, session
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_wtf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from PIL import Image
from pypdf import PdfReader, PdfWriter

import ajustes
import tls

# Versión del código. Es la única fuente: docker-update.sh la lee de aquí
# para etiquetar la imagen que construye (porfolio de imágenes etiquetadas =
# poder volver atrás en segundos) y /api/health la devuelve para saber qué
# versión está realmente en marcha, no cuál crees que desplegaste.
__version__ = "1.0.0"

# Explicit allowlist of MIME types that are safe to render inline (never
# includes text/html, xml, or svg, which could execute script if previewed
# same-origin). The client-supplied upload Content-Type is never trusted for
# this decision — mime_type is always derived server-side from the filename.
SAFE_PREVIEW_MIME_TYPES = {
    "application/pdf",
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
    "image/tiff", "image/x-icon",
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/webm", "audio/aac", "audio/flac",
    "video/mp4", "video/webm", "video/ogg", "video/quicktime",
    "text/plain",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_DIR = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "app.db")

# La consola de Windows usa cp1252 por defecto, y ahí cualquier mensaje con un
# acento o una flecha hace que logging escupa un UnicodeEncodeError con su
# traza en mitad del arranque. En el contenedor no pasa (todo es UTF-8), pero
# ejecutando "python app.py" en Windows sí, y el error asusta más que el propio
# mensaje que intentaba imprimirse. errors="replace" además garantiza que un
# nombre de fichero raro en el log de acceso nunca tumbe una petición.
for _flujo in (sys.stdout, sys.stderr):
    try:
        _flujo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass  # no siempre es un flujo de texto reconfigurable

# Sin esto no se ve absolutamente nada por consola: waitress anuncia el
# "Serving on ..." con logging.info, y sin handlers configurados Python solo
# deja pasar WARNING o superior. Va a stdout para que "docker logs" lo recoja
# igual que cuando se ejecuta a mano con "python app.py".
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("httpWebServer")

# config.ini son los valores de fábrica: viaja con el código y se actualiza con
# él, así que editarlo en el servidor choca con cada git pull. El entorno (.env,
# que no se versiona) manda sobre él, y es ahí donde se configura una
# instalación concreta. Todos los ajustes de abajo siguen esa misma regla.
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
_config = configparser.ConfigParser()
_config.read(CONFIG_PATH)


def ajuste(seccion, clave, env, defecto):
    valor = os.environ.get(env, "").strip()
    if valor:
        return valor
    return _config.get(seccion, clave, fallback=str(defecto))


def ajuste_int(seccion, clave, env, defecto):
    valor = ajuste(seccion, clave, env, defecto)
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        log.warning("Valor no numérico para %s (%s); se usa %s", env, valor, defecto)
        return defecto


def ajuste_bool(seccion, clave, env, defecto):
    valor = str(ajuste(seccion, clave, env, defecto)).strip().lower()
    if valor in ("1", "true", "yes", "on", "si", "sí"):
        return True
    if valor in ("0", "false", "no", "off"):
        return False
    return bool(defecto)


SERVER_PORT = ajuste_int("server", "port", "PORT", 8000)

# ── Ajustes que se cambian desde la web ───────────────────────────────────────
# Límites de subida y política de acceso viven en ajustes.py y se leen en cada
# petición, no aquí: son los que el administrador puede cambiar desde
# Administración → Ajustes, y un valor capturado al arrancar dejaría a cada
# worker de gunicorn con una copia distinta según cuándo se guardó el cambio.
def limite_subida_mb():
    return ajustes.valor("max_upload_mb", DATA_DIR)


def cuota_usuario_mb():
    return ajustes.valor("user_quota_mb", DATA_DIR)


def disco_reservado_mb():
    return ajustes.valor("min_free_disk_mb", DATA_DIR)


def longitud_minima_password():
    return ajustes.valor("min_password_length", DATA_DIR)


def intentos_maximos_login():
    return ajustes.valor("login_max_attempts", DATA_DIR)


def ventana_login_minutos():
    return ajustes.valor("login_window_minutes", DATA_DIR)

# ── Seguridad ─────────────────────────────────────────────────────────────────
# behind_proxy activa dos cosas que solo son correctas detrás de un proxy con
# TLS: leer las cabeceras X-Forwarded-* (si no, el log y el bloqueo por intentos
# verían siempre la IP del proxy, y el bloqueo sería inútil) y marcar las
# cookies como Secure. Ponerlo a true sin proxy rompe el login; dejarlo a false
# detrás de uno deja las cookies viajando sin la marca Secure.
BEHIND_PROXY = ajuste_bool("security", "behind_proxy", "BEHIND_PROXY", False)

# HTTPS propio: el servidor sirve TLS él mismo con un certificado firmado por
# una CA local, sin proxy delante. Desactivado hasta que se active desde
# Administración → HTTPS, porque activarlo sin haber instalado antes la CA en
# los dispositivos deja a todo el mundo con un aviso rojo.
#
# El estado NO vive en config.ini ni en .env: ninguno de los dos se puede
# escribir desde dentro del contenedor. Vive en el volumen de datos, junto a
# los propios certificados, que es lo que también mira docker-entrypoint.sh
# para decidir si arrancar gunicorn con TLS.
HTTPS_ACTIVO = tls.esta_activo(DATA_DIR)

_secure_cookies = str(ajuste("security", "secure_cookies", "SECURE_COOKIES", "auto")).lower()
SECURE_COOKIES = (
    (BEHIND_PROXY or HTTPS_ACTIVO) if _secure_cookies == "auto"
    else _secure_cookies in ("1", "true", "yes", "on", "si", "sí")
)


# Segundos que una petición espera a que se libere la base de datos antes de
# rendirse. Con subidas grandes y varios workers, 5 s (el valor por defecto de
# sqlite3) se queda corto.
DB_TIMEOUT_S = ajuste_int("server", "db_timeout_seconds", "DB_TIMEOUT_SECONDS", 30)

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    # Persist a generated key to disk so sessions (and "remember me" logins)
    # survive process restarts instead of being invalidated every time.
    _secret_key_path = os.path.join(DATA_DIR, "secret_key")
    if os.path.exists(_secret_key_path):
        with open(_secret_key_path, "r") as f:
            _secret_key = f.read().strip()
    if not _secret_key:
        _secret_key = secrets.token_hex(32)
        with open(_secret_key_path, "w") as f:
            f.write(_secret_key)
app.secret_key = _secret_key

# Detrás de un proxy con TLS, sin esto request.remote_addr sería siempre la IP
# del proxy: el log de acceso no distinguiría a nadie y el bloqueo por intentos
# fallidos contaría todos los intentos del mundo en el mismo cubo, con lo que o
# no bloquea nunca o bloquea a todos a la vez. x_for=1 porque se confía en UN
# salto: el proxy propio, que reescribe la cabecera.
if BEHIND_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)
# La cookie de sesión y la de "recuérdame" no las necesita nunca JavaScript, y
# SameSite=Lax evita que un sitio de terceros dispare peticiones autenticadas.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
# Secure impide que el navegador mande la cookie por HTTP en claro. Solo se
# puede activar si hay TLS delante: sobre HTTP pelado el navegador nunca
# devolvería la cookie y el login quedaría en un bucle sin explicación. De ahí
# que el valor por defecto sea "auto" = lo mismo que behind_proxy.
app.config["SESSION_COOKIE_SECURE"] = SECURE_COOKIES
app.config["REMEMBER_COOKIE_SECURE"] = SECURE_COOKIES

if not SECURE_COOKIES:
    log.warning(
        "Cookies SIN marca Secure: la sesión y la contraseña viajan en claro. "
        "Correcto solo en una LAN de confianza. Para cifrarlo: activa el HTTPS "
        "desde Administración → HTTPS (certificado propio, sin salir de la LAN), "
        "o pon un proxy con TLS delante y BEHIND_PROXY=true en .env."
    )

login_manager = LoginManager(app)
login_manager.login_view = "login"
# "basic" (por defecto) y no "strong": en LAN la IP del cliente cambia al
# saltar de wifi a cable y "strong" cerraría la sesión en cada cambio.
login_manager.session_protection = "basic"
login_manager.login_message = "Debes iniciar sesión para acceder a esta página."
login_manager.login_message_category = "error"
csrf = CSRFProtect(app)


@app.before_request
def make_session_permanent():
    session.permanent = True
    g.request_started_at = time.monotonic()
    # Se sincroniza aquí y no al arrancar porque el límite se cambia desde la
    # web: Werkzeug lo consulta al parsear el cuerpo de la petición, que ocurre
    # después de este gancho, así que cambiarlo ahora ya cuenta para esta misma
    # subida y para todos los workers por igual.
    mb = limite_subida_mb()
    app.config["MAX_CONTENT_LENGTH"] = mb * 1024 * 1024 if mb > 0 else None


@app.after_request
def no_store_html(response):
    # Sin esto el navegador guarda el HTML (y su bfcache) y el botón "atrás"
    # desde el menú principal vuelve a pintar el login ya usado. Solo afecta a
    # las páginas; descargas y vistas previas siguen siendo cacheables.
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# Las plantillas no llevan ni un solo <script> en línea ni un onclick: todo el
# JavaScript vive en static/app.js. Eso permite prohibir el script en línea sin
# 'unsafe-inline', que es lo único que convierte a la CSP en una defensa real
# contra XSS en vez de en un adorno.
CSP_PAGINAS = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "object-src 'self'; "      # el <embed> del visor de PDF
    "frame-src 'self'; "       # el <iframe> de la vista previa de texto
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'self'"
)

# Los ficheros que sirven /download y /preview son contenido subido por
# usuarios. Aquí la respuesta ES el fichero, así que se le prohíbe cargar
# absolutamente nada: si algún día se cuela un tipo previsualizable capaz de
# ejecutar algo, no tendrá con qué llamar a casa.
CSP_FICHEROS = (
    "default-src 'none'; "
    "img-src 'self'; "
    "media-src 'self'; "
    "style-src 'unsafe-inline'; "
    "frame-ancestors 'self'"
)


@app.after_request
def security_headers(response):
    response.headers.setdefault("Content-Security-Policy", CSP_PAGINAS)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # El servidor no se embebe en sitios de terceros; frame-ancestors ya lo dice
    # para navegadores modernos y X-Frame-Options cubre a los que no.
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    # Sin esto, al pinchar el enlace del pie se filtraría la URL interna del
    # servidor (y con ella el nombre del fichero) al sitio de destino.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=(), interest-cohort=()"
    )
    if SECURE_COOKIES:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.errorhandler(413)
def upload_demasiado_grande(error):
    # Sin este manejador, pasarse del límite devuelve la página de error cruda
    # de Werkzeug a mitad de subida y el usuario no llega a saber por qué.
    flash(
        f"El archivo supera el límite de subida ({limite_subida_mb()} MB por petición).",
        "error",
    )
    return redirect(url_for("upload" if current_user.is_authenticated else "index")), 413


@app.after_request
def log_request(response):
    # Log de acceso tipo servidor web: ni waitress ni gunicorn (sin
    # --access-logfile) registran las peticiones por su cuenta.
    if request.path.startswith("/static/"):
        return response
    started = g.pop("request_started_at", None)
    took_ms = (time.monotonic() - started) * 1000 if started else 0
    user = current_user.username if current_user.is_authenticated else "anon"
    log.info(
        "%s %s %s -> %s (%s, %.0f ms)",
        request.remote_addr,
        request.method,
        request.full_path.rstrip("?"),
        response.status_code,
        user,
        took_ms,
    )
    return response


def conectar(ruta):
    """Conexión con los PRAGMA que hacen falta para servir con varios workers.

    En modo journal clásico un escritor bloquea a TODOS los lectores, así que
    con los 2 workers de gunicorn bastaba una subida en curso para que otra
    petición muriera con «database is locked». WAL deja que lectores y escritor
    convivan, y busy_timeout convierte el choque entre dos escritores en una
    espera corta en vez de un error inmediato.
    """
    conn = sqlite3.connect(ruta, timeout=DB_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = %d" % (DB_TIMEOUT_S * 1000))
    return conn


def get_db():
    if "db" not in g:
        g.db = conectar(DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = conectar(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            must_change_password INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE,
            uploader_id INTEGER NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'private', -- 'public' or 'private'
            uploaded_at TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            FOREIGN KEY (uploader_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS file_shares (
            file_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (file_id, user_id),
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- Intentos de login fallidos, para poder frenar la fuerza bruta. Va en
        -- la base de datos y no en memoria a propósito: con varios workers de
        -- gunicorn, un contador en memoria cuenta por proceso y el atacante solo
        -- tiene que repartir los intentos para no llegar nunca al límite.
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            attempted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_login_attempts
            ON login_attempts (ip, attempted_at);
        """
    )
    existing_columns = {row["name"] for row in db.execute("PRAGMA table_info(files)")}
    added_metadata_columns = False
    if "size_bytes" not in existing_columns:
        db.execute("ALTER TABLE files ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0")
        added_metadata_columns = True
    if "mime_type" not in existing_columns:
        db.execute("ALTER TABLE files ADD COLUMN mime_type TEXT NOT NULL DEFAULT 'application/octet-stream'")
        added_metadata_columns = True
    if added_metadata_columns:
        for row in db.execute("SELECT id, original_name, stored_name FROM files"):
            file_path = os.path.join(UPLOAD_DIR, row["stored_name"])
            if os.path.exists(file_path):
                size_bytes = os.path.getsize(file_path)
                mime_type = mimetypes.guess_type(row["original_name"])[0] or "application/octet-stream"
                db.execute(
                    "UPDATE files SET size_bytes = ?, mime_type = ? WHERE id = ?",
                    (size_bytes, mime_type, row["id"]),
                )
    db.commit()
    db.close()


def ahora():
    """Instante actual con zona horaria.

    `datetime.utcnow()` está deprecado en 3.12 y además devolvía un datetime
    "ingenuo": la cadena guardada no decía en qué huso estaba, así que no había
    forma de interpretarla sin saberlo de memoria.
    """
    return datetime.now(timezone.utc)


# No pretende ser una lista exhaustiva —para eso hace falta un diccionario de
# millones de entradas—, solo cortar lo que aparece en el primer intento de
# cualquier ataque automatizado.
CONTRASENAS_COMUNES = {
    "123456", "123456789", "12345678", "1234567890", "12345", "1234567",
    "password", "password1", "password123", "qwerty", "qwerty123", "abc123",
    "111111", "000000", "iloveyou", "admin", "administrador", "administrator",
    "letmein", "welcome", "monkey", "dragon", "sunshine", "princess",
    "contrasena", "contraseña", "contrasena1", "1q2w3e4r", "qwertyuiop",
    "servidor", "usuario", "root", "toor", "test", "invitado", "guest",
    "asdasd", "asdfgh", "zxcvbnm", "666666", "654321", "123123", "888888",
    "barcelona", "realmadrid", "cambiame", "changeme",
}


def validar_password(password, username=""):
    """Devuelve el motivo del rechazo, o None si la contraseña es aceptable."""
    minimo = longitud_minima_password()
    if len(password) < minimo:
        return f"La contraseña debe tener al menos {minimo} caracteres."
    if password.lower() in CONTRASENAS_COMUNES:
        return "Esa contraseña es de las primeras que prueba cualquier ataque. Elige otra."
    if username and password.lower() == username.lower():
        return "La contraseña no puede ser igual al nombre de usuario."
    if len(set(password)) < 5:
        return "La contraseña repite demasiado los mismos caracteres."
    return None


def ip_cliente():
    return request.remote_addr or "desconocida"


def segundos_de_bloqueo(ip):
    """Segundos que faltan para que esta IP pueda volver a intentarlo, o 0.

    Se cuenta por IP y no por usuario a propósito: bloquear por nombre de
    usuario deja que cualquiera deje fuera a otra persona sin más que fallar su
    contraseña unas cuantas veces.
    """
    db = get_db()
    ventana = ventana_login_minutos()
    limite = (ahora() - timedelta(minutes=ventana)).isoformat()
    # Los intentos fuera de la ventana ya no cuentan para nada: se borran aquí
    # y así la tabla no crece sin fin sin necesidad de una tarea aparte.
    db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (limite,))
    db.commit()
    filas = db.execute(
        "SELECT attempted_at FROM login_attempts WHERE ip = ? ORDER BY attempted_at",
        (ip,),
    ).fetchall()
    if len(filas) < intentos_maximos_login():
        return 0
    # El bloqueo se levanta cuando el intento más antiguo salga de la ventana.
    try:
        mas_antiguo = datetime.fromisoformat(filas[0]["attempted_at"])
    except ValueError:
        return 0
    libre_en = mas_antiguo + timedelta(minutes=ventana)
    return max(0, int((libre_en - ahora()).total_seconds()))


def registrar_intento_fallido(ip, username):
    db = get_db()
    db.execute(
        "INSERT INTO login_attempts (ip, username, attempted_at) VALUES (?, ?, ?)",
        (ip, username[:150], ahora().isoformat()),
    )
    db.commit()


def limpiar_intentos(ip):
    db = get_db()
    db.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
    db.commit()


def espacio_libre_mb():
    """MB libres en el volumen de subidas, o None si no se puede saber."""
    try:
        return shutil.disk_usage(UPLOAD_DIR).free // (1024 * 1024)
    except OSError:
        return None


def uso_del_usuario_mb(user_id):
    fila = get_db().execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM files WHERE uploader_id = ?",
        (user_id,),
    ).fetchone()
    return fila["total"] / (1024 * 1024)


def borrar_de_disco(stored_name):
    """Borra un fichero subido sin reventar si ya no está.

    Que falte no es un error: puede haberse borrado a mano, o haber fallado una
    subida a medias. Lo que sí importa es que un fallo aquí no deje la petición
    a medio hacer cuando la fila ya se ha borrado de la base de datos.
    """
    ruta = os.path.join(UPLOAD_DIR, stored_name)
    try:
        os.remove(ruta)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("No se pudo borrar %s: %s", ruta, exc)
        return False


def human_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def is_previewable(mime_type):
    return mime_type in SAFE_PREVIEW_MIME_TYPES


def supports_metadata_removal(original_name, mime_type):
    ext = os.path.splitext(original_name)[1].lower()
    return ext in IMAGE_EXTENSIONS or mime_type == "application/pdf"


def admin_exists():
    db = get_db()
    row = db.execute("SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
    return row is not None


@app.before_request
def require_initial_setup():
    if request.endpoint in ("setup", "static", "health", "descargar_ca"):
        return
    if not admin_exists():
        return redirect(url_for("setup"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if admin_exists():
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        motivo = validar_password(password, username)
        if not username:
            flash("Debes indicar un nombre de usuario.", "error")
        elif motivo:
            flash(motivo, "error")
        elif password != confirm_password:
            flash("Las contraseñas no coinciden.", "error")
        else:
            db = get_db()
            cur = db.execute(
                "INSERT INTO users (username, password_hash, is_admin, must_change_password) "
                "VALUES (?, ?, 1, 0)",
                (username, generate_password_hash(password)),
            )
            db.commit()
            # Quien acaba de teclear la contraseña no tiene por qué volver a
            # escribirla: se entra directamente con la cuenta recién creada.
            row = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
            login_user(User(row), remember=True)
            flash("Cuenta de administrador creada. Sesión iniciada.", "success")
            return redirect(url_for("index"))
    return render_template("setup.html")


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.is_admin = bool(row["is_admin"])
        self.must_change_password = bool(row["must_change_password"])


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User(row) if row else None


def visible_files_for_current_user():
    db = get_db()
    if current_user.is_authenticated:
        if current_user.is_admin:
            rows = db.execute(
                """
                SELECT f.*, u.username AS uploader_name
                FROM files f JOIN users u ON f.uploader_id = u.id
                ORDER BY f.uploaded_at DESC
                """
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT DISTINCT f.*, u.username AS uploader_name
                FROM files f
                JOIN users u ON f.uploader_id = u.id
                LEFT JOIN file_shares s ON s.file_id = f.id
                WHERE f.visibility = 'public'
                   OR f.uploader_id = ?
                   OR s.user_id = ?
                ORDER BY f.uploaded_at DESC
                """,
                (current_user.id, current_user.id),
            ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT f.*, u.username AS uploader_name
            FROM files f JOIN users u ON f.uploader_id = u.id
            WHERE f.visibility = 'public'
            ORDER BY f.uploaded_at DESC
            """
        ).fetchall()
    return rows


def can_access_file(file_row):
    if file_row["visibility"] == "public":
        return True
    if not current_user.is_authenticated:
        return False
    if current_user.is_admin or current_user.id == file_row["uploader_id"]:
        return True
    db = get_db()
    shared = db.execute(
        "SELECT 1 FROM file_shares WHERE file_id = ? AND user_id = ?",
        (file_row["id"], current_user.id),
    ).fetchone()
    return shared is not None


@app.context_processor
def template_helpers():
    return dict(
        human_size=human_size,
        is_previewable=is_previewable,
        supports_metadata_removal=supports_metadata_removal,
        min_password_length=longitud_minima_password(),
        # Se pinta en el pie de todas las páginas. Saber a simple vista qué
        # versión está sirviendo evita la duda de si una actualización llegó a
        # aplicarse, sin tener que entrar al servidor a mirarlo.
        version=__version__,
    )


@app.route("/")
def index():
    files = visible_files_for_current_user()
    return render_template("index.html", files=files)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        # Volver al login con una sesión abierta (típicamente con el botón
        # "atrás") no debe mostrar el formulario otra vez.
        return redirect(url_for("index"))
    ip = ip_cliente()
    if request.method == "POST":
        # El bloqueo se comprueba antes de tocar la base de datos de usuarios:
        # así un ataque por fuerza bruta ni siquiera llega a gastar el tiempo de
        # comprobar el hash, que es la parte cara.
        espera = segundos_de_bloqueo(ip)
        if espera > 0:
            log.warning("Login bloqueado para %s (%d s restantes)", ip, espera)
            flash(
                f"Demasiados intentos fallidos. Vuelve a intentarlo en "
                f"{max(1, espera // 60)} minuto(s).",
                "error",
            )
            return render_template("login.html"), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            limpiar_intentos(ip)
            login_user(User(row), remember=True)
            if row["must_change_password"]:
                return redirect(url_for("change_password"))
            return redirect(url_for("index"))
        registrar_intento_fallido(ip, username)
        # El mismo mensaje tanto si el usuario no existe como si la contraseña
        # falla: distinguirlos le diría a un atacante qué nombres son válidos.
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE id = ?", (current_user.id,)).fetchone()
        motivo = validar_password(new_password, current_user.username)
        if not check_password_hash(row["password_hash"], current_password):
            flash("La contraseña actual no es correcta.", "error")
        elif motivo:
            flash(motivo, "error")
        elif new_password != confirm_password:
            flash("Las contraseñas nuevas no coinciden.", "error")
        elif new_password == current_password:
            flash("La nueva contraseña debe ser distinta de la actual.", "error")
        else:
            db.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                (generate_password_hash(new_password), current_user.id),
            )
            db.commit()
            flash("Contraseña actualizada correctamente.", "success")
            return redirect(url_for("index"))
    return render_template("change_password.html")


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    db = get_db()
    if request.method == "POST":
        uploaded_files = [f for f in request.files.getlist("files") if f and f.filename]
        visibility_choice = request.form.get("visibility", "private_me")
        selected_users = request.form.getlist("shared_users")

        if not uploaded_files:
            flash("Debes seleccionar al menos un archivo.", "error")
            return redirect(url_for("upload"))

        # Antes de aceptar nada: si el disco ya está al límite, mejor decirlo
        # que empezar a escribir y dejar el volumen sin sitio ni para que la
        # base de datos pueda registrar lo que acaba de pasar.
        libre = espacio_libre_mb()
        reserva = disco_reservado_mb()
        if libre is not None and libre <= reserva:
            log.error("Subida rechazada: solo quedan %s MB libres", libre)
            flash(
                "No hay espacio suficiente en el servidor. Avisa al administrador.",
                "error",
            )
            return redirect(url_for("upload"))

        visibility = "public" if visibility_choice == "public" else "private"
        uploaded_at = ahora().isoformat()
        count = 0
        interrumpido = None

        for uploaded_file in uploaded_files:
            original_name = secure_filename(uploaded_file.filename)
            if not original_name:
                continue
            stored_name = f"{uuid.uuid4().hex}_{original_name}"
            dest_path = os.path.join(UPLOAD_DIR, stored_name)
            uploaded_file.save(dest_path)
            size_bytes = os.path.getsize(dest_path)

            # El tamaño real solo se conoce cuando el fichero ya está escrito
            # (el navegador no lo anuncia de forma fiable), así que la cuota y
            # la reserva de disco se comprueban aquí y se deshace lo escrito si
            # se han pasado. Deshacerlo es lo que evita que un rechazo deje
            # basura ocupando sitio.
            libre = espacio_libre_mb()
            if libre is not None and libre <= reserva:
                borrar_de_disco(stored_name)
                interrumpido = (
                    f"«{original_name}» no cabe: el servidor debe mantener "
                    f"{reserva} MB libres."
                )
                break

            cuota = cuota_usuario_mb()
            if cuota > 0:
                usado = uso_del_usuario_mb(current_user.id) + size_bytes / (1024 * 1024)
                if usado > cuota:
                    borrar_de_disco(stored_name)
                    interrumpido = (
                        f"«{original_name}» supera tu cuota de {cuota} MB. "
                        "Borra algún archivo antes de subir más."
                    )
                    break
            # Never trust the client-supplied Content-Type for the stored mime_type:
            # it drives what gets rendered inline in the browser (see is_previewable),
            # so it must be derived server-side from the filename instead.
            mime_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"

            cur = db.execute(
                "INSERT INTO files (original_name, stored_name, uploader_id, visibility, uploaded_at, size_bytes, mime_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (original_name, stored_name, current_user.id, visibility, uploaded_at, size_bytes, mime_type),
            )
            file_id = cur.lastrowid
            count += 1

            if visibility == "private" and visibility_choice == "private_select":
                for uid in selected_users:
                    if uid.isdigit() and int(uid) != current_user.id:
                        db.execute(
                            "INSERT OR IGNORE INTO file_shares (file_id, user_id) VALUES (?, ?)",
                            (file_id, int(uid)),
                        )
        db.commit()
        if interrumpido:
            # Lo ya guardado se queda: rehacer una subida entera porque el
            # último fichero no cabía sería peor que decir exactamente dónde se
            # cortó.
            if count:
                flash(f"Se subieron {count} archivo(s). {interrumpido}", "error")
            else:
                flash(interrumpido, "error")
            return redirect(url_for("index"))
        if count:
            flash(f"{count} archivo(s) subido(s) correctamente.", "success")
        else:
            flash("No se pudo subir ningún archivo.", "error")
        return redirect(url_for("index"))

    users = db.execute(
        "SELECT id, username FROM users WHERE id != ? ORDER BY username", (current_user.id,)
    ).fetchall()
    return render_template("upload.html", users=users)


@app.route("/download/<int:file_id>")
def download(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        abort(404)
    if not can_access_file(row):
        abort(403)
    response = send_from_directory(
        UPLOAD_DIR, row["stored_name"], as_attachment=True, download_name=row["original_name"]
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = CSP_FICHEROS
    return response


@app.route("/preview/<int:file_id>")
def preview(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        abort(404)
    if not can_access_file(row):
        abort(403)
    if not is_previewable(row["mime_type"]):
        abort(415)
    response = send_from_directory(
        UPLOAD_DIR, row["stored_name"], as_attachment=False, mimetype=row["mime_type"]
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    # La CSP de las páginas dejaría que este contenido —subido por un usuario—
    # cargara recursos del propio origen. Aquí la respuesta ES el fichero, así
    # que no necesita cargar nada.
    response.headers["Content-Security-Policy"] = CSP_FICHEROS
    return response


def _can_manage_file(row):
    return current_user.is_authenticated and (
        current_user.is_admin or current_user.id == row["uploader_id"]
    )


@app.route("/permissions/<int:file_id>", methods=["GET", "POST"])
@login_required
def file_permissions(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        abort(404)
    if not _can_manage_file(row):
        abort(403)

    if request.method == "POST":
        visibility_choice = request.form.get("visibility", "private_me")
        selected_users = request.form.getlist("shared_users")
        visibility = "public" if visibility_choice == "public" else "private"

        db.execute("UPDATE files SET visibility = ? WHERE id = ?", (visibility, file_id))
        # Los permisos individuales se reescriben por completo en cada guardado:
        # así desmarcar a alguien le retira el acceso.
        db.execute("DELETE FROM file_shares WHERE file_id = ?", (file_id,))
        if visibility == "private" and visibility_choice == "private_select":
            for uid in selected_users:
                if uid.isdigit() and int(uid) != row["uploader_id"]:
                    db.execute(
                        "INSERT OR IGNORE INTO file_shares (file_id, user_id) VALUES (?, ?)",
                        (file_id, int(uid)),
                    )
        db.commit()
        flash("Permisos actualizados correctamente.", "success")
        return redirect(url_for("index"))

    users = db.execute(
        "SELECT id, username FROM users WHERE id != ? ORDER BY username", (row["uploader_id"],)
    ).fetchall()
    shared_ids = {
        r["user_id"]
        for r in db.execute("SELECT user_id FROM file_shares WHERE file_id = ?", (file_id,))
    }
    if row["visibility"] == "public":
        current_choice = "public"
    elif shared_ids:
        current_choice = "private_select"
    else:
        current_choice = "private_me"
    return render_template(
        "file_permissions.html",
        file=row,
        users=users,
        shared_ids=shared_ids,
        current_choice=current_choice,
    )


@app.route("/strip_metadata/<int:file_id>", methods=["POST"])
@login_required
def strip_metadata(file_id):
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        abort(404)
    if not _can_manage_file(row):
        abort(403)

    file_path = os.path.join(UPLOAD_DIR, row["stored_name"])
    ext = os.path.splitext(row["original_name"])[1].lower()

    if ext not in IMAGE_EXTENSIONS and row["mime_type"] != "application/pdf":
        flash("Este tipo de archivo no soporta eliminación de metadatos.", "error")
        return redirect(url_for("index"))

    # Se escribe en un temporal del MISMO directorio y solo al final se sustituye
    # el original con os.replace, que es atómico. Antes se reescribía el fichero
    # en el sitio: si Pillow o pypdf fallaban a media escritura —un JPEG con el
    # final truncado, un PDF cifrado—, el archivo del usuario quedaba destruido
    # y el mensaje de error llegaba cuando ya no había nada que salvar.
    # Mismo directorio porque os.replace solo es atómico dentro de un sistema de
    # ficheros, y /tmp puede estar en otro.
    tmp_fd, tmp_path = tempfile.mkstemp(dir=UPLOAD_DIR, prefix=".limpiando_")
    os.close(tmp_fd)
    try:
        if ext in IMAGE_EXTENSIONS:
            with Image.open(file_path) as image:
                formato = image.format
                data = list(image.getdata())
                clean_image = Image.new(image.mode, image.size)
                if image.mode == "P":
                    clean_image.putpalette(image.getpalette())
                clean_image.putdata(data)
                # Sin format explícito, Pillow lo deduce de la extensión del
                # destino, y el temporal no tiene ninguna.
                clean_image.save(tmp_path, format=formato)
        else:
            reader = PdfReader(file_path)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            writer.add_metadata({})
            with open(tmp_path, "wb") as f:
                writer.write(f)
        os.replace(tmp_path, file_path)
    except Exception as exc:
        log.warning("Fallo al limpiar metadatos de %s: %s", row["stored_name"], exc)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        flash(
            "No se pudieron eliminar los metadatos de este archivo. "
            "El original no se ha modificado.",
            "error",
        )
        return redirect(url_for("index"))

    new_size = os.path.getsize(file_path)
    db.execute("UPDATE files SET size_bytes = ? WHERE id = ?", (new_size, file_id))
    db.commit()
    flash("Metadatos eliminados correctamente.", "success")
    return redirect(url_for("index"))


@app.route("/delete/<int:file_id>", methods=["POST"])
@login_required
def delete_file(file_id):
    if not current_user.is_admin:
        abort(403)
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        abort(404)
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))
    db.commit()
    borrar_de_disco(row["stored_name"])
    flash("Archivo eliminado.", "success")
    return redirect(url_for("index"))


@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    if not current_user.is_admin:
        abort(403)
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create_user":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            motivo = validar_password(password, username)
            if not username:
                flash("Debes indicar un nombre de usuario.", "error")
            elif motivo:
                flash(motivo, "error")
            else:
                try:
                    db.execute(
                        "INSERT INTO users (username, password_hash, is_admin, must_change_password) "
                        "VALUES (?, ?, 0, 1)",
                        (username, generate_password_hash(password)),
                    )
                    db.commit()
                    flash(f"Usuario '{username}' creado. Deberá cambiar la contraseña al iniciar sesión.", "success")
                except sqlite3.IntegrityError:
                    flash("Ese nombre de usuario ya existe.", "error")
        elif action == "reset_password":
            user_id = request.form.get("user_id", "")
            new_password = request.form.get("new_password", "")
            target = None
            if user_id.isdigit():
                target = db.execute(
                    "SELECT * FROM users WHERE id = ?", (int(user_id),)
                ).fetchone()
            motivo = validar_password(new_password, target["username"] if target else "")
            if not target:
                flash("Usuario no encontrado.", "error")
            elif target["id"] == current_user.id:
                flash("Para cambiar tu propia contraseña usa 'Cambiar contraseña'.", "error")
            elif motivo:
                flash(motivo, "error")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
                    (generate_password_hash(new_password), target["id"]),
                )
                db.commit()
                flash(
                    f"Contraseña de '{target['username']}' actualizada. "
                    "Deberá cambiarla en su próximo inicio de sesión.",
                    "success",
                )
        elif action == "delete_user":
            # `int(user_id)` a pelo daba un 500 con cualquier valor no numérico;
            # el resto de acciones ya usaban isdigit().
            user_id = request.form.get("user_id", "")
            if not user_id.isdigit():
                flash("Usuario no encontrado.", "error")
            elif int(user_id) == current_user.id:
                flash("No puedes eliminar tu propia cuenta.", "error")
            else:
                # Los ficheros del usuario se borran del disco a mano: el ON
                # DELETE CASCADE se lleva las filas de `files`, pero deja los
                # archivos en /data/uploads ocupando sitio para siempre y sin
                # ninguna fila que los mencione, así que nadie los encuentra ya.
                # Se leen ANTES de borrar, que es cuando todavía se sabe cuáles
                # son.
                huerfanos = [
                    r["stored_name"]
                    for r in db.execute(
                        "SELECT stored_name FROM files WHERE uploader_id = ?",
                        (int(user_id),),
                    )
                ]
                cur = db.execute(
                    "DELETE FROM users WHERE id = ? AND is_admin = 0", (int(user_id),)
                )
                db.commit()
                if cur.rowcount:
                    borrados = sum(1 for n in huerfanos if borrar_de_disco(n))
                    log.info(
                        "Usuario %s eliminado; %d de %d archivos borrados del disco",
                        user_id, borrados, len(huerfanos),
                    )
                    flash(
                        f"Usuario eliminado junto con {len(huerfanos)} archivo(s).",
                        "success",
                    )
                else:
                    flash("No se puede eliminar a un administrador.", "error")
        return redirect(url_for("admin"))

    users = db.execute("SELECT * FROM users ORDER BY is_admin DESC, username").fetchall()
    return render_template("admin.html", users=users)


@app.route("/api/health")
def health():
    """Comprobación de despliegue: "el contenedor está arriba" no es lo mismo
    que "la aplicación funciona". Por eso consulta de verdad la base de datos y
    el directorio de subidas, que es lo que se rompe al actualizar (volumen mal
    montado, permisos del volumen a nombre de otro usuario, esquema a medias).
    Devuelve 503 si algo falla, para que docker-update.sh pueda distinguirlo.
    """
    checks = {}
    ok = True

    try:
        get_db().execute("SELECT COUNT(*) FROM users").fetchone()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc.__class__.__name__}"
        ok = False

    # El fallo clásico tras actualizar: el volumen existe pero el proceso ya no
    # puede escribir en él, y no se nota hasta que alguien intenta subir algo.
    if os.path.isdir(UPLOAD_DIR) and os.access(UPLOAD_DIR, os.W_OK):
        checks["uploads"] = "ok"
    else:
        checks["uploads"] = "error: no se puede escribir"
        ok = False

    payload = {
        "status": "ok" if ok else "error",
        "version": __version__,
        # docker-update.sh lo consulta para saber si sondear por http o https:
        # con TLS activo, una comprobación en http no obtiene respuesta y daría
        # por muerta una versión que funciona.
        "https": HTTPS_ACTIVO,
        "checks": checks,
    }
    return jsonify(payload), (200 if ok else 503)


@app.route("/ca.crt")
def descargar_ca():
    """El certificado de la CA, para instalarlo en el PC y en el móvil.

    Es público a propósito, sin sesión: es la única forma de que un dispositivo
    nuevo pueda confiar en el servidor. No hay nada que proteger — un
    certificado de CA es la parte pública, y el servidor lo manda entero en
    cada handshake TLS. Lo que jamás se sirve es ca.key, que está en el mismo
    directorio y es lo que permitiría suplantar al servidor.

    Se sirve también por HTTPS aunque el navegador todavía no confíe: en un
    móvil nuevo se acepta el aviso una vez, se instala la CA, y a partir de ahí
    el aviso desaparece para siempre.
    """
    if not tls.estado(DATA_DIR)["hay_ca"]:
        abort(404)
    respuesta = app.response_class(
        tls.leer_ca_pem(DATA_DIR),
        # Este tipo MIME es lo que hace que Android ofrezca instalarlo como
        # certificado y que Safari lo trate como perfil de configuración. Con
        # application/octet-stream se descarga como un fichero cualquiera y el
        # usuario se queda sin saber qué hacer con él.
        mimetype="application/x-x509-ca-cert",
    )
    # inline y no attachment: attachment hace que Safari lo guarde en Archivos
    # en vez de ofrecer la instalación del perfil.
    respuesta.headers["Content-Disposition"] = 'inline; filename="servidor-archivos-ca.crt"'
    respuesta.headers["Cache-Control"] = "no-store"
    return respuesta


@app.route("/admin/ajustes", methods=["GET", "POST"])
@login_required
def admin_ajustes():
    """Los ajustes que se pueden cambiar sin entrar al servidor.

    Lo que se guarda va al volumen de datos, no a config.ini ni a .env: el
    primero se monta en solo lectura y el segundo ni siquiera está dentro del
    contenedor, así que son los dos únicos sitios donde la aplicación NO puede
    escribir.
    """
    if not current_user.is_admin:
        abort(403)

    if request.method == "POST":
        accion = request.form.get("accion", "guardar")

        if accion == "restablecer":
            ajustes.restablecer(DATA_DIR)
            flash("Ajustes restablecidos a los valores de fábrica.", "success")
            return redirect(url_for("admin_ajustes"))

        if accion in ("activar_https", "desactivar_https"):
            try:
                if accion == "activar_https":
                    nombres = [request.host]
                    nombres += request.form.get("nombres_https", "").replace(",", " ").split()
                    estado = tls.activar(DATA_DIR, nombres)
                    log.info("HTTPS activado; el certificado cubre %s", estado["nombres"])
                    flash(
                        "HTTPS preparado. Ahora descarga el certificado, instálalo "
                        "en tus dispositivos y reinicia el servidor.",
                        "success",
                    )
                    return redirect(url_for("admin_https"))
                tls.desactivar(DATA_DIR)
                flash(
                    "HTTPS desactivado. Reinicia el servidor para volver a HTTP. "
                    "Los certificados se conservan por si lo reactivas.",
                    "success",
                )
            except Exception as exc:
                log.exception("Fallo al cambiar el estado del HTTPS")
                flash(f"No se pudo cambiar el HTTPS: {exc}", "error")
            return redirect(url_for("admin_ajustes"))

        # Guardar los valores numéricos. Se validan TODOS antes de escribir
        # nada: guardar la mitad dejaría una configuración a medias y el
        # usuario no sabría cuál se aplicó y cuál no.
        cambios = {}
        errores = []
        for clave, definicion in ajustes.DEFINICIONES.items():
            if ajustes.fijado_en_entorno(clave):
                continue      # el campo va bloqueado; ni se mira lo que llegue
            if clave not in request.form:
                continue
            numero, error = ajustes.validar(clave, request.form[clave])
            if error:
                errores.append(error)
            else:
                cambios[clave] = numero

        if errores:
            for error in errores:
                flash(error, "error")
        elif cambios:
            ajustes.guardar(DATA_DIR, cambios)
            log.info("Ajustes actualizados: %s", cambios)
            flash("Ajustes guardados. Se aplican al momento.", "success")
        return redirect(url_for("admin_ajustes"))

    estado_tls = tls.estado(DATA_DIR)
    campos = []
    for clave, definicion in ajustes.DEFINICIONES.items():
        campos.append({
            "clave": clave,
            "valor": ajustes.valor(clave, DATA_DIR),
            "origen": ajustes.origen(clave, DATA_DIR),
            "bloqueado": ajustes.fijado_en_entorno(clave),
            **definicion,
        })

    return render_template(
        "ajustes.html",
        grupos=ajustes.GRUPOS,
        campos=campos,
        estado_tls=estado_tls,
        https_activo=HTTPS_ACTIVO,
        https_en_uso=request.is_secure,
        nombres_sugeridos=" ".join(
            dict.fromkeys([request.host] + tls.nombres_del_sistema())
        ),
    )


@app.route("/admin/https", methods=["GET", "POST"])
@login_required
def admin_https():
    if not current_user.is_admin:
        abort(403)

    if request.method == "POST":
        accion = request.form.get("accion")
        # Los nombres que tendrá que cubrir el certificado. El que de verdad
        # importa se conoce sin preguntar: la dirección por la que el propio
        # administrador ha llegado hasta aquí, que por definición funciona.
        # Dentro del contenedor no hay forma de averiguarla (solo se ve la IP
        # del puente de Docker), así que este es el único dato fiable.
        nombres = [request.host]
        nombres += request.form.get("nombres", "").replace(",", " ").split()

        try:
            if accion == "activar":
                estado = tls.activar(DATA_DIR, nombres)
                log.info("HTTPS activado; el certificado cubre %s", estado["nombres"])
                flash(
                    "HTTPS preparado. Descarga el certificado, instálalo en tus "
                    "dispositivos y reinicia el servidor para que empiece a usarse.",
                    "success",
                )
            elif accion == "regenerar":
                # Con la MISMA CA: los dispositivos que ya la tienen instalada
                # siguen confiando, así que no hay que volver a tocarlos. Es lo
                # que se usa cuando cambia la IP del servidor.
                tls.generar_certificado_servidor(DATA_DIR, nombres)
                flash(
                    "Certificado regenerado con la misma CA: no hace falta "
                    "reinstalar nada en los dispositivos. Reinicia el servidor.",
                    "success",
                )
            elif accion == "desactivar":
                tls.desactivar(DATA_DIR)
                flash(
                    "HTTPS desactivado. Reinicia el servidor para volver a HTTP. "
                    "Los certificados se conservan por si quieres reactivarlo.",
                    "success",
                )
            else:
                flash("Acción no reconocida.", "error")
        except Exception as exc:
            log.exception("Fallo al preparar el HTTPS")
            flash(f"No se pudo completar la operación: {exc}", "error")
        return redirect(url_for("admin_https"))

    estado = tls.estado(DATA_DIR)
    return render_template(
        "https.html",
        estado=estado,
        # Si el certificado no cubre la dirección por la que se está entrando,
        # el navegador seguirá avisando por mucho que la CA esté instalada. Es
        # el fallo más desconcertante de todos, así que se detecta y se dice.
        cubre_esta_direccion=tls.cubre(DATA_DIR, request.host) if estado["hay_certificado"] else False,
        host_actual=request.host,
        nombres_sugeridos=" ".join(
            dict.fromkeys([request.host] + tls.nombres_del_sistema())
        ),
        https_en_uso=request.is_secure,
        activo=HTTPS_ACTIVO,
    )


def lan_ip():
    """IP de esta máquina en la LAN, para imprimir una URL utilizable."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No envía nada: solo hace que el SO elija la interfaz de salida.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


init_db()
log.info("Datos en %s (base de datos: %s)", DATA_DIR, DB_PATH)

if __name__ == "__main__":
    # El servidor de desarrollo de Flask es de un solo hilo y limita el
    # rendimiento en subidas grandes por LAN. waitress maneja varias
    # conexiones en paralelo con hilos, sin capar el ancho de banda.
    from waitress import serve

    log.info("Servidor escuchando en http://0.0.0.0:%s", SERVER_PORT)
    log.info("  local: http://127.0.0.1:%s", SERVER_PORT)
    log.info("  LAN:   http://%s:%s", lan_ip(), SERVER_PORT)
    log.info("Pulsa Ctrl+C para parar.")
    try:
        serve(
            app,
            host="0.0.0.0",
            port=SERVER_PORT,
            threads=16,
            max_request_body_size=1024 ** 5,  # 1 PB, en la práctica sin límite
        )
    except KeyboardInterrupt:
        log.info("Servidor detenido.")
