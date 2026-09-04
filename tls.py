"""Certificados para el HTTPS de la red local.

En una LAN no hay forma de conseguir un certificado de una autoridad pública:
Let's Encrypt no valida direcciones como 192.168.1.50, y sin certificado el
navegador enseña un aviso rojo que todo el mundo acaba aprendiendo a ignorar.

La alternativa es crear una autoridad certificadora propia (la «CA»), instalarla
una vez en cada dispositivo y firmar con ella el certificado del servidor. A
partir de ahí el candado sale verde de verdad, no «aceptado a la fuerza».

Se generan DOS certificados por eso mismo:

  · ca.crt  — la autoridad. Es lo que se instala en el PC y en el móvil. Dura
              10 años, y mientras no cambie no hay que volver a tocar ningún
              dispositivo.
  · server.crt — el del servidor, firmado por la CA. Dura 825 días, que es el
              máximo que acepta iOS desde la 13: uno más largo lo rechaza sin
              decir por qué. Se puede regenerar cuando cambie la IP del
              servidor SIN tocar la CA, y por tanto sin reinstalar nada.

La clave privada de la CA (ca.key) es el objeto más sensible del proyecto:
quien la tenga puede suplantar cualquier web ante los dispositivos que hayan
instalado la CA. Se guarda con permisos 0600 y NUNCA se sirve por HTTP; lo que
se descarga es solo ca.crt, que es público (viaja en cada handshake TLS).
"""

import datetime
import ipaddress
import json
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DIAS_CA = 3650
# 825 días: el tope que aceptan iOS/macOS para un certificado de servidor. Poner
# más hace que Safari lo rechace sin mensaje útil.
DIAS_SERVIDOR = 825

NOMBRE_CA = "Servidor de Archivos - CA local"


def rutas(data_dir):
    base = os.path.join(data_dir, "tls")
    return {
        "dir": base,
        "ca_crt": os.path.join(base, "ca.crt"),
        "ca_key": os.path.join(base, "ca.key"),
        "server_crt": os.path.join(base, "server.crt"),
        "server_key": os.path.join(base, "server.key"),
        # Fichero-bandera. Se separa de la existencia de los certificados para
        # que «desactivar» no borre la CA: si la borrase, al reactivar habría
        # que reinstalarla en todos los dispositivos.
        "flag": os.path.join(base, "enabled"),
    }


# ── Nombres que cubre el certificado ──────────────────────────────────────────

def separar_nombres(nombres):
    """Reparte una lista de nombres en (dominios, IPs), sin repetidos.

    El SAN distingue entre DNS e IP, y un navegador que pide por IP NO acepta
    un certificado que solo la declare como nombre DNS: es el motivo más común
    de que un certificado casero siga dando aviso.
    """
    dns, ips = [], []
    for bruto in nombres:
        nombre = (bruto or "").strip().lower()
        if not nombre:
            continue
        # Quita el puerto si viene pegado (el Host de la petición lo trae).
        if nombre.startswith("["):                      # IPv6 entre corchetes
            nombre = nombre.split("]")[0].lstrip("[")
        elif nombre.count(":") == 1:
            nombre = nombre.split(":")[0]
        try:
            ip = ipaddress.ip_address(nombre)
            if ip not in ips:
                ips.append(ip)
        except ValueError:
            if nombre not in dns:
                dns.append(nombre)
    return dns, ips


def nombres_del_sistema():
    """Lo que se puede averiguar sin ayuda: localhost y el nombre del equipo.

    Dentro de un contenedor esto NO incluye la IP del servidor en la LAN: lo
    que se ve desde dentro es la IP del puente de Docker (172.x), que no sirve
    a nadie. La IP buena llega por otras dos vías: la variable HOST_LAN_IPS que
    exportan docker-up.sh y docker-update.sh, y la cabecera Host de la petición
    del propio administrador, que por definición es una dirección que funciona.
    """
    nombres = ["localhost", "127.0.0.1"]
    try:
        equipo = socket.gethostname()
        if equipo:
            nombres.append(equipo)
            nombres.append(equipo + ".local")
    except OSError:
        pass
    for bruto in os.environ.get("HOST_LAN_IPS", "").replace(",", " ").split():
        nombres.append(bruto)
    return nombres


# ── Generación ────────────────────────────────────────────────────────────────

def _guardar_clave(ruta, clave):
    # 0600 antes de escribir nada: crear el fichero con permisos abiertos y
    # arreglarlos después deja una ventana en la que la clave es legible.
    descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as f:
        f.write(clave.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    try:
        os.chmod(ruta, 0o600)
    except OSError:
        pass  # en Windows no aplica; el aviso no aporta nada


def _guardar_certificado(ruta, certificado):
    with open(ruta, "wb") as f:
        f.write(certificado.public_bytes(serialization.Encoding.PEM))


def generar_ca(data_dir):
    """Crea la autoridad certificadora. Solo se hace una vez."""
    r = rutas(data_dir)
    os.makedirs(r["dir"], exist_ok=True)

    clave = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    sujeto = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, NOMBRE_CA),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Servidor de Archivos"),
    ])
    ahora = datetime.datetime.now(datetime.timezone.utc)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(sujeto)
        .issuer_name(sujeto)                      # autofirmado: es la raíz
        .public_key(clave.public_key())
        .serial_number(x509.random_serial_number())
        # Un minuto de margen hacia atrás: si el reloj del móvil va ligeramente
        # adelantado respecto al del servidor, un certificado "del futuro" se
        # rechaza y el fallo es incomprensible.
        .not_valid_before(ahora - datetime.timedelta(minutes=1))
        .not_valid_after(ahora + datetime.timedelta(days=DIAS_CA))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(clave.public_key()), critical=False
        )
        .sign(clave, hashes.SHA256())
    )
    _guardar_clave(r["ca_key"], clave)
    _guardar_certificado(r["ca_crt"], certificado)
    return certificado


def generar_certificado_servidor(data_dir, nombres):
    """Firma un certificado de servidor con la CA existente.

    Se puede llamar tantas veces como haga falta (por ejemplo, al cambiar la IP
    del servidor): como la CA no cambia, los dispositivos que ya la tienen
    instalada siguen confiando sin tocar nada.
    """
    r = rutas(data_dir)
    if not (os.path.exists(r["ca_crt"]) and os.path.exists(r["ca_key"])):
        raise FileNotFoundError("No hay CA: hay que generarla antes.")

    with open(r["ca_key"], "rb") as f:
        clave_ca = serialization.load_pem_private_key(f.read(), password=None)
    with open(r["ca_crt"], "rb") as f:
        cert_ca = x509.load_pem_x509_certificate(f.read())

    dns, ips = separar_nombres(list(nombres) + nombres_del_sistema())
    if not dns and not ips:
        raise ValueError("Hay que indicar al menos un nombre o IP.")

    alternativos = [x509.DNSName(n) for n in dns] + [x509.IPAddress(i) for i in ips]
    principal = dns[0] if dns else str(ips[0])

    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ahora = datetime.datetime.now(datetime.timezone.utc)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, principal)]))
        .issuer_name(cert_ca.subject)
        .public_key(clave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ahora - datetime.timedelta(minutes=1))
        .not_valid_after(ahora + datetime.timedelta(days=DIAS_SERVIDOR))
        # El SAN es lo que miran los navegadores desde hace años; el Common Name
        # lo ignoran por completo. Sin esta extensión el certificado no vale
        # para nada por muy bien firmado que esté.
        .add_extension(x509.SubjectAlternativeName(alternativos), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        # Apple exige serverAuth explícito; sin él, Safari rechaza el
        # certificado aunque la CA esté instalada y marcada como de confianza.
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(cert_ca.public_key()),
            critical=False,
        )
        .sign(clave_ca, hashes.SHA256())
    )
    _guardar_clave(r["server_key"], clave)
    _guardar_certificado(r["server_crt"], certificado)
    return certificado


# ── Estado ────────────────────────────────────────────────────────────────────

def _leer_certificado(ruta):
    try:
        with open(ruta, "rb") as f:
            return x509.load_pem_x509_certificate(f.read())
    except (OSError, ValueError):
        return None


def _nombres_del_certificado(certificado):
    try:
        san = certificado.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        return []
    return (
        [str(n) for n in san.get_values_for_type(x509.DNSName)]
        + [str(n) for n in san.get_values_for_type(x509.IPAddress)]
    )


def esta_activo(data_dir):
    """Si el servidor debe arrancar con TLS.

    Las tres condiciones tienen que darse: es la misma comprobación que hace
    docker-entrypoint.sh antes de pasarle --certfile a gunicorn, y si aquí
    dijese que sí y allí que no, la interfaz mentiría.
    """
    r = rutas(data_dir)
    return (
        os.path.exists(r["flag"])
        and os.path.exists(r["server_crt"])
        and os.path.exists(r["server_key"])
    )


def estado(data_dir):
    r = rutas(data_dir)
    cert_ca = _leer_certificado(r["ca_crt"])
    cert_srv = _leer_certificado(r["server_crt"])
    ahora = datetime.datetime.now(datetime.timezone.utc)

    def caduca_en(certificado):
        if certificado is None:
            return None
        return (certificado.not_valid_after_utc - ahora).days

    return {
        "activo": esta_activo(data_dir),
        "hay_ca": cert_ca is not None,
        "hay_certificado": cert_srv is not None,
        "ca_caduca_en_dias": caduca_en(cert_ca),
        "certificado_caduca_en_dias": caduca_en(cert_srv),
        "nombres": _nombres_del_certificado(cert_srv) if cert_srv else [],
        # Sirve para avisar de que el certificado no cubre la dirección por la
        # que se está entrando, que es el fallo que más desconcierta.
        "huella_ca": cert_ca.fingerprint(hashes.SHA256()).hex(":").upper() if cert_ca else None,
    }


def cubre(data_dir, host):
    """¿El certificado sirve para la dirección por la que se entra ahora?"""
    cert = _leer_certificado(rutas(data_dir)["server_crt"])
    if cert is None:
        return False
    dns, ips = separar_nombres([host])
    cubiertos = {n.lower() for n in _nombres_del_certificado(cert)}
    return all(n in cubiertos for n in dns) and all(str(i) in cubiertos for i in ips)


# ── Acciones ──────────────────────────────────────────────────────────────────

def activar(data_dir, nombres):
    """Genera lo que falte y deja el HTTPS marcado para el próximo arranque."""
    r = rutas(data_dir)
    os.makedirs(r["dir"], exist_ok=True)
    if not (os.path.exists(r["ca_crt"]) and os.path.exists(r["ca_key"])):
        generar_ca(data_dir)
    generar_certificado_servidor(data_dir, nombres)
    with open(r["flag"], "w", encoding="utf-8") as f:
        json.dump(
            {"activado": datetime.datetime.now(datetime.timezone.utc).isoformat()}, f
        )
    return estado(data_dir)


def desactivar(data_dir):
    """Vuelve a HTTP. NO borra la CA ni el certificado a propósito: reactivar
    con la misma CA evita tener que reinstalarla en todos los dispositivos."""
    try:
        os.remove(rutas(data_dir)["flag"])
    except FileNotFoundError:
        pass
    return estado(data_dir)


def leer_ca_pem(data_dir):
    """El certificado público de la CA, que es lo único descargable."""
    with open(rutas(data_dir)["ca_crt"], "rb") as f:
        return f.read()
