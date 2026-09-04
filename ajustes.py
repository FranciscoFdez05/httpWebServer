"""Ajustes que se pueden cambiar desde la interfaz, sin tocar ficheros.

Hay tres sitios de donde puede salir el valor de un ajuste, y el orden importa:

    1. El entorno (.env)      — manda sobre todo. Es la configuración de la
                                máquina, y quien la pone quiere que no se la
                                cambien desde la web. En la pantalla de Ajustes
                                estos salen bloqueados y diciendo por qué.
    2. Este fichero           — lo que el administrador guarda desde la web.
                                Vive en el volumen de datos porque es el único
                                sitio donde el contenedor puede escribir:
                                config.ini se monta en solo lectura y .env ni
                                siquiera está dentro del contenedor.
    3. config.ini             — los valores de fábrica.

Los valores se leen en cada petición, no al arrancar. Con varios workers de
gunicorn, un ajuste guardado en memoria solo lo vería el worker que atendió el
formulario: los demás seguirían con el valor viejo y el resultado dependería de
a qué worker cayera cada petición. Leer del fichero lo ven todos por igual, y
una caché por fecha de modificación hace que el coste sea un stat().
"""

import configparser
import json
import logging
import os
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# El mismo logger que app.py, para que los avisos salgan en el mismo sitio.
log = logging.getLogger("httpWebServer")

_config = configparser.ConfigParser()
_config.read(os.path.join(BASE_DIR, "config.ini"))


# Cada ajuste declara de dónde sale su valor de fábrica (config.ini), con qué
# variable de entorno se fija, y cómo describirlo en la pantalla de Ajustes.
DEFINICIONES = {
    "max_upload_mb": {
        "seccion": "server",
        "env": "MAX_UPLOAD_MB",
        "defecto": 0,
        "grupo": "Subidas",
        "etiqueta": "Tamaño máximo por subida (MB)",
        "ayuda": "0 = sin límite. Ponle un número si el servidor está expuesto: "
                 "quien pueda subir puede llenarte el disco.",
        "minimo": 0,
    },
    "lan_sin_limite": {
        "seccion": "server",
        "env": "LAN_SIN_LIMITE",
        "tipo": "bool",
        "defecto": True,
        "grupo": "Subidas",
        "etiqueta": "Sin límite de tamaño desde la red local",
        "ayuda": "El límite de arriba solo se aplica a quien entra desde fuera "
                 "de la red local. Desde una IP privada (192.168.x, 10.x, "
                 "172.16-31.x) o desde la propia máquina se sube sin tope. Si "
                 "hay un proxy inverso delante y BEHIND_PROXY no está a true, "
                 "todas las peticiones parecen venir de la LAN y el límite no "
                 "se aplicaría a nadie.",
    },
    "user_quota_mb": {
        "seccion": "server",
        "env": "USER_QUOTA_MB",
        "defecto": 0,
        "grupo": "Subidas",
        "etiqueta": "Cuota por usuario (MB)",
        "ayuda": "0 = sin cuota. Es el total que puede tener subido cada persona.",
        "minimo": 0,
    },
    "min_free_disk_mb": {
        "seccion": "server",
        "env": "MIN_FREE_DISK_MB",
        "defecto": 1024,
        "grupo": "Subidas",
        "etiqueta": "Espacio libre reservado (MB)",
        "ayuda": "Una subida que fuera a bajar de aquí se rechaza y se borra. "
                 "No lo pongas a 0: si el disco se llena del todo, SQLite deja "
                 "de poder escribir y no funciona nada, ni siquiera borrar lo "
                 "que sobra para recuperarse.",
        "minimo": 0,
    },
    "min_password_length": {
        "seccion": "security",
        "env": "MIN_PASSWORD_LENGTH",
        "defecto": 12,
        "grupo": "Seguridad",
        "etiqueta": "Longitud mínima de contraseña",
        "ayuda": "Solo afecta a las contraseñas nuevas; las que ya existen "
                 "siguen valiendo.",
        "minimo": 4,
        "maximo": 128,
    },
    "login_max_attempts": {
        "seccion": "security",
        "env": "LOGIN_MAX_ATTEMPTS",
        "defecto": 10,
        "grupo": "Seguridad",
        "etiqueta": "Intentos fallidos antes de bloquear",
        "ayuda": "Se cuentan por dirección IP. El bloqueo se levanta solo.",
        "minimo": 1,
        "maximo": 1000,
    },
    "login_window_minutes": {
        "seccion": "security",
        "env": "LOGIN_WINDOW_MINUTES",
        "defecto": 15,
        "grupo": "Seguridad",
        "etiqueta": "Duración del bloqueo (minutos)",
        "ayuda": "También es la ventana en la que se cuentan los intentos.",
        "minimo": 1,
        "maximo": 1440,
    },
}

GRUPOS = ("Subidas", "Seguridad")

# Caché por ruta: {ruta: (mtime_ns, datos)}. Evita releer y parsear el JSON en
# cada petición sin dejar de ver los cambios de otro worker.
_cache = {}


# Los ajustes son enteros salvo los que se declaran "bool". Un booleano no
# tiene mínimo ni máximo y en la pantalla sale como una casilla, no como un
# campo numérico; el resto del módulo se comporta igual para los dos.
CIERTOS = ("1", "true", "yes", "on", "si", "sí")
FALSOS = ("0", "false", "no", "off")


def es_booleano(clave):
    return DEFINICIONES[clave].get("tipo") == "bool"


def valores_de_fabrica(config=None):
    """Lee config.ini una sola vez y deja un entero válido para cada ajuste.

    El `fallback` de getint() SOLO cubre que la opción no esté: si está pero
    con algo que no es un número («min_free_disk_mb = mil», una errata al
    editar), getint lanza ValueError. Como valor() se consulta en cada
    petición, esa excepción daría un 500 en todas —incluida /api/health, con
    lo que docker-update.sh daría por muerta una versión que funciona— y por
    una errata en un fichero de configuración.

    Aquí se resuelve al importar: se avisa una vez y se sigue con el valor por
    defecto del código, que siempre es válido.
    """
    config = _config if config is None else config
    valores = {}
    for clave, definicion in DEFINICIONES.items():
        leer = config.getboolean if es_booleano(clave) else config.getint
        try:
            valores[clave] = leer(
                definicion["seccion"], clave, fallback=definicion["defecto"]
            )
        except ValueError:
            bruto = config.get(definicion["seccion"], clave, fallback="")
            log.warning(
                "config.ini: [%s] %s = %r no es un %s; se usa %s.",
                definicion["seccion"], clave,
                bruto, "sí/no" if es_booleano(clave) else "número",
                definicion["defecto"],
            )
            valores[clave] = definicion["defecto"]
    return valores


FABRICA = valores_de_fabrica()


def ruta(data_dir):
    return os.path.join(data_dir, "ajustes.json")


def leer_archivo(data_dir):
    """Lo guardado desde la web. {} si no hay nada o si está corrupto."""
    p = ruta(data_dir)
    try:
        mtime = os.stat(p).st_mtime_ns
    except OSError:
        _cache.pop(p, None)
        return {}

    guardado = _cache.get(p)
    if guardado and guardado[0] == mtime:
        return guardado[1]

    try:
        with open(p, encoding="utf-8") as f:
            datos = json.load(f)
        if not isinstance(datos, dict):
            datos = {}
    except (OSError, ValueError):
        # Un fichero corrupto no puede tumbar el servidor: se ignora y se sigue
        # con los valores de fábrica, que siempre son válidos.
        datos = {}

    _cache[p] = (mtime, datos)
    return datos


def _de_entorno(clave):
    definicion = DEFINICIONES[clave]
    valor = os.environ.get(definicion["env"], "").strip()
    return valor or None


def origen(clave, data_dir):
    """De dónde sale ahora mismo: 'entorno', 'guardado' o 'fábrica'."""
    if _de_entorno(clave) is not None:
        return "entorno"
    if clave in leer_archivo(data_dir):
        return "guardado"
    return "fábrica"


def fijado_en_entorno(clave):
    """Si está en .env no se puede cambiar desde la web, y la pantalla lo dice
    en vez de dejar que guardes algo que luego se ignora."""
    return _de_entorno(clave) is not None


def _a_booleano(bruto, defecto):
    texto = str(bruto).strip().lower()
    if texto in CIERTOS:
        return True
    if texto in FALSOS:
        return False
    return defecto


def _convertir(clave, bruto, defecto):
    """Lo leído del entorno o del JSON, al tipo del ajuste.

    Nunca lanza: si el valor guardado no vale (un .env con LAN_SIN_LIMITE=quizá,
    un JSON editado a mano), se usa el de fábrica en vez de dejar el servidor
    devolviendo un 500 en cada petición.
    """
    if es_booleano(clave):
        return _a_booleano(bruto, defecto)
    try:
        return int(str(bruto).strip())
    except (TypeError, ValueError):
        return defecto


def valor(clave, data_dir):
    defecto = FABRICA[clave]

    del_entorno = _de_entorno(clave)
    if del_entorno is not None:
        return _convertir(clave, del_entorno, defecto)

    guardado = leer_archivo(data_dir)
    if clave in guardado:
        return _convertir(clave, guardado[clave], defecto)

    return defecto


def todos(data_dir):
    return {clave: valor(clave, data_dir) for clave in DEFINICIONES}


def validar(clave, bruto):
    """Devuelve (valor, error). El error es el texto que verá el usuario."""
    definicion = DEFINICIONES[clave]
    texto = str(bruto).strip()

    if es_booleano(clave):
        # Una casilla manda "1" cuando está marcada y nada cuando no; el campo
        # oculto que la acompaña en la plantilla convierte ese "nada" en "0",
        # así una casilla desmarcada se guarda en vez de ignorarse.
        if texto.lower() in CIERTOS:
            return True, None
        if texto.lower() in FALSOS or not texto:
            return False, None
        return None, f"«{definicion['etiqueta']}» solo puede estar marcado o no."

    if not texto:
        return None, f"«{definicion['etiqueta']}» no puede quedar vacío."
    try:
        numero = int(texto)
    except ValueError:
        return None, f"«{definicion['etiqueta']}» tiene que ser un número entero."

    minimo = definicion.get("minimo")
    maximo = definicion.get("maximo")
    if minimo is not None and numero < minimo:
        return None, f"«{definicion['etiqueta']}» no puede ser menor que {minimo}."
    if maximo is not None and numero > maximo:
        return None, f"«{definicion['etiqueta']}» no puede ser mayor que {maximo}."
    return numero, None


def guardar(data_dir, cambios):
    """Escribe los ajustes. Solo guarda lo que NO esté fijado en el entorno."""
    os.makedirs(data_dir, exist_ok=True)
    datos = dict(leer_archivo(data_dir))
    for clave, valor_nuevo in cambios.items():
        if clave in DEFINICIONES and not fijado_en_entorno(clave):
            datos[clave] = valor_nuevo

    # Se escribe en un temporal del mismo directorio y se sustituye de forma
    # atómica: si el proceso muriera a media escritura, un JSON cortado por la
    # mitad dejaría la configuración ilegible en el siguiente arranque.
    descriptor, temporal = tempfile.mkstemp(dir=data_dir, prefix=".ajustes_")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(temporal, ruta(data_dir))
    except Exception:
        try:
            os.remove(temporal)
        except OSError:
            pass
        raise
    _cache.pop(ruta(data_dir), None)
    return datos


def restablecer(data_dir):
    """Borra lo guardado: se vuelve a los valores de fábrica de config.ini."""
    try:
        os.remove(ruta(data_dir))
    except FileNotFoundError:
        pass
    _cache.pop(ruta(data_dir), None)
