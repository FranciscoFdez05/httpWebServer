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
    assert modulo.disco_reservado_mb() == 1024


def test_sqlite_en_modo_wal(modulo):
    """Sin WAL, un escritor bloquea a todos los lectores y con 2 workers de
    gunicorn basta una subida para tumbar otra petición."""
    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        modo = db.execute("PRAGMA journal_mode").fetchone()[0]
        db.close()
    assert modo.lower() == "wal"


def test_los_nombres_de_docker_son_coherentes():
    """El servicio que nombran los scripts tiene que existir en el compose. Si
    se separan, docker-update.sh no encuentra el contenedor y da por muerta una
    versión que arrancó bien."""
    import re
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with open(os.path.join(raiz, "docker-compose.yml"), encoding="utf-8") as f:
        compose = f.read()
    servicios = re.findall(r"^  ([A-Za-z0-9_-]+):$", compose, re.MULTILINE)
    assert "httpwebserver" in servicios, f"servicios en el compose: {servicios}"

    for script in ("docker-up.sh", "docker-update.sh"):
        with open(os.path.join(raiz, script), encoding="utf-8") as f:
            contenido = f.read()
        servicio = re.search(r'^SERVICIO="([^"]+)"', contenido, re.MULTILINE)
        assert servicio, f"{script} no define SERVICIO"
        assert servicio.group(1) in servicios, \
            f"{script} usa el servicio {servicio.group(1)}, que no está en el compose"


def test_el_nombre_de_la_imagen_es_valido_para_docker():
    """Docker solo admite [a-z0-9._-] en el nombre de una imagen: con mayúsculas
    el build falla con «repository name must be lowercase»."""
    import re
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "docker-compose.yml"), encoding="utf-8") as f:
        compose = f.read()
    imagen = re.search(r'^\s*image:\s*"?([^":$]+)', compose, re.MULTILINE).group(1)
    assert re.fullmatch(r"[a-z0-9]+([._-][a-z0-9]+)*", imagen), \
        f"nombre de imagen no válido para Docker: {imagen}"


def test_el_volumen_de_datos_se_llama_httpwebserver():
    """El nombre explícito importa: sin `name:`, Compose le antepone el del
    proyecto y el volumen pasaría a llamarse httpwebserver_httpWebServer. Ese
    cambio de nombre crearía un volumen nuevo y vacío, con los datos intactos
    pero invisibles en el anterior."""
    import re
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "docker-compose.yml"), encoding="utf-8") as f:
        compose = f.read()
    assert re.search(r"^\s+name:\s*httpWebServer\s*$", compose, re.MULTILINE), (
        "el volumen debe declarar name: httpWebServer"
    )
    assert "- httpWebServer:/data" in compose


def test_el_volumen_de_datos_no_lo_puede_borrar_compose():
    """Declarado como external, "docker compose down -v" no puede llevarse por
    delante los archivos subidos, la base de datos y los certificados. A cambio
    lo tienen que crear los scripts, porque Compose ya no lo hace."""
    import re
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with open(os.path.join(raiz, "docker-compose.yml"), encoding="utf-8") as f:
        compose = f.read()
    assert re.search(r"^\s+external:\s*true\s*$", compose, re.MULTILINE), (
        "el volumen de datos debe ser external"
    )

    # Si Compose no lo crea, alguien tiene que hacerlo, o el "up" falla con
    # «external volume not found».
    for script in ("docker-up.sh", "docker-update.sh"):
        with open(os.path.join(raiz, script), encoding="utf-8") as f:
            contenido = f.read()
        assert "docker volume create httpWebServer" in contenido, (
            f"{script} debe crear el volumen antes de levantar"
        )


def test_la_imagen_incluye_todos_los_modulos_locales():
    """Un módulo que app.py importa pero que el Dockerfile no copia hace que el
    contenedor muera al arrancar con un ImportError. En local no se nota,
    porque el fichero está ahí; solo se ve al construir la imagen."""
    import re
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with open(os.path.join(raiz, "app.py"), encoding="utf-8") as f:
        codigo = f.read()
    with open(os.path.join(raiz, "Dockerfile"), encoding="utf-8") as f:
        dockerfile = f.read()

    # Los import de una sola palabra que además existen como .py en la raíz son
    # módulos del proyecto; el resto vienen de requirements.txt.
    importados = re.findall(r"^import ([a-z_][a-z0-9_]*)$", codigo, re.MULTILINE)
    locales = [
        m for m in importados
        if os.path.exists(os.path.join(raiz, m + ".py"))
    ]
    assert locales, "se esperaba al menos un módulo local"

    for modulo in locales:
        assert f"{modulo}.py" in dockerfile, \
            f"app.py importa {modulo}, pero el Dockerfile no copia {modulo}.py"


def test_la_guarda_de_cambios_locales_ignora_los_permisos():
    """En Linux los scripts hay que poder ejecutarlos, y un `chmod +x` cuenta
    para git como una modificación del fichero. Sin ignorar los permisos, la
    guarda veía «tienes cambios sin confirmar» y se negaba a actualizar: hacer
    el script ejecutable impedía ejecutarlo. Y el diff que imprimía decía
    «0 insertions(+), 0 deletions(-)», que no explica nada."""
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "docker-update.sh"), encoding="utf-8") as f:
        script = f.read()

    assert "core.fileMode=false" in script, "la guarda debe ignorar los permisos"
    # Y ninguna comprobación puede haberse quedado con el git de siempre.
    for linea in script.splitlines():
        limpia = linea.strip()
        if limpia.startswith("#"):
            continue
        assert "git diff --quiet" not in limpia, (
            f"esta comprobación no ignora los permisos: {limpia}"
        )
