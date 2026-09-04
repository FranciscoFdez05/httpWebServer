#!/usr/bin/env sh
# Actualiza la instalación en marcha y la deja comprobada.
#
# La actualización a mano era `git pull && ./docker-up.sh`, con cuatro cosas que
# solo se descubrían tarde:
#
#   1. `config.ini` está versionado y se monta desde el host. Si lo has editado
#      en el servidor, el pull da un conflicto en mitad de la actualización.
#      Aquí se comprueba ANTES de tocar nada y se explica que lo que hay que
#      editar en producción es `.env`, que no se versiona.
#   2. Nada verificaba que la versión nueva arrancase. El contenedor se quedaba
#      arriba con `restart: unless-stopped` reiniciándose en bucle, y te
#      enterabas al abrir la web. Aquí se espera a que `/api/health` responda,
#      que es una comprobación de verdad: consulta la base de datos y el
#      directorio de subidas.
#   3. `docker compose build` sin etiqueta deja solo la imagen nueva, así que
#      volver atrás era reconstruir desde el código anterior, varios minutos con
#      la base de datos ya migrada. Ahora cada versión queda etiquetada
#      (`httpwebserver:<version>`) y la vuelta atrás es inmediata.
#   4. Este script se actualiza a sí mismo. El `git pull` reemplaza el fichero
#      que el intérprete está leyendo, y `sh` guarda un desplazamiento dentro de
#      él: si el fichero nuevo tiene otro tamaño, las órdenes que quedan por leer
#      se descolocan y la actualización se queda a medias por un error de
#      sintaxis absurdo. Por eso lo primero que hace es ejecutarse desde una
#      copia (ver abajo).
#
# Si el arranque no responde, se vuelve solo a la imagen anterior. Lo que este
# script NO deshace es la migración del esquema que hace `init_db()` al
# arrancar: para eso está la copia previa de la base de datos, cuya ruta se
# imprime antes de reconstruir.
#
# Uso: ./docker-update.sh [--sin-pull]
set -e

# El directorio del proyecto se fija antes que nada: la copia de la que se
# reejecuta vive en /tmp, así que allí `dirname "$0"` ya no sirve.
FS_PROYECTO="${FS_PROYECTO:-$(cd "$(dirname "$0")" && pwd)}"
export FS_PROYECTO
cd "$FS_PROYECTO"

# Reejecutarse desde una copia: lo que corre es una foto del script, y el pull
# de más abajo puede reemplazar el original sin descolocar esta ejecución. Si no
# se pudiera crear la copia se sigue igualmente: es una protección, no un
# requisito.
if [ -z "$FS_UPDATE_COPIA" ]; then
    copia=$(mktemp "${TMPDIR:-/tmp}/docker-update.XXXXXX" 2>/dev/null) || copia=""
    if [ -n "$copia" ] && cp "$0" "$copia" 2>/dev/null; then
        FS_UPDATE_COPIA="$copia"
        export FS_UPDATE_COPIA
        codigo=0
        sh "$copia" "$@" || codigo=$?
        rm -f "$copia"
        exit "$codigo"
    fi
fi

SIN_PULL=0
[ "$1" = "--sin-pull" ] && SIN_PULL=1

# Con sudo, el git pull deja los ficheros del repositorio a nombre de root y la
# siguiente actualización como usuario normal falla por permisos. Docker no
# necesita sudo cuando el usuario pertenece al grupo docker:
#   sudo usermod -aG docker "$USER"     (y volver a entrar en la sesión)
if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: no ejecutes docker-update.sh con sudo." >&2
    echo "       Ejecútalo como tu usuario normal: ./docker-update.sh" >&2
    exit 1
fi

SERVICIO="httpwebserver"
IMAGEN="httpwebserver"
ESPERA_SALUD=90   # segundos que se le dan a la versión nueva para responder

aviso()  { printf '\n\033[33m%s\033[0m\n' "$*"; }
error()  { printf '\n\033[31m%s\033[0m\n' "$*" >&2; }
paso()   { printf '\n\033[36m── %s\033[0m\n' "$*"; }

# git ignorando el bit de ejecución.
#
# Un "chmod +x docker-update.sh" —que en Linux hace falta si el fichero llegó
# sin el bit puesto— cuenta para git como una modificación del fichero. La
# guarda de más abajo lo veía como "tienes cambios sin confirmar" y se negaba a
# actualizar: el propio acto de hacer ejecutable el script impedía ejecutarlo,
# y el diff que se imprimía decía "0 insertions(+), 0 deletions(-)", que no
# ayuda nada a entender qué pasa.
#
# core.fileMode=false hace que git no mire los permisos. Solo afecta a estas
# comprobaciones, no a la configuración del repositorio.
git_sin_modos() {
    git -c core.fileMode=false "$@"
}

# La versión vive en app.py (`__version__`), que es lo que devuelve también
# /api/health: así la etiqueta de la imagen y lo que responde el contenedor no
# pueden desincronizarse.
version_del_codigo() {
    sed -n 's/^__version__ = "\(.*\)"/\1/p' app.py | head -n 1
}

# Puerto: mismo criterio que la app. Manda .env (no versionado, sobrevive a los
# pull); si no lo define, config.ini. Se resuelve en shell puro a propósito, sin
# depender de que el host tenga python, igual que hace docker-up.sh.
puerto_configurado() {
    valor=""
    if [ -f .env ]; then
        valor=$(sed -n 's/^[[:space:]]*PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' .env | head -n 1)
    fi
    if [ -z "$valor" ]; then
        valor=$(awk -F= '
            /^[[:space:]]*\[/ { section=$0; gsub(/[][ \t\r]/, "", section); next }
            section == "server" {
                key=$1; gsub(/[ \t\r]/, "", key)
                if (key == "port") { val=$2; gsub(/[ \t\r]/, "", val); print val; exit }
            }
        ' config.ini 2>/dev/null)
    fi
    case "$valor" in
        ''|*[!0-9]*) echo 8000 ;;
        *) echo "$valor" ;;
    esac
}

# El host puede no tener curl ni wget. Como último recurso se pregunta desde
# dentro del propio contenedor, donde python siempre está: la imagen trae
# healthcheck.py, que es exactamente la misma comprobación que hace Docker.
#
# Se prueba https y luego http porque el esquema depende de si el HTTPS está
# activado, y ese estado puede cambiar justo en esta actualización. Sondear
# solo http daría por muerta una versión que arrancó bien con TLS.
#
# --insecure / --no-check-certificate: el certificado lo firma la CA local, que
# el host no tiene instalada. Lo que se comprueba aquí es que la aplicación
# responde, no a quién pertenece el certificado, y la conexión es a localhost.
comprobar_salud() {
    for esquema in https http; do
        if command -v curl >/dev/null 2>&1; then
            curl -fsS --insecure "${esquema}://localhost:${PORT}/api/health" 2>/dev/null && return 0
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- --no-check-certificate "${esquema}://localhost:${PORT}/api/health" 2>/dev/null && return 0
        else
            docker compose exec -T "$SERVICIO" python /app/healthcheck.py >/dev/null 2>&1 && return 0
        fi
    done
    return 1
}

# ── 1. Comprobaciones previas ─────────────────────────────────────────────────
paso "Comprobando el estado local"

if ! command -v docker >/dev/null 2>&1; then
    error "docker no está instalado o no está en el PATH."
    exit 1
fi

if [ ! -f .env ]; then
    error "No hay .env. Esto es una instalación nueva: usa ./docker-up.sh."
    exit 1
fi
# `config.ini` se distribuye con el código, así que editarlo en el servidor
# choca con cada actualización. El puerto tiene su variable de entorno, y ese es
# el canal que sobrevive a los pulls.
if [ "$SIN_PULL" -eq 0 ] && ! git_sin_modos diff --quiet -- config.ini 2>/dev/null; then
    error "config.ini tiene cambios locales y el pull chocaría con ellos."
    cat <<'FIN'

  config.ini son los valores de fábrica y se actualizan con el código. Para
  configurar esta instalación usa .env, que no se versiona y por tanto
  sobrevive a las actualizaciones (ver .env.example).

  Para ver qué has cambiado:      git diff config.ini
  Para pasarlo a .env:            añade  PORT=<tu puerto>  a .env
  Para descartarlo y actualizar:  git checkout -- config.ini && ./docker-update.sh

FIN
    exit 1
fi

# Cualquier otro cambio local hace fallar el pull a mitad. Mejor pararse aquí,
# con el servicio antiguo todavía intacto, que a medio camino.
if [ "$SIN_PULL" -eq 0 ] && ! git_sin_modos diff --quiet 2>/dev/null; then
    error "Hay cambios locales sin confirmar en el repositorio:"
    git_sin_modos --no-pager diff --stat >&2
    echo >&2
    echo "  Guárdalos (git stash), confírmalos o descártalos antes de actualizar." >&2
    echo "  Si solo quieres reconstruir sin traer nada: ./docker-update.sh --sin-pull" >&2
    exit 1
fi

VERSION_ANTERIOR=$(version_del_codigo)
[ -n "$VERSION_ANTERIOR" ] || VERSION_ANTERIOR="desconocida"
echo "Versión instalada: $VERSION_ANTERIOR"

# Primera actualización con este script: la imagen que está corriendo se
# construyó sin etiqueta de versión, así que no habría a dónde volver. Se
# etiqueta ahora, mientras todavía es la buena.
if [ "$VERSION_ANTERIOR" != "desconocida" ] \
   && ! docker image inspect "${IMAGEN}:${VERSION_ANTERIOR}" >/dev/null 2>&1; then
    imagen_actual=$(docker compose images -q "$SERVICIO" 2>/dev/null | head -n 1)
    if [ -n "$imagen_actual" ]; then
        docker tag "$imagen_actual" "${IMAGEN}:${VERSION_ANTERIOR}" 2>/dev/null \
            && echo "Etiquetada la imagen en marcha como ${IMAGEN}:${VERSION_ANTERIOR} (para poder volver a ella)."
    fi
fi

# ── 2. Copia de seguridad de la base de datos ─────────────────────────────────
# Antes de nada, porque `init_db()` migra el esquema al arrancar la versión
# nueva y eso no lo deshace volver a la imagen anterior. Se usa la API `backup`
# de sqlite3 y no un `cp`: copiar en caliente un fichero SQLite con escrituras
# en curso da una copia rota justo cuando más falta hace.
RUTA_BACKUP=""
if docker compose ps --status running --services 2>/dev/null | grep -qx "$SERVICIO"; then
    paso "Copia de seguridad de la base de datos"
    RUTA_BACKUP=$(docker compose exec -T -e V="$VERSION_ANTERIOR" "$SERVICIO" python -c '
import datetime, os, sqlite3, sys
os.makedirs("/data/backups", exist_ok=True)
sello = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
destino = "/data/backups/app_pre-%s_%s.db" % (os.environ.get("V", "x"), sello)
origen = sqlite3.connect("/data/app.db")
copia = sqlite3.connect(destino)
origen.backup(copia)
origen.close()
copia.close()
sys.stdout.write(destino)
' 2>/dev/null) || RUTA_BACKUP=""

    # Los certificados van aparte de la base de datos, pero perder ca.key
    # obliga a crear una CA nueva y a reinstalarla en TODOS los dispositivos.
    # Es lo más caro de recuperar de todo el volumen.
    if docker compose exec -T "$SERVICIO" test -f /data/tls/ca.key 2>/dev/null; then
        mkdir -p backups
        if docker compose cp "${SERVICIO}:/data/tls" backups/tls >/dev/null 2>&1; then
            chmod -R go-rwx backups/tls 2>/dev/null || true
            echo "Certificados copiados a backups/tls (incluye la clave de la CA:"
            echo "  guárdala como guardarías una contraseña)."
        fi
    fi

    if [ -n "$RUTA_BACKUP" ]; then
        # También al host: si algún día el problema es el propio volumen, una
        # copia que vive dentro de él no sirve de nada.
        mkdir -p backups
        docker compose cp "${SERVICIO}:${RUTA_BACKUP}" "backups/$(basename "$RUTA_BACKUP")" >/dev/null 2>&1 || true
        echo "Copia guardada en:"
        echo "  volumen: $RUTA_BACKUP"
        [ -f "backups/$(basename "$RUTA_BACKUP")" ] && echo "  host:    backups/$(basename "$RUTA_BACKUP")"
    else
        aviso "No se pudo copiar la base de datos. Se continúa, pero sin red de seguridad
ante una migración de esquema. Ctrl+C ahora si prefieres pararlo."
    fi
else
    aviso "El servicio no está en marcha: no hay base de datos que copiar."
fi

# ── 3. Traer los cambios ──────────────────────────────────────────────────────
if [ "$SIN_PULL" -eq 0 ]; then
    paso "Descargando la versión nueva"
    git pull --ff-only
fi

VERSION_NUEVA=$(version_del_codigo)
[ -n "$VERSION_NUEVA" ] || VERSION_NUEVA="desconocida"

if [ "$VERSION_NUEVA" = "$VERSION_ANTERIOR" ]; then
    aviso "Ya estabas en la $VERSION_NUEVA. Se reconstruye igualmente."
else
    echo "Actualizando: $VERSION_ANTERIOR → $VERSION_NUEVA"
    if [ -f CHANGELOG.md ]; then
        paso "Novedades de la $VERSION_NUEVA"
        awk '/^## \[/{n++} n==1{print} n==2{exit}' CHANGELOG.md
    fi
fi

# ── 4. Puerto y versión para docker compose ───────────────────────────────────
# PORT alimenta el mapeo host:contenedor de docker-compose.yml y APP_VERSION la
# etiqueta de la imagen. Exportarlos aquí es lo que hace posible la vuelta atrás
# del paso 7.
PORT=$(puerto_configurado)
export PORT
export APP_VERSION="$VERSION_NUEVA"

# Las IPs de este servidor en la LAN, para que el certificado HTTPS pueda
# cubrirlas: desde dentro del contenedor solo se ve la IP del puente de Docker.
HOST_LAN_IPS=$(
    if command -v hostname >/dev/null 2>&1 && hostname -I >/dev/null 2>&1; then
        hostname -I
    elif command -v ip >/dev/null 2>&1; then
        ip -4 -o addr show scope global | awk '{split($4,a,"/"); printf "%s ", a[1]}'
    fi
)
export HOST_LAN_IPS

# ── 5. Construir y levantar ───────────────────────────────────────────────────
# El volumen es "external": lo crea el script, no Compose. Así "docker compose
# down -v" no puede borrar los archivos subidos ni la base de datos. Crearlo es
# idempotente: si ya existe, no hace nada.
docker volume create httpWebServer >/dev/null

paso "Construyendo la imagen ${IMAGEN}:${VERSION_NUEVA}"
docker compose build

paso "Levantando"
docker compose up -d

# ── 6. Comprobar que arranca de verdad ────────────────────────────────────────
# /api/health consulta la base de datos y el directorio de subidas, y devuelve
# 503 si algo falla; esperar aquí distingue «el contenedor está arriba» de «la
# aplicación funciona».
paso "Esperando a que responda (hasta ${ESPERA_SALUD}s)"

sano=0
i=0
while [ "$i" -lt "$ESPERA_SALUD" ]; do
    if comprobar_salud >/dev/null 2>&1; then
        sano=1
        break
    fi
    i=$((i + 1))
    printf '.'
    sleep 1
done
printf '\n'

if [ "$sano" -eq 1 ]; then
    paso "Actualización correcta"
    comprobar_salud || true
    if docker compose exec -T "$SERVICIO" test -f /data/tls/enabled 2>/dev/null; then
        ESQUEMA="https"
    else
        ESQUEMA="http"
    fi
    printf '\n\nVersión %s en marcha en el puerto %s (%s).\n' "$VERSION_NUEVA" "$PORT" "$ESQUEMA"
    if [ "$VERSION_NUEVA" != "$VERSION_ANTERIOR" ] && [ "$VERSION_ANTERIOR" != "desconocida" ]; then
        printf 'Si aparece algún problema más tarde:  APP_VERSION=%s docker compose up -d --no-build\n' \
            "$VERSION_ANTERIOR"
    fi
    exit 0
fi

# ── 7. Vuelta atrás ───────────────────────────────────────────────────────────
error "La versión $VERSION_NUEVA no responde tras ${ESPERA_SALUD}s. Volviendo atrás."

echo
echo "Últimas líneas del log:"
docker compose logs --tail 40 "$SERVICIO" 2>&1 || true

if [ "$VERSION_ANTERIOR" != "desconocida" ] \
   && docker image inspect "${IMAGEN}:${VERSION_ANTERIOR}" >/dev/null 2>&1; then
    paso "Levantando de nuevo la $VERSION_ANTERIOR"
    APP_VERSION="$VERSION_ANTERIOR" docker compose up -d --no-build
    aviso "Se ha vuelto a la $VERSION_ANTERIOR. El código del repositorio SÍ está actualizado:
para dejarlo también como estaba, ejecuta  git checkout v${VERSION_ANTERIOR}"
else
    error "No hay imagen etiquetada de la $VERSION_ANTERIOR; no se puede volver sola.
Para reconstruir la anterior:  git checkout v${VERSION_ANTERIOR} && ./docker-update.sh --sin-pull"
fi

echo
echo "  Si la versión nueva llegó a migrar el esquema, los datos ya están migrados y"
echo "  volver a la imagen anterior no basta. La copia previa a la migración está en:"
echo
if [ -n "$RUTA_BACKUP" ]; then
    echo "      $RUTA_BACKUP   (dentro del volumen httpWebServer)"
    if [ -f "backups/$(basename "$RUTA_BACKUP")" ]; then
        echo "      backups/$(basename "$RUTA_BACKUP")   (en este servidor)"
    fi
else
    echo "      (no se pudo hacer la copia en este intento)"
fi
cat <<'FIN'

  Para restaurarla, para el contenedor y sustituye /data/app.db por esa copia:

      docker compose stop
      docker compose cp <copia> httpwebserver:/data/app.db
      docker compose start

FIN
exit 1
