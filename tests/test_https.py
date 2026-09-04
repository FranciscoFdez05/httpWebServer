"""HTTPS con certificado propio: generación, descarga e interfaz."""

import datetime
import os
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding

import tls
from conftest import crear_usuario, entrar


@pytest.fixture
def datos(tmp_path):
    return str(tmp_path / "datos-tls")


# ── Generación ────────────────────────────────────────────────────────────────

def test_estado_inicial_sin_nada(datos):
    estado = tls.estado(datos)
    assert estado["activo"] is False
    assert estado["hay_ca"] is False
    assert estado["hay_certificado"] is False


def test_activar_genera_ca_y_certificado(datos):
    estado = tls.activar(datos, ["192.168.1.50"])
    assert estado["activo"] is True
    assert estado["hay_ca"] and estado["hay_certificado"]
    r = tls.rutas(datos)
    for clave in ("ca_crt", "ca_key", "server_crt", "server_key", "flag"):
        assert os.path.exists(r[clave]), f"falta {clave}"


def test_la_ca_firma_de_verdad_el_certificado(datos):
    """Si la firma no valida, el navegador no confiará por mucho que la CA esté
    instalada."""
    tls.activar(datos, ["192.168.1.50"])
    r = tls.rutas(datos)
    with open(r["ca_crt"], "rb") as f:
        ca = x509.load_pem_x509_certificate(f.read())
    with open(r["server_crt"], "rb") as f:
        srv = x509.load_pem_x509_certificate(f.read())

    ca.public_key().verify(
        srv.signature, srv.tbs_certificate_bytes,
        padding.PKCS1v15(), srv.signature_hash_algorithm,
    )
    assert srv.issuer == ca.subject


def test_el_san_separa_ips_de_nombres(datos):
    """Un navegador que entra por IP NO acepta un certificado que la declare
    como nombre DNS. Es el motivo más común de que un certificado casero siga
    dando aviso."""
    tls.activar(datos, ["192.168.1.50", "servidor.casa"])
    with open(tls.rutas(datos)["server_crt"], "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value

    ips = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    dns = san.get_values_for_type(x509.DNSName)
    assert "192.168.1.50" in ips
    assert "127.0.0.1" in ips
    assert "servidor.casa" in dns
    assert "localhost" in dns
    assert "192.168.1.50" not in dns, "una IP no puede ir como nombre DNS"


def test_el_puerto_se_quita_del_nombre(datos):
    """El Host de la petición trae el puerto pegado; en el certificado no va."""
    tls.activar(datos, ["192.168.1.50:8000"])
    assert tls.cubre(datos, "192.168.1.50")
    assert tls.cubre(datos, "192.168.1.50:9999")


def test_caducidades_dentro_de_lo_que_acepta_apple(datos):
    """iOS rechaza sin explicación los certificados de servidor de más de 825
    días."""
    tls.activar(datos, ["192.168.1.50"])
    estado = tls.estado(datos)
    assert 0 < estado["certificado_caduca_en_dias"] <= 825
    assert estado["ca_caduca_en_dias"] > 3000


def test_el_certificado_lleva_serverauth(datos):
    """Sin serverAuth explícito, Safari lo rechaza aunque la CA esté instalada
    y marcada como de confianza."""
    tls.activar(datos, ["192.168.1.50"])
    with open(tls.rutas(datos)["server_crt"], "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in eku

    basicas = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basicas.ca is False, "el certificado del servidor no puede ser una CA"


def test_regenerar_conserva_la_ca(datos):
    """Es lo que permite cambiar la IP del servidor sin tener que reinstalar
    nada en los dispositivos."""
    tls.activar(datos, ["192.168.1.50"])
    with open(tls.rutas(datos)["ca_crt"], "rb") as f:
        ca_antes = f.read()

    tls.generar_certificado_servidor(datos, ["192.168.1.99"])

    with open(tls.rutas(datos)["ca_crt"], "rb") as f:
        assert f.read() == ca_antes, "la CA no debe cambiar al regenerar"
    assert tls.cubre(datos, "192.168.1.99")
    assert not tls.cubre(datos, "192.168.1.50")


def test_desactivar_no_borra_los_certificados(datos):
    """Si los borrase, reactivar exigiría reinstalar la CA en todos lados."""
    tls.activar(datos, ["192.168.1.50"])
    tls.desactivar(datos)

    estado = tls.estado(datos)
    assert estado["activo"] is False
    assert estado["hay_ca"] is True
    assert estado["hay_certificado"] is True

    # Y reactivar reutiliza la misma CA.
    with open(tls.rutas(datos)["ca_crt"], "rb") as f:
        ca_antes = f.read()
    tls.activar(datos, ["192.168.1.50"])
    with open(tls.rutas(datos)["ca_crt"], "rb") as f:
        assert f.read() == ca_antes


def test_no_se_puede_firmar_sin_ca(datos):
    with pytest.raises(FileNotFoundError):
        tls.generar_certificado_servidor(datos, ["192.168.1.50"])


@pytest.mark.skipif(sys.platform == "win32", reason="Windows no aplica permisos POSIX")
def test_las_claves_privadas_no_son_legibles_por_otros(datos):
    tls.activar(datos, ["192.168.1.50"])
    for clave in ("ca_key", "server_key"):
        modo = os.stat(tls.rutas(datos)[clave]).st_mode & 0o777
        assert modo == 0o600, f"{clave} tiene permisos {oct(modo)}"


# ── Interfaz ──────────────────────────────────────────────────────────────────

def test_la_pagina_es_solo_para_administradores(modulo):
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    entrar(cliente, "ana", "contrasena-larga")
    assert cliente.get("/admin/https").status_code == 403
    assert cliente.post("/admin/https", data={"accion": "activar"}).status_code == 403


def test_activar_desde_la_interfaz(modulo):
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")

    assert cliente.get("/admin/https").status_code == 200
    cliente.post("/admin/https", data={"accion": "activar", "nombres": "192.168.1.50"})

    estado = tls.estado(modulo.DATA_DIR)
    assert estado["activo"] is True
    # El Host de la petición se añade solo: es la dirección por la que el
    # administrador ha llegado, así que por definición funciona.
    assert "localhost" in estado["nombres"]
    assert "192.168.1.50" in estado["nombres"]


def test_la_descarga_de_la_ca_es_publica(modulo):
    """Un móvil recién llegado tiene que poder instalarla sin iniciar sesión."""
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")
    cliente.post("/admin/https", data={"accion": "activar", "nombres": "192.168.1.50"})

    anonimo = modulo.app.test_client()
    respuesta = anonimo.get("/ca.crt")
    assert respuesta.status_code == 200
    assert respuesta.mimetype == "application/x-x509-ca-cert"
    assert respuesta.data.startswith(b"-----BEGIN CERTIFICATE-----")
    # Debe poder parsearse como certificado de verdad.
    x509.load_pem_x509_certificate(respuesta.data)


def test_la_descarga_no_expone_ninguna_clave_privada(modulo):
    """Lo que se sirve es la parte pública. ca.key permitiría suplantar
    cualquier web ante los dispositivos que hayan instalado la CA."""
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")
    cliente.post("/admin/https", data={"accion": "activar", "nombres": "192.168.1.50"})

    datos = modulo.app.test_client().get("/ca.crt").data
    assert b"PRIVATE KEY" not in datos

    # Y no hay ninguna ruta que sirva el directorio de certificados.
    for ruta in ("/ca.key", "/tls/ca.key", "/static/../data/tls/ca.key"):
        assert modulo.app.test_client().get(ruta).status_code in (404, 403, 308)


def test_sin_ca_la_descarga_da_404(modulo):
    assert modulo.app.test_client().get("/ca.crt").status_code == 404


def test_desactivar_desde_la_interfaz(modulo):
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")
    cliente.post("/admin/https", data={"accion": "activar", "nombres": "192.168.1.50"})
    cliente.post("/admin/https", data={"accion": "desactivar"})

    assert tls.esta_activo(modulo.DATA_DIR) is False
    # Pero la CA sigue descargable, para no perderla de vista.
    assert modulo.app.test_client().get("/ca.crt").status_code == 200


def test_avisa_si_el_certificado_no_cubre_esta_direccion(modulo):
    """Es el fallo más desconcertante: la CA instalada, todo aparentemente
    bien, y el navegador sigue avisando igual."""
    # Se activa por la vía directa: hacerlo desde la interfaz añadiría el Host
    # de esa petición al certificado, que es justo lo que hay que evitar aquí.
    tls.activar(modulo.DATA_DIR, ["192.168.1.50"])

    def pagina_desde(base):
        # La sesión se abre desde la misma dirección: la cookie va atada al
        # dominio, y con otro base_url no se enviaría y acabaríamos leyendo el
        # formulario de login.
        cliente = modulo.app.test_client()
        cliente.post(
            "/login",
            data={"username": "root", "password": "contrasena-de-instalacion"},
            base_url=base,
        )
        return cliente.get("/admin/https", base_url=base).data.decode()

    # Una dirección que el certificado no cubre. localhost no sirve para esta
    # prueba: nombres_del_sistema() siempre lo incluye.
    assert "no cubre" in pagina_desde("http://192.168.99.99:8000")
    # Y una que sí cubre no debe avisar.
    assert "no cubre" not in pagina_desde("http://192.168.1.50:8000")


def test_health_informa_del_estado_del_tls(modulo):
    datos = modulo.app.test_client().get("/api/health").get_json()
    assert datos["https"] is False


def test_https_activo_marca_las_cookies_como_secure(crear_app, tmp_path):
    """Con TLS propio no hace falta BEHIND_PROXY para que las cookies lleven
    Secure: el cifrado ya lo pone el propio servidor."""
    modulo = crear_app()
    tls.activar(modulo.DATA_DIR, ["192.168.1.50"])
    # Se recarga para que app.py vuelva a leer el estado del arranque.
    import importlib
    modulo = importlib.reload(modulo)
    assert modulo.HTTPS_ACTIVO is True
    assert modulo.app.config["SESSION_COOKIE_SECURE"] is True


def test_el_entrypoint_y_la_app_deciden_lo_mismo(datos):
    """docker-entrypoint.sh comprueba tres ficheros para decidir si arrancar
    gunicorn con TLS. Si su criterio y el de tls.esta_activo() se separan, la
    interfaz diría que el HTTPS está activo mientras el servidor sigue en HTTP.
    """
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "docker-entrypoint.sh"), encoding="utf-8") as f:
        script = f.read()
    for fichero in ("enabled", "server.crt", "server.key"):
        assert f'"$TLS_DIR/{fichero}"' in script, f"el entrypoint no comprueba {fichero}"

    tls.activar(datos, ["192.168.1.50"])
    r = tls.rutas(datos)
    # Quitar cualquiera de los tres tiene que desactivarlo en ambos criterios.
    for clave in ("flag", "server_crt", "server_key"):
        copia = open(r[clave], "rb").read()
        os.remove(r[clave])
        assert tls.esta_activo(datos) is False, f"esta_activo ignora que falte {clave}"
        with open(r[clave], "wb") as f:
            f.write(copia)
    assert tls.esta_activo(datos) is True


# ── Handshake real ────────────────────────────────────────────────────────────

def test_un_cliente_que_confia_en_la_ca_completa_el_handshake(datos):
    """La prueba que de verdad importa: un cliente que tiene la CA instalada
    debe conectarse SIN avisos, con verificación de nombre incluida. Todo lo
    demás (extensiones, fechas, SAN) puede estar bien y aun así fallar aquí.
    """
    import socket
    import ssl
    import threading

    tls.activar(datos, ["127.0.0.1"])
    r = tls.rutas(datos)

    servidor = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    servidor.load_cert_chain(r["server_crt"], r["server_key"])

    escucha = socket.socket()
    escucha.bind(("127.0.0.1", 0))
    escucha.listen(1)
    puerto = escucha.getsockname()[1]

    fallo = []

    def atender():
        try:
            crudo, _ = escucha.accept()
            with servidor.wrap_socket(crudo, server_side=True) as conexion:
                conexion.recv(16)
                conexion.send(b"ok")
        except Exception as exc:      # se comprueba desde el hilo principal
            fallo.append(exc)

    hilo = threading.Thread(target=atender, daemon=True)
    hilo.start()

    # El cliente confía SOLO en nuestra CA, como un móvil con el certificado ya
    # instalado, y con check_hostname activado: si el SAN estuviera mal, esto
    # falla aquí igual que fallaría en el navegador.
    cliente = ssl.create_default_context(cafile=r["ca_crt"])
    cliente.check_hostname = True
    with socket.create_connection(("127.0.0.1", puerto), timeout=10) as crudo:
        with cliente.wrap_socket(crudo, server_hostname="127.0.0.1") as conexion:
            conexion.send(b"hola")
            assert conexion.recv(16) == b"ok"
            cert = conexion.getpeercert()

    hilo.join(timeout=10)
    escucha.close()
    assert not fallo, f"el servidor falló: {fallo}"
    assert cert, "el certificado debería haberse validado"


def test_un_cliente_sin_la_ca_es_rechazado(datos):
    """El reverso: sin instalar la CA, la conexión NO debe validar. Si validara,
    querría decir que el certificado lo firma algo en lo que ya se confía, y
    todo el mecanismo sobraría."""
    import socket
    import ssl
    import threading

    tls.activar(datos, ["127.0.0.1"])
    r = tls.rutas(datos)

    servidor = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    servidor.load_cert_chain(r["server_crt"], r["server_key"])

    escucha = socket.socket()
    escucha.bind(("127.0.0.1", 0))
    escucha.listen(1)
    puerto = escucha.getsockname()[1]

    def atender():
        try:
            crudo, _ = escucha.accept()
            with servidor.wrap_socket(crudo, server_side=True):
                pass
        except Exception:
            pass      # el rechazo del cliente aborta el handshake: es lo normal

    threading.Thread(target=atender, daemon=True).start()

    cliente = ssl.create_default_context()      # solo las CA públicas
    with pytest.raises(ssl.SSLCertVerificationError):
        with socket.create_connection(("127.0.0.1", puerto), timeout=10) as crudo:
            with cliente.wrap_socket(crudo, server_hostname="127.0.0.1"):
                pass
    escucha.close()
