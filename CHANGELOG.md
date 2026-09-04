# Registro de cambios

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el versionado es [semántico](https://semver.org/lang/es/).

`docker-update.sh` imprime la sección de la versión nueva al actualizar, así
que el primer bloque `## [...]` de este fichero es lo que verá quien despliegue.

## [1.0.0] - 2026-09-04

Primera versión publicada.

### Compartir archivos
- Subida de varios archivos a la vez, sin límite de tamaño por defecto.
- Permisos por archivo: público (descargable sin iniciar sesión), privado solo
  para quien lo sube, o compartido con una lista concreta de usuarios.
  Modificables en cualquier momento.
- Vista previa en el navegador de imágenes, PDF, audio, vídeo y texto. La lista
  de tipos que se sirven en línea es una allowlist explícita: nunca incluye
  HTML, XML ni SVG, que podrían ejecutar código en el mismo origen. El tipo se
  deduce siempre del nombre en el servidor, nunca del `Content-Type` que manda
  el navegador.
- Eliminación de metadatos de imágenes y PDF. Se escribe en un temporal y se
  sustituye de forma atómica, para que un fallo a media escritura no destruya
  el archivo original.
- Usuarios y sesiones: el primer arranque lleva a crear la cuenta de
  administrador. Desde `/admin` se dan de alta usuarios, se resetean
  contraseñas y se eliminan cuentas; los usuarios nuevos deben cambiar su
  contraseña al entrar por primera vez.

### HTTPS en la red local
- Activable desde **Administración → HTTPS**, desactivado por defecto. Crea una
  autoridad certificadora propia y firma con ella el certificado del servidor,
  y ofrece la CA para descargar e instalar en PC y móvil con las instrucciones
  de cada sistema.
- La CA dura 10 años; el certificado del servidor, 825 días (el máximo que
  acepta iOS). El certificado se puede regenerar cuando cambie la IP **sin**
  tocar la CA, así que no hay que reinstalar nada en los dispositivos.
- El SAN incluye las IPs y los nombres por los que se entra, separados como
  corresponde: un navegador que entra por IP no acepta un certificado que solo
  la declare como nombre DNS.
- `/ca.crt` es público a propósito, para que un dispositivo nuevo pueda
  instalar el certificado sin iniciar sesión. La clave privada de la CA nunca
  se sirve y se guarda con permisos `600`.
- La pantalla avisa si el certificado no cubre la dirección por la que se está
  entrando, que es la causa más desconcertante de que el aviso del navegador no
  desaparezca.

### Seguridad
- Protección CSRF en todos los formularios y contraseñas hasheadas.
- Política de contraseñas: longitud mínima configurable (12 por defecto),
  rechazo de las más comunes y de la igual al nombre de usuario.
- Bloqueo por intentos fallidos de login, contado por IP en la base de datos
  para que funcione con varios workers.
- Cabeceras de seguridad: CSP sin `unsafe-inline` (todo el JavaScript vive en
  `static/app.js`, ninguna plantilla lleva `onclick` ni `<script>` en línea),
  `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, y HSTS cuando hay
  TLS. Los archivos servidos llevan una CSP más estricta todavía.
- Soporte de proxy inverso (`BEHIND_PROXY`) y cookies `Secure`, que se activan
  solas cuando hay TLS.
- Reserva de espacio en disco (`min_free_disk_mb`, activa por defecto), límite
  de subida y cuota por usuario, ambos opcionales.

### Despliegue
- `./docker-up.sh` para una instalación nueva: genera el `.env` con una
  `SECRET_KEY` aleatoria y resuelve el puerto sin necesitar Python en el host.
- `./docker-update.sh` para actualizar una instalación en marcha: comprueba el
  estado local, copia la base de datos y los certificados, construye la imagen
  etiquetada con su versión, y **espera a que `/api/health` responda**. Si la
  versión nueva no arranca, vuelve sola a la anterior.
- Imágenes etiquetadas por versión (`httpwebserver:<version>`, en minúsculas
  porque Docker lo exige), que es lo que hace posible volver atrás en segundos
  en vez de reconstruyendo. El contenedor y el volumen se llaman `httpWebServer`.
- `migrar-volumen.sh`: mueve los datos del volumen antiguo (`ftp_data`) al
  nuevo. Renombrar un volumen no mueve nada por sí solo, así que sin esto una
  instalación anterior arrancaría vacía. No borra el volumen viejo, y
  `docker-up.sh` y `docker-update.sh` se paran y avisan si falta la migración.
- El número de versión se muestra en el pie de todas las páginas.
- Endpoint `/api/health`: comprueba la base de datos y que el directorio de
  subidas sea escribible. Devuelve 503 si algo falla.
- Configuración en dos capas: `config.ini` son los valores de fábrica y viaja
  con el código; `.env` es la configuración de cada instalación, no se versiona
  y siempre manda.
- SQLite en modo WAL con `busy_timeout`, para que una subida en curso no tumbe
  otra petición con «database is locked».
- Rotación de los logs de Docker, y batería de tests con CI en GitHub Actions.
