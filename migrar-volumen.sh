#!/usr/bin/env sh
# Mueve los datos del volumen antiguo (ftp_data) al nuevo (httpWebServer).
#
# El volumen se llamaba ftp_data, de cuando el proyecto tenía otro nombre.
# Cambiar el nombre en docker-compose.yml NO mueve nada: Docker se limita a
# crear un volumen nuevo y vacío, y los archivos subidos, la base de datos, la
# clave de sesión y los certificados se quedan en el viejo. El servidor arranca
# como recién instalado, sin un solo mensaje de error, y es fácil creer que se
# ha perdido todo.
#
# Este script hace la copia. Solo hay que ejecutarlo UNA vez, y solo si venías
# de una instalación anterior; en una instalación nueva no hay nada que migrar.
#
# NO borra el volumen antiguo: se queda como copia de seguridad hasta que
# compruebes que todo está en su sitio. Al final se indica cómo borrarlo.
#
# Uso: ./migrar-volumen.sh
set -e

cd "$(dirname "$0")"

VOLUMEN_NUEVO="httpWebServer"
# Imagen para el contenedor que hace la copia. Es la base del proyecto, así que
# lo normal es que ya esté descargada y no haya que bajar nada.
IMAGEN_COPIA="python:3.12-slim"

aviso()  { printf '\n\033[33m%s\033[0m\n' "$*"; }
error()  { printf '\n\033[31m%s\033[0m\n' "$*" >&2; }
paso()   { printf '\n\033[36m── %s\033[0m\n' "$*"; }

if ! command -v docker >/dev/null 2>&1; then
    error "docker no está instalado o no está en el PATH."
    exit 1
fi

# ── 1. Localizar el volumen antiguo ───────────────────────────────────────────
# Compose antepone el nombre del proyecto, así que puede llamarse "ftp_data" o
# "loquesea_ftp_data" según desde dónde se levantara.
paso "Buscando el volumen antiguo"

ANTIGUO=$(docker volume ls -q 2>/dev/null | grep -E '(^|_)ftp_data$' | head -n 1 || true)

if [ -z "$ANTIGUO" ]; then
    aviso "No hay ningún volumen ftp_data: no hay nada que migrar."
    echo "Si esta es una instalación nueva, es lo esperado. Usa ./docker-up.sh."
    exit 0
fi
echo "Encontrado: $ANTIGUO"

# ── 2. Comprobar que el nuevo no tiene ya datos ───────────────────────────────
# Sobrescribir un volumen con datos sería justo el desastre que este script
# existe para evitar.
if docker volume inspect "$VOLUMEN_NUEVO" >/dev/null 2>&1; then
    CONTENIDO=$(docker run --rm -v "${VOLUMEN_NUEVO}:/v" "$IMAGEN_COPIA" \
        sh -c 'ls -A /v 2>/dev/null | head -n 1' 2>/dev/null || true)
    if [ -n "$CONTENIDO" ]; then
        error "El volumen $VOLUMEN_NUEVO ya existe y tiene datos dentro."
        echo "  No se toca nada: sobrescribirlo podría destruir una instalación buena." >&2
        echo "  Si estás seguro de que ese contenido sobra, bórralo a mano:" >&2
        echo "      docker compose down && docker volume rm $VOLUMEN_NUEVO" >&2
        echo "  y vuelve a ejecutar este script." >&2
        exit 1
    fi
fi

# ── 3. Parar el servicio ──────────────────────────────────────────────────────
# Copiar una base de datos SQLite con escrituras en curso da una copia rota.
paso "Parando el servidor"
docker compose down 2>/dev/null || true

# ── 4. Copiar ─────────────────────────────────────────────────────────────────
paso "Copiando los datos"
echo "  $ANTIGUO  ->  $VOLUMEN_NUEVO"

docker volume create "$VOLUMEN_NUEVO" >/dev/null

# cp -a conserva dueño y permisos, que aquí importan: los ficheros son del
# usuario 1000 de la imagen, y si acabaran siendo de root el contenedor no
# podría escribir en su propio volumen. /desde en solo lectura para que un
# error de tecleo no pueda estropear el original.
docker run --rm \
    -v "${ANTIGUO}:/desde:ro" \
    -v "${VOLUMEN_NUEVO}:/hacia" \
    "$IMAGEN_COPIA" \
    sh -c 'cp -a /desde/. /hacia/ && echo "Copia terminada."'

# ── 5. Comprobar ──────────────────────────────────────────────────────────────
paso "Comprobando la copia"

resumen() {
    docker run --rm -v "${1}:/v:ro" "$IMAGEN_COPIA" \
        sh -c 'find /v -type f | wc -l' 2>/dev/null | tr -d ' \r'
}

FICHEROS_ANTES=$(resumen "$ANTIGUO")
FICHEROS_DESPUES=$(resumen "$VOLUMEN_NUEVO")
echo "Ficheros en el volumen antiguo: $FICHEROS_ANTES"
echo "Ficheros en el volumen nuevo:   $FICHEROS_DESPUES"

if [ "$FICHEROS_ANTES" != "$FICHEROS_DESPUES" ]; then
    error "El recuento no coincide. NO se ha borrado nada: el volumen antiguo
sigue intacto. Revisa el error de arriba antes de continuar."
    exit 1
fi

# La base de datos es lo que de verdad importa que haya llegado.
if docker run --rm -v "${VOLUMEN_NUEVO}:/v:ro" "$IMAGEN_COPIA" \
        test -f /v/app.db 2>/dev/null; then
    echo "La base de datos está en su sitio."
else
    aviso "No se ve /data/app.db en el volumen nuevo. Puede ser normal si el
servidor nunca llegó a arrancar, pero compruébalo antes de borrar el antiguo."
fi

# ── 6. Listo ──────────────────────────────────────────────────────────────────
paso "Migración terminada"
cat <<FIN

  Ya puedes levantar el servidor:

      ./docker-up.sh

  Entra, comprueba que están tus archivos y tus usuarios, y solo ENTONCES
  borra el volumen antiguo, que se ha dejado intacto a propósito:

      docker volume rm $ANTIGUO

  Mientras no lo borres, tienes una copia completa de cómo estaba todo.

FIN
