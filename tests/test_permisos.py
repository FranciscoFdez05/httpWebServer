"""Permisos de acceso a los archivos.

Es la parte donde un fallo no da un error visible: da un archivo privado a
quien no debía verlo, en silencio. Por eso se prueba cada combinación de
(quién pide) x (qué visibilidad tiene el archivo) en vez de unos pocos casos
representativos.
"""

from conftest import crear_usuario, entrar, subir


def test_publico_visible_sin_sesion(modulo, cliente):
    crear_usuario(modulo, "ana", "contrasena-larga")
    entrar(cliente, "ana", "contrasena-larga")
    subir(cliente, "publico.txt", b"hola", visibility="public")

    anonimo = modulo.app.test_client()
    assert anonimo.get("/download/1").status_code == 200
    assert b"publico.txt" in anonimo.get("/").data


def test_privado_invisible_sin_sesion(modulo, cliente):
    crear_usuario(modulo, "ana", "contrasena-larga")
    entrar(cliente, "ana", "contrasena-larga")
    subir(cliente, "secreto.txt", b"hola", visibility="private_me")

    anonimo = modulo.app.test_client()
    assert anonimo.get("/download/1").status_code == 403
    assert anonimo.get("/preview/1").status_code == 403
    assert b"secreto.txt" not in anonimo.get("/").data


def test_privado_invisible_para_otro_usuario(modulo):
    crear_usuario(modulo, "ana", "contrasena-larga")
    crear_usuario(modulo, "bea", "contrasena-larga")

    de_ana = modulo.app.test_client()
    entrar(de_ana, "ana", "contrasena-larga")
    subir(de_ana, "de-ana.txt", b"hola", visibility="private_me")

    de_bea = modulo.app.test_client()
    entrar(de_bea, "bea", "contrasena-larga")
    assert de_bea.get("/download/1").status_code == 403
    assert b"de-ana.txt" not in de_bea.get("/").data


def test_compartido_visible_solo_para_quien_se_comparte(modulo):
    crear_usuario(modulo, "ana", "contrasena-larga")
    id_bea = crear_usuario(modulo, "bea", "contrasena-larga")
    crear_usuario(modulo, "carlos", "contrasena-larga")

    de_ana = modulo.app.test_client()
    entrar(de_ana, "ana", "contrasena-larga")
    subir(de_ana, "compartido.txt", b"hola",
          visibility="private_select", compartir=(id_bea,))

    de_bea = modulo.app.test_client()
    entrar(de_bea, "bea", "contrasena-larga")
    assert de_bea.get("/download/1").status_code == 200

    de_carlos = modulo.app.test_client()
    entrar(de_carlos, "carlos", "contrasena-larga")
    assert de_carlos.get("/download/1").status_code == 403


def test_admin_ve_todo(modulo):
    crear_usuario(modulo, "ana", "contrasena-larga")
    crear_usuario(modulo, "jefe", "contrasena-larga", admin=True)

    de_ana = modulo.app.test_client()
    entrar(de_ana, "ana", "contrasena-larga")
    subir(de_ana, "de-ana.txt", b"hola", visibility="private_me")

    de_jefe = modulo.app.test_client()
    entrar(de_jefe, "jefe", "contrasena-larga")
    assert de_jefe.get("/download/1").status_code == 200


def test_quitar_permiso_retira_el_acceso(modulo):
    crear_usuario(modulo, "ana", "contrasena-larga")
    id_bea = crear_usuario(modulo, "bea", "contrasena-larga")

    de_ana = modulo.app.test_client()
    entrar(de_ana, "ana", "contrasena-larga")
    subir(de_ana, "x.txt", b"hola", visibility="private_select", compartir=(id_bea,))

    de_bea = modulo.app.test_client()
    entrar(de_bea, "bea", "contrasena-larga")
    assert de_bea.get("/download/1").status_code == 200

    # Ana se lo quita: guardar los permisos reescribe la lista entera.
    de_ana.post("/permissions/1", data={"visibility": "private_me"})
    assert de_bea.get("/download/1").status_code == 403


def test_solo_el_dueno_o_un_admin_cambian_permisos(modulo):
    crear_usuario(modulo, "ana", "contrasena-larga")
    crear_usuario(modulo, "bea", "contrasena-larga")

    de_ana = modulo.app.test_client()
    entrar(de_ana, "ana", "contrasena-larga")
    subir(de_ana, "x.txt", b"hola")

    de_bea = modulo.app.test_client()
    entrar(de_bea, "bea", "contrasena-larga")
    assert de_bea.get("/permissions/1").status_code == 403
    # Y no puede hacerlo público por la puerta de atrás.
    assert de_bea.post("/permissions/1", data={"visibility": "public"}).status_code == 403
    assert modulo.app.test_client().get("/download/1").status_code == 403


def test_solo_un_admin_borra_archivos(modulo):
    crear_usuario(modulo, "ana", "contrasena-larga")
    crear_usuario(modulo, "bea", "contrasena-larga")

    de_ana = modulo.app.test_client()
    entrar(de_ana, "ana", "contrasena-larga")
    subir(de_ana, "x.txt", b"hola")

    de_bea = modulo.app.test_client()
    entrar(de_bea, "bea", "contrasena-larga")
    assert de_bea.post("/delete/1").status_code == 403


def test_preview_rechaza_los_tipos_no_seguros(modulo, cliente):
    """El HTML nunca se sirve en línea: se ejecutaría en el mismo origen y
    podría leer la sesión de quien lo previsualiza."""
    crear_usuario(modulo, "ana", "contrasena-larga")
    entrar(cliente, "ana", "contrasena-larga")
    subir(cliente, "trampa.html", b"<script>alert(1)</script>", visibility="public")

    assert cliente.get("/preview/1").status_code == 415
    # Descargarlo sí se puede: va como adjunto y con nosniff.
    r = cliente.get("/download/1")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "attachment" in r.headers["Content-Disposition"]


def test_el_mime_no_lo_decide_el_cliente(modulo, cliente):
    """El Content-Type que manda el navegador se ignora: si mandara
    'image/png' para un .html, la vista previa lo serviría en línea."""
    import io as _io

    crear_usuario(modulo, "ana", "contrasena-larga")
    entrar(cliente, "ana", "contrasena-larga")
    cliente.post(
        "/upload",
        data={
            "files": (_io.BytesIO(b"<script>alert(1)</script>"), "trampa.html", "image/png"),
            "visibility": "public",
        },
        content_type="multipart/form-data",
    )
    with modulo.app.app_context():
        db = modulo.conectar(modulo.DB_PATH)
        fila = db.execute("SELECT mime_type FROM files WHERE id = 1").fetchone()
        db.close()
    assert fila["mime_type"] == "text/html"
    assert cliente.get("/preview/1").status_code == 415
