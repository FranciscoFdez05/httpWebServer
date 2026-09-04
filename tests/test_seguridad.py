"""Las defensas de seguridad, cada una con su test."""

import os

from conftest import crear_usuario, entrar, subir


# ── Bloqueo por intentos fallidos ─────────────────────────────────────────────

def test_bloqueo_tras_demasiados_intentos(crear_app):
    modulo = crear_app(LOGIN_MAX_ATTEMPTS=3, LOGIN_WINDOW_MINUTES=15)
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()

    for _ in range(3):
        assert entrar(cliente, "ana", "mal").status_code == 200

    # Al cuarto intento ya no se comprueba la contraseña: ni con la buena.
    respuesta = entrar(cliente, "ana", "contrasena-larga")
    assert respuesta.status_code == 429
    assert b"Demasiados intentos" in respuesta.data


def test_un_login_correcto_borra_el_contador(crear_app):
    modulo = crear_app(LOGIN_MAX_ATTEMPTS=3)
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()

    entrar(cliente, "ana", "mal")
    entrar(cliente, "ana", "mal")
    assert entrar(cliente, "ana", "contrasena-larga").status_code == 302

    # Quien acierta vuelve a tener sus intentos completos.
    otro = modulo.app.test_client()
    for _ in range(2):
        assert entrar(otro, "ana", "mal").status_code == 200


def test_el_bloqueo_no_distingue_usuarios_existentes(crear_app):
    """Un nombre inexistente y una contraseña mala dan el mismo mensaje: si no,
    la respuesta serviría para averiguar qué cuentas existen."""
    modulo = crear_app()
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()

    a = entrar(cliente, "ana", "mal")
    b = entrar(cliente, "no-existe", "mal")
    assert a.data == b.data


def test_los_intentos_viejos_se_purgan(crear_app):
    """La tabla no puede crecer sin fin: los intentos fuera de la ventana ya no
    cuentan, así que se borran al comprobarlos."""
    modulo = crear_app(LOGIN_MAX_ATTEMPTS=3, LOGIN_WINDOW_MINUTES=15)
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    entrar(cliente, "ana", "mal")

    from datetime import timedelta
    viejo = (modulo.ahora() - timedelta(hours=2)).isoformat()
    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        db.execute("UPDATE login_attempts SET attempted_at = ?", (viejo,))
        db.commit()
        db.close()

    entrar(cliente, "ana", "mal")  # dispara la purga

    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        total = db.execute("SELECT COUNT(*) AS n FROM login_attempts").fetchone()["n"]
        db.close()
    assert total == 1, "el intento antiguo debería haberse purgado"


# ── Política de contraseñas ───────────────────────────────────────────────────

def test_contrasena_corta_rechazada(crear_app):
    modulo = crear_app(MIN_PASSWORD_LENGTH=12)
    assert modulo.validar_password("corta1") is not None
    assert modulo.validar_password("una-contrasena-larga-y-variada") is None


def test_contrasena_comun_rechazada(crear_app):
    modulo = crear_app(MIN_PASSWORD_LENGTH=6)
    assert modulo.validar_password("password123") is not None
    assert modulo.validar_password("qwertyuiop") is not None


def test_contrasena_igual_al_usuario_rechazada(crear_app):
    modulo = crear_app(MIN_PASSWORD_LENGTH=6)
    assert modulo.validar_password("MiUsuario", "miusuario") is not None


def test_contrasena_con_poca_variedad_rechazada(crear_app):
    modulo = crear_app(MIN_PASSWORD_LENGTH=6)
    assert modulo.validar_password("aaaaaaaaaaaaaaa") is not None


def test_setup_aplica_la_politica(crear_app):
    modulo = crear_app(sin_admin=True, MIN_PASSWORD_LENGTH=12)
    cliente = modulo.app.test_client()
    respuesta = cliente.post(
        "/setup",
        data={"username": "jefe", "password": "corta", "confirm_password": "corta"},
        follow_redirects=True,
    )
    assert "al menos 12" in respuesta.data.decode()

    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        total = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        db.close()
    assert total == 0, "no debería haberse creado la cuenta"


# ── Cabeceras ─────────────────────────────────────────────────────────────────

def test_cabeceras_de_seguridad(modulo, cliente):
    crear_usuario(modulo, "ana", "contrasena-larga")
    respuesta = cliente.get("/login")
    csp = respuesta.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp.split("style-src")[0], "el script en línea debe seguir prohibido"
    assert respuesta.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert respuesta.headers["X-Content-Type-Options"] == "nosniff"
    assert respuesta.headers["Referrer-Policy"] == "same-origin"


def test_las_plantillas_no_tienen_javascript_en_linea():
    """Si alguien vuelve a meter un onclick, la CSP lo romperá en silencio en el
    navegador. Mejor que falle aquí."""
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plantillas = os.path.join(raiz, "templates")
    for nombre in os.listdir(plantillas):
        if not nombre.endswith(".html"):
            continue
        with open(os.path.join(plantillas, nombre), encoding="utf-8") as f:
            contenido = f.read()
        for prohibido in ("onclick=", "onsubmit=", "onchange=", "<script>", 'style="'):
            assert prohibido not in contenido, f"{nombre} contiene {prohibido}"


def test_los_ficheros_servidos_llevan_csp_estricta(modulo, cliente):
    crear_usuario(modulo, "ana", "contrasena-larga")
    entrar(cliente, "ana", "contrasena-larga")
    subir(cliente, "foto.png", b"\x89PNG\r\n\x1a\n", visibility="public")
    csp = cliente.get("/download/1").headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'none'")


def test_cookies_secure_con_proxy(crear_app):
    con_proxy = crear_app(BEHIND_PROXY="true")
    assert con_proxy.app.config["SESSION_COOKIE_SECURE"] is True
    assert con_proxy.app.config["REMEMBER_COOKIE_SECURE"] is True

    sin_proxy = crear_app(BEHIND_PROXY="false")
    assert sin_proxy.app.config["SESSION_COOKIE_SECURE"] is False


def test_csrf_protege_los_formularios(crear_app):
    """Con CSRF activado (como en producción), un POST sin token se rechaza."""
    modulo = crear_app()
    modulo.app.config["WTF_CSRF_ENABLED"] = True
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    assert cliente.post("/login", data={"username": "ana", "password": "x"}).status_code == 400
