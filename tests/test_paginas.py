"""Que cada página siga renderizando.

Al mover el JavaScript a static/app.js se tocaron las ocho plantillas. Un fallo
ahí no es un error de Python: es un 500 en una página concreta que nadie ve
hasta que alguien entra en ella. Estos tests entran en todas.
"""

from conftest import crear_usuario, entrar, subir


def test_todas_las_paginas_responden(modulo):
    crear_usuario(modulo, "jefe", "contrasena-larga", admin=True)
    cliente = modulo.app.test_client()
    entrar(cliente, "jefe", "contrasena-larga")
    subir(cliente, "x.txt", b"hola")

    for ruta in ("/", "/upload", "/admin", "/change_password", "/permissions/1"):
        respuesta = cliente.get(ruta)
        assert respuesta.status_code == 200, f"{ruta} devolvió {respuesta.status_code}"


def test_paginas_sin_sesion(crear_app):
    modulo = crear_app()
    cliente = modulo.app.test_client()
    assert cliente.get("/").status_code == 200
    assert cliente.get("/login").status_code == 200


def test_setup_renderiza_en_instalacion_virgen(crear_app):
    modulo = crear_app(sin_admin=True)
    assert modulo.app.test_client().get("/setup").status_code == 200


def test_las_paginas_cargan_el_javascript_externo(modulo, cliente):
    """Si base.html deja de incluir app.js, la interfaz se queda muerta: los
    modales no abren y el formulario de subida pierde la barra de progreso."""
    cuerpo = cliente.get("/login").data.decode()
    assert "/static/app.js" in cuerpo
    assert cliente.get("/static/app.js").status_code == 200


def test_los_botones_llevan_los_datos_que_espera_app_js(modulo):
    """app.js lee los datos de atributos data-*; antes iban como argumentos de
    un onclick. Si la plantilla y el script dejan de coincidir, el botón deja
    de hacer nada sin ningún error visible."""
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    entrar(cliente, "ana", "contrasena-larga")
    subir(cliente, "foto.png", b"\x89PNG\r\n\x1a\n", visibility="public")

    cuerpo = cliente.get("/").data.decode()
    assert 'data-action="preview"' in cuerpo
    assert 'data-action="props"' in cuerpo
    assert 'data-file-id="1"' in cuerpo
    assert 'data-mime="image/png"' in cuerpo


def test_el_minimo_de_contrasena_llega_a_los_formularios(crear_app):
    """El minlength del formulario y lo que valida el servidor tienen que decir
    lo mismo; si no, el navegador acepta y el servidor rechaza."""
    modulo = crear_app(sin_admin=True, MIN_PASSWORD_LENGTH=14)
    cuerpo = modulo.app.test_client().get("/setup").data.decode()
    assert 'minlength="14"' in cuerpo
    assert "al menos 14" in cuerpo
