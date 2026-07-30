# FTP-Server

Servidor web para compartir archivos en red local (LAN), con login, subida sin
límite de tamaño, y control de visibilidad por archivo (privado, privado
compartido con usuarios concretos, o público sin necesidad de iniciar sesión).

## Requisitos

- Docker y Docker Compose
- Python 3 disponible en el host (lo usa `docker-up.sh` para leer `config.ini`
  y generar el `.env` inicial; no hace falta ningún paquete extra, solo el
  intérprete)

## Arranque rápido

```
./docker-up.sh
```

Ese script hace todo lo necesario:

1. Si no existe `.env` todavía en esta máquina, lo crea a partir de
   `.env.example` con una `SECRET_KEY` aleatoria ya generada (no hace falta
   tocarlo a mano).
2. Lee el puerto configurado en `config.ini` (`[server] port`) y lo exporta
   como variable `PORT`, para que el mapeo de puertos del contenedor use
   siempre ese mismo valor.
3. Construye la imagen y levanta el contenedor en segundo plano
   (`docker compose up -d --build`).

La primera vez que ejecutes `docker-up.sh` puede que necesites darle permiso:

```
chmod +x docker-up.sh
```

## Arranque manual (sin el script)

Si prefieres no usar `docker-up.sh`, o quieres entender qué hace por debajo,
estos son los pasos equivalentes a mano:

1. Crea el archivo de secretos a partir de la plantilla (solo la primera vez
   en esta máquina):
   ```
   cp .env.example .env
   ```
   Edita `.env` y sustituye el valor de `SECRET_KEY` por una clave real y
   larga, por ejemplo generada con:
   ```
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```
2. Comprueba el puerto en `config.ini` (`[server] port = 8000` por defecto).
3. Exporta ese mismo puerto como variable de entorno `PORT`, porque el
   mapeo de puertos de `docker-compose.yml` lo necesita (`${PORT:-8000}`):
   ```
   export PORT=8000
   ```
   (usa el valor real de `config.ini` si no es 8000).
4. Levanta el stack:
   ```
   docker compose up -d --build
   ```

Si te olvidas del paso 3, el mapeo de puertos usa `8000` por defecto, así que
solo falla si tu `config.ini` usa un puerto distinto de 8000 y no lo exportas.

## Primer uso

Al entrar por primera vez a `http://<ip-del-servidor>:<puerto>` (el puerto por
defecto es `8000`, ver `config.ini`), como no hay ningún usuario en la base de
datos todavía, la app redirige sola a `/setup` para crear la cuenta de
administrador. A partir de ahí ya puedes iniciar sesión y crear más usuarios
desde el panel de administración (`/admin`).

## Configuración

- **Puerto**: se cambia únicamente en `config.ini` (`[server] port = ...`).
  Tras editarlo, vuelve a lanzar con `./docker-up.sh` para que el contenedor
  se reconstruya con el mapeo de puertos correcto.
- **SECRET_KEY**: vive en `.env` (no se sube a git). Firma las cookies de
  sesión y los tokens CSRF de los formularios; si cambia, se cierra la sesión
  de todo el mundo. Se genera sola la primera vez, pero puedes editarla a mano
  en `.env` si quieres controlar tú el valor.
- **Datos persistentes**: usuarios, metadatos de archivos y los propios
  archivos subidos viven en el volumen Docker con nombre `ftp_data`
  (`/data` dentro del contenedor), así que sobreviven a
  `docker compose down` / reconstrucciones de la imagen.

## Comandos útiles

```
docker compose ps            # ver estado del contenedor
docker logs -f ftp-web       # logs en vivo
docker compose down          # parar (sin borrar datos)
./docker-up.sh                # (re)construir y levantar
```
