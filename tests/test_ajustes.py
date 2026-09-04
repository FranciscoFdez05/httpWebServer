"""La pantalla de Ajustes y la resolución de valores.

El orden de precedencia es lo delicado: entorno > guardado en la web > fábrica.
Si se invirtiera, alguien podría cambiar desde el navegador algo que el
administrador había fijado en .env a propósito, o al revés: guardar un valor
que se ignora en silencio.
"""

import json
import os

import pytest

import ajustes
from conftest import crear_usuario, entrar


@pytest.fixture
def datos(tmp_path):
    d = str(tmp_path / "datos-ajustes")
    os.makedirs(d, exist_ok=True)
    ajustes._cache.clear()
    return d


# ── Precedencia ───────────────────────────────────────────────────────────────

def test_sin_nada_se_usa_el_valor_de_fabrica(datos, monkeypatch):
    monkeypatch.delenv("MIN_FREE_DISK_MB", raising=False)
    assert ajustes.valor("min_free_disk_mb", datos) == 1024
    assert ajustes.origen("min_free_disk_mb", datos) == "fábrica"


def test_lo_guardado_manda_sobre_la_fabrica(datos, monkeypatch):
    monkeypatch.delenv("MIN_FREE_DISK_MB", raising=False)
    ajustes.guardar(datos, {"min_free_disk_mb": 4096})
    assert ajustes.valor("min_free_disk_mb", datos) == 4096
    assert ajustes.origen("min_free_disk_mb", datos) == "guardado"


def test_el_entorno_manda_sobre_lo_guardado(datos, monkeypatch):
    """.env es la configuración de la máquina: quien la pone no quiere que se
    la cambien desde el navegador."""
    ajustes.guardar(datos, {"min_free_disk_mb": 4096})
    monkeypatch.setenv("MIN_FREE_DISK_MB", "77")
    assert ajustes.valor("min_free_disk_mb", datos) == 77
    assert ajustes.origen("min_free_disk_mb", datos) == "entorno"
    assert ajustes.fijado_en_entorno("min_free_disk_mb") is True


def test_guardar_ignora_lo_fijado_en_el_entorno(datos, monkeypatch):
    """Guardar algo que luego se ignora sería peor que no dejar guardarlo."""
    monkeypatch.setenv("MIN_FREE_DISK_MB", "77")
    ajustes.guardar(datos, {"min_free_disk_mb": 4096, "user_quota_mb": 500})
    guardado = ajustes.leer_archivo(datos)
    assert "min_free_disk_mb" not in guardado
    assert guardado["user_quota_mb"] == 500


def test_restablecer_vuelve_a_fabrica(datos, monkeypatch):
    monkeypatch.delenv("MIN_FREE_DISK_MB", raising=False)
    ajustes.guardar(datos, {"min_free_disk_mb": 4096})
    ajustes.restablecer(datos)
    assert ajustes.valor("min_free_disk_mb", datos) == 1024


# ── Robustez del fichero ──────────────────────────────────────────────────────

def test_un_fichero_corrupto_no_tumba_el_servidor(datos, monkeypatch):
    """Los valores de fábrica siempre son válidos, así que un JSON roto se
    ignora en vez de impedir el arranque."""
    monkeypatch.delenv("MIN_FREE_DISK_MB", raising=False)
    with open(ajustes.ruta(datos), "w", encoding="utf-8") as f:
        f.write("{esto no es json")
    ajustes._cache.clear()
    assert ajustes.valor("min_free_disk_mb", datos) == 1024


def test_la_cache_ve_los_cambios_de_otro_worker(datos, monkeypatch):
    """Con varios workers de gunicorn, el que atiende el formulario escribe el
    fichero y los demás tienen que enterarse."""
    monkeypatch.delenv("USER_QUOTA_MB", raising=False)
    assert ajustes.valor("user_quota_mb", datos) == 0

    # Simula la escritura de otro proceso, sin pasar por guardar().
    with open(ajustes.ruta(datos), "w", encoding="utf-8") as f:
        json.dump({"user_quota_mb": 999}, f)
    os.utime(ajustes.ruta(datos), None)

    assert ajustes.valor("user_quota_mb", datos) == 999


def test_guardar_es_atomico(datos, monkeypatch):
    """Se escribe en un temporal y se sustituye: un JSON cortado por la mitad
    dejaría la configuración ilegible."""
    monkeypatch.delenv("USER_QUOTA_MB", raising=False)
    ajustes.guardar(datos, {"user_quota_mb": 10})
    assert not any(n.startswith(".ajustes_") for n in os.listdir(datos))
    with open(ajustes.ruta(datos), encoding="utf-8") as f:
        assert json.load(f)["user_quota_mb"] == 10


# ── Validación ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bruto", ["", "  ", "abc", "1.5", "-"])
def test_valores_no_numericos_rechazados(bruto):
    _, error = ajustes.validar("user_quota_mb", bruto)
    assert error is not None


def test_los_limites_se_respetan():
    assert ajustes.validar("min_password_length", "2")[1] is not None   # mínimo 4
    assert ajustes.validar("min_password_length", "999")[1] is not None  # máximo 128
    assert ajustes.validar("min_password_length", "12")[1] is None
    assert ajustes.validar("user_quota_mb", "-5")[1] is not None
    assert ajustes.validar("user_quota_mb", "0")[1] is None


# ── Pantalla ──────────────────────────────────────────────────────────────────

def test_la_pantalla_es_solo_para_administradores(modulo):
    crear_usuario(modulo, "ana", "contrasena-larga")
    cliente = modulo.app.test_client()
    entrar(cliente, "ana", "contrasena-larga")
    assert cliente.get("/admin/ajustes").status_code == 403
    assert cliente.post("/admin/ajustes", data={"user_quota_mb": "1"}).status_code == 403


def test_la_pantalla_se_ve_y_enlaza_desde_la_barra(modulo):
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")

    portada = cliente.get("/").data.decode()
    assert "/admin/ajustes" in portada, "falta el enlace de Ajustes en la barra"

    assert cliente.get("/admin/ajustes").status_code == 200


def test_guardar_desde_la_pantalla_se_aplica_al_momento(modulo, monkeypatch):
    """Sin reiniciar: es la diferencia entre unos ajustes útiles y unos que
    obligan a entrar al servidor."""
    monkeypatch.delenv("USER_QUOTA_MB", raising=False)
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")

    assert modulo.cuota_usuario_mb() == 0
    cliente.post("/admin/ajustes", data={"accion": "guardar", "user_quota_mb": "250"})
    assert modulo.cuota_usuario_mb() == 250


def test_un_valor_invalido_no_guarda_nada(modulo, monkeypatch):
    """Se validan todos antes de escribir: guardar la mitad dejaría una
    configuración a medias sin que se sepa cuál se aplicó."""
    monkeypatch.delenv("USER_QUOTA_MB", raising=False)
    monkeypatch.delenv("MIN_FREE_DISK_MB", raising=False)
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")

    respuesta = cliente.post(
        "/admin/ajustes",
        data={"accion": "guardar", "user_quota_mb": "250", "min_free_disk_mb": "abc"},
        follow_redirects=True,
    )
    assert "número entero" in respuesta.data.decode()
    assert modulo.cuota_usuario_mb() == 0, "no debería haber guardado nada"


def test_el_campo_fijado_en_env_sale_bloqueado(crear_app):
    modulo = crear_app(USER_QUOTA_MB=512)
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")

    cuerpo = cliente.get("/admin/ajustes").data.decode()
    assert "disabled" in cuerpo
    assert "Fijado en .env" in cuerpo

    # Y un POST que intente saltárselo no cambia nada.
    cliente.post("/admin/ajustes", data={"accion": "guardar", "user_quota_mb": "1"})
    assert modulo.cuota_usuario_mb() == 512


def test_restablecer_desde_la_pantalla(modulo, monkeypatch):
    monkeypatch.delenv("USER_QUOTA_MB", raising=False)
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")

    cliente.post("/admin/ajustes", data={"accion": "guardar", "user_quota_mb": "250"})
    assert modulo.cuota_usuario_mb() == 250

    cliente.post("/admin/ajustes", data={"accion": "restablecer"})
    assert modulo.cuota_usuario_mb() == 0


def test_activar_y_desactivar_https_desde_ajustes(modulo):
    """Es lo que se pedía poder hacer sin buscarlo: el interruptor está en
    Ajustes, junto al resto."""
    import tls

    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")

    cuerpo = cliente.get("/admin/ajustes").data.decode()
    assert "activar_https" in cuerpo

    cliente.post(
        "/admin/ajustes",
        data={"accion": "activar_https", "nombres_https": "192.168.1.152"},
    )
    assert tls.esta_activo(modulo.DATA_DIR) is True

    cliente.post("/admin/ajustes", data={"accion": "desactivar_https"})
    assert tls.esta_activo(modulo.DATA_DIR) is False


def test_el_limite_de_subida_guardado_se_aplica_a_una_subida(modulo, monkeypatch):
    """El caso completo: se cambia el límite desde la web y la siguiente subida
    ya lo respeta, sin reiniciar."""
    import io as _io

    monkeypatch.delenv("MAX_UPLOAD_MB", raising=False)
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")

    cliente.post("/admin/ajustes", data={"accion": "guardar", "max_upload_mb": "1"})

    respuesta = cliente.post(
        "/upload",
        data={
            "files": (_io.BytesIO(b"x" * (2 * 1024 * 1024)), "grande.bin"),
            "visibility": "private_me",
        },
        content_type="multipart/form-data",
        # Desde fuera de la LAN: dentro, el límite no se aplica (lan_sin_limite).
        environ_base={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert respuesta.status_code == 413


# ── Robustez de config.ini ────────────────────────────────────────────────────

def test_una_errata_en_config_ini_no_tumba_cada_peticion(caplog):
    """El `fallback` de getint() solo cubre que la opción NO esté. Si está pero
    con algo que no es un número, getint lanza ValueError; y como valor() se
    consulta en cada petición, eso daría un 500 en todas —incluida
    /api/health, con lo que docker-update.sh revertiría una versión buena— por
    una simple errata al editar el fichero.
    """
    import configparser

    roto = configparser.ConfigParser()
    roto.read_string(
        "[server]\n"
        "min_free_disk_mb = mil\n"
        "user_quota_mb = 2048\n"
        "[security]\n"
        "min_password_length = doce\n"
    )

    valores = ajustes.valores_de_fabrica(roto)

    # Los inválidos caen al valor por defecto del código, que siempre vale.
    assert valores["min_free_disk_mb"] == 1024
    assert valores["min_password_length"] == 12
    # Los válidos se respetan.
    assert valores["user_quota_mb"] == 2048
    # Y los ausentes también.
    assert valores["login_max_attempts"] == 10


def test_la_errata_se_avisa_una_sola_vez(caplog):
    """Se resuelve al importar, no por petición: si no, el aviso se repetiría
    en cada visita y el log sería inservible."""
    import configparser
    import logging

    roto = configparser.ConfigParser()
    roto.read_string("[server]\nmin_free_disk_mb = mil\n")

    with caplog.at_level(logging.WARNING, logger="httpWebServer"):
        ajustes.valores_de_fabrica(roto)

    avisos = [r for r in caplog.records if "min_free_disk_mb" in r.getMessage()]
    assert len(avisos) == 1
    assert "no es un número" in avisos[0].getMessage()


def test_valor_no_relee_config_ini_en_cada_llamada(datos, monkeypatch):
    """FABRICA se resuelve al importar. Además de evitar la excepción, quita un
    parseo de configparser de cada petición."""
    monkeypatch.delenv("MIN_FREE_DISK_MB", raising=False)

    def explota(*args, **kwargs):
        raise AssertionError("valor() no debe tocar config.ini")

    monkeypatch.setattr(ajustes._config, "getint", explota)
    assert ajustes.valor("min_free_disk_mb", datos) == 1024


# ── Ajustes de sí/no ──────────────────────────────────────────────────────────

def test_un_booleano_sale_de_fabrica_como_bool(datos, monkeypatch):
    monkeypatch.delenv("LAN_SIN_LIMITE", raising=False)
    assert ajustes.valor("lan_sin_limite", datos) is True
    assert ajustes.es_booleano("lan_sin_limite") is True


def test_un_booleano_guardado_a_false_se_respeta(datos, monkeypatch):
    """El caso que rompe si se trata como un entero: False y 0 se guardan
    igual, pero al leerlos hay que devolver un bool, no un 0."""
    monkeypatch.delenv("LAN_SIN_LIMITE", raising=False)
    ajustes.guardar(datos, {"lan_sin_limite": False})
    assert ajustes.valor("lan_sin_limite", datos) is False
    assert ajustes.origen("lan_sin_limite", datos) == "guardado"


def test_un_booleano_del_entorno_admite_las_formas_de_siempre(datos, monkeypatch):
    for texto, esperado in (("false", False), ("0", False), ("no", False),
                            ("true", True), ("1", True), ("sí", True)):
        monkeypatch.setenv("LAN_SIN_LIMITE", texto)
        assert ajustes.valor("lan_sin_limite", datos) is esperado, texto


def test_un_booleano_ilegible_no_tumba_nada(datos, monkeypatch):
    monkeypatch.setenv("LAN_SIN_LIMITE", "quizá")
    assert ajustes.valor("lan_sin_limite", datos) is True   # el de fábrica


def test_desmarcar_la_casilla_se_guarda(modulo, monkeypatch):
    """Una casilla desmarcada no envía nada; el campo oculto de la plantilla es
    lo que hace que «no» se guarde en vez de dejar el valor anterior."""
    monkeypatch.delenv("LAN_SIN_LIMITE", raising=False)
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")

    # Como lo manda el navegador: el oculto siempre, la casilla solo si va marcada.
    cliente.post("/admin/ajustes", data={"accion": "guardar", "lan_sin_limite": ["0"]})
    assert modulo.lan_sin_limite() is False

    cliente.post("/admin/ajustes", data={"accion": "guardar", "lan_sin_limite": ["0", "1"]})
    assert modulo.lan_sin_limite() is True


def test_la_pantalla_pinta_la_casilla(modulo):
    cliente = modulo.app.test_client()
    entrar(cliente, "root", "contrasena-de-instalacion")
    cuerpo = cliente.get("/admin/ajustes").get_data(as_text=True)
    assert 'type="checkbox"' in cuerpo
    assert 'name="lan_sin_limite"' in cuerpo
