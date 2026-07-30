import configparser
import io
import mimetypes
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta

from flask import (
    Flask, request, redirect, url_for, render_template,
    send_from_directory, send_file, abort, flash, g, jsonify, session
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from PIL import Image
from pypdf import PdfReader, PdfWriter

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

# Fuente única del puerto: config.ini. Tanto waitress (más abajo) como el
# CMD de gunicorn en el Dockerfile lo leen de aquí, para no tener el puerto
# repetido y desincronizado en varios sitios.
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
_config = configparser.ConfigParser()
_config.read(CONFIG_PATH)
SERVER_PORT = _config.getint("server", "port", fallback=8000)

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
app.config["MAX_CONTENT_LENGTH"] = None  # sin límite de tamaño de subida
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)

login_manager = LoginManager(app)
login_manager.login_view = "login"
csrf = CSRFProtect(app)


@app.before_request
def make_session_permanent():
    session.permanent = True


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
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
    if request.endpoint in ("setup", "static"):
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
        if not username:
            flash("Debes indicar un nombre de usuario.", "error")
        elif len(password) < 6:
            flash("La contraseña debe tener al menos 6 caracteres.", "error")
        elif password != confirm_password:
            flash("Las contraseñas no coinciden.", "error")
        else:
            db = get_db()
            db.execute(
                "INSERT INTO users (username, password_hash, is_admin, must_change_password) "
                "VALUES (?, ?, 1, 0)",
                (username, generate_password_hash(password)),
            )
            db.commit()
            flash("Cuenta de administrador creada. Ya puedes iniciar sesión.", "success")
            return redirect(url_for("login"))
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
    )


@app.route("/")
def index():
    files = visible_files_for_current_user()
    return render_template("index.html", files=files)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            login_user(User(row), remember=True)
            if row["must_change_password"]:
                return redirect(url_for("change_password"))
            return redirect(url_for("index"))
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
        if not check_password_hash(row["password_hash"], current_password):
            flash("La contraseña actual no es correcta.", "error")
        elif len(new_password) < 6:
            flash("La nueva contraseña debe tener al menos 6 caracteres.", "error")
        elif new_password != confirm_password:
            flash("Las contraseñas nuevas no coinciden.", "error")
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

        visibility = "public" if visibility_choice == "public" else "private"
        uploaded_at = datetime.utcnow().isoformat()
        count = 0

        for uploaded_file in uploaded_files:
            original_name = secure_filename(uploaded_file.filename)
            if not original_name:
                continue
            stored_name = f"{uuid.uuid4().hex}_{original_name}"
            dest_path = os.path.join(UPLOAD_DIR, stored_name)
            uploaded_file.save(dest_path)
            size_bytes = os.path.getsize(dest_path)
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
    return response


def _can_manage_file(row):
    return current_user.is_authenticated and (
        current_user.is_admin or current_user.id == row["uploader_id"]
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

    try:
        if ext in IMAGE_EXTENSIONS:
            image = Image.open(file_path)
            data = list(image.getdata())
            clean_image = Image.new(image.mode, image.size)
            if image.mode == "P":
                clean_image.putpalette(image.getpalette())
            clean_image.putdata(data)
            clean_image.save(file_path)
        elif row["mime_type"] == "application/pdf":
            reader = PdfReader(file_path)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            writer.add_metadata({})
            with open(file_path, "wb") as f:
                writer.write(f)
        else:
            flash("Este tipo de archivo no soporta eliminación de metadatos.", "error")
            return redirect(url_for("index"))
    except Exception:
        flash("No se pudieron eliminar los metadatos de este archivo.", "error")
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
    file_path = os.path.join(UPLOAD_DIR, row["stored_name"])
    if os.path.exists(file_path):
        os.remove(file_path)
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
            if not username or len(password) < 6:
                flash("Nombre de usuario o contraseña inválidos (mínimo 6 caracteres).", "error")
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
        elif action == "delete_user":
            user_id = request.form.get("user_id")
            if user_id and int(user_id) != current_user.id:
                db.execute("DELETE FROM users WHERE id = ? AND is_admin = 0", (user_id,))
                db.commit()
                flash("Usuario eliminado.", "success")
        return redirect(url_for("admin"))

    users = db.execute("SELECT * FROM users ORDER BY is_admin DESC, username").fetchall()
    return render_template("admin.html", users=users)


init_db()

if __name__ == "__main__":
    # El servidor de desarrollo de Flask es de un solo hilo y limita el
    # rendimiento en subidas grandes por LAN. waitress maneja varias
    # conexiones en paralelo con hilos, sin capar el ancho de banda.
    from waitress import serve
    serve(
        app,
        host="0.0.0.0",
        port=SERVER_PORT,
        threads=16,
        max_request_body_size=1024 ** 5,  # 1 PB, en la práctica sin límite
    )
