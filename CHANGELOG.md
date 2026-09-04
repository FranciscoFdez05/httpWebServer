# Registro de cambios

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el versionado es [semántico](https://semver.org/lang/es/).

`docker-update.sh` imprime la sección de la versión nueva al actualizar, así
que el primer bloque `## [...]` de este fichero es lo que verá quien despliegue.

## [1.2.3] - 2026-09-04

### Arreglado
- **La flecha de subir y bajar de los campos numéricos de Ajustes salía en
  claro**: un bloque blanco pegado al número, dentro de un campo oscuro. Esa
  flecha la dibuja el navegador, no el CSS de la página, y mientras no se le
  diga de qué color va la interfaz la pinta clara aunque el campo sea negro.
  Ahora el esquema está declarado (`color-scheme: dark`), así que el navegador
  usa su propia versión oscura —y de paso también en las barras de
  desplazamiento y en el botón de elegir fichero.
- La flecha se dibuja dentro del hueco del texto y, con el número alineado a
  la derecha, las dos cosas quedaban tocándose. Ahora hay un margen entre ellas.

## [1.2.2] - 2026-09-04

### Arreglado
- **El `git pull` de la actualización abortaba por el bit de ejecución del
  propio script.** En Linux hay que hacer `chmod +x docker-update.sh` para
  poder ejecutarlo, y eso cuenta para git como una modificación del fichero.
  Las comprobaciones previas ya lo perdonaban a propósito (`core.fileMode=
  false`), pero el `git pull` de dos pasos más abajo no: dejaba pasar la
  actualización para abortarla acto seguido con «your local changes to the
  following files would be overwritten by merge», ya con la copia de seguridad
  hecha y sin que el contenido del fichero hubiera cambiado en ningún momento.
  Ahora el pull ignora los permisos igual que el resto.
- Los tres `.sh` se guardan ya como ejecutables en el repositorio, así que en
  una instalación nueva no hace falta el `chmod +x` que causaba todo esto.

## [1.2.1] - 2026-09-04

### Arreglado
- **`docker-update.sh` se quedaba colgado esperando a que la versión nueva
  respondiera.** La comprobación de salud probaba `https` antes que `http`, y
  pedir `https` a un servidor que está sirviendo `http` no da error: se queda
  esperando. `curl` manda el saludo TLS y espera respuesta; `gunicorn` recibe
  unos bytes binarios, no encuentra el final de la línea de petición y sigue
  leyendo del socket a la espera de más. Los dos esperando al otro, y sin nadie
  que corte, porque el servidor arranca con `--timeout 0` justo para no cortar
  las subidas largas.
  - Ahora se prueba `http` primero. Al revés no pasa: una petición en claro
    contra un servidor con TLS se rechaza al instante.
  - Y cada sonda tiene su tope de 5 s. Cuando lo que se comprueba es si el
    servidor arrancó bien, lo último que se puede dar por hecho es que va a
    contestar.
- La espera del arranque se cuenta con el reloj y no contando vueltas del
  bucle. «Hasta 90s» era en realidad «hasta 90 intentos»: un intento que se
  colgaba no gastaba espera, así que no se llegaba nunca ni al aviso ni a la
  vuelta atrás automática. Se quedaba ahí parado, con un punto en pantalla.
- La comprobación de salud del contenedor (`healthcheck.py`, la que ejecuta
  Docker cada 30 s) probaba los esquemas en el mismo orden y por el mismo
  motivo: con HTTP en claro dejaba clavado uno de los dos workers cinco
  segundos de cada treinta, para nada.
- Cuando una actualización falla y se vuelve a la versión anterior, el aviso
  decía «ejecuta `git checkout v<version>`» y esa etiqueta podía no existir: se
  crean a mano al publicar y no había ninguna en el repositorio, así que el
  comando fallaba justo en el momento en el que uno menos lo necesita. Ahora se
  usa la etiqueta solo si está de verdad, y si no, el commit que había antes
  del `git pull`, que es exactamente el código de la imagen anterior. También
  se dice cómo volver luego a la última versión (`git checkout main`), que con
  un `checkout` a pelo se queda uno con el HEAD desatado sin saberlo.
  - Publicadas además las etiquetas que faltaban: `v1.0.0`, `v1.1.0`, `v1.2.0`.

## [1.2.0] - 2026-09-04

### Cambiado
- **Las subidas escriben en disco una sola vez.** Cada fichero que llegaba se
  guardaba primero en un temporal del sistema y se copiaba después al volumen
  de datos en trozos de 16 KB: dos escrituras completas y una lectura entera de
  más por cada subida. Ahora el temporal se crea ya dentro del directorio de
  subidas, así que guardar es un `rename` y no se copia ni un byte. En una LAN
  rápida el cuello de botella era eso, no la red.
  - Un temporal que no llegue a renombrarse (subida cancelada, red caída,
    rechazo por tamaño) se borra al cerrar la petición, y al arrancar se
    barren los que dejara un corte de luz.
- **Sin límite de tiempo en las subidas, y bien contado.**
  - `gunicorn` ya arrancaba con `--timeout 0`; ahora el servidor local
    (waitress) tampoco cierra la conexión a los 120 s de un bache de red, y
    lee del socket en trozos de 64 KB en vez de 8 KB.
  - La velocidad de la barra de progreso se calcula sobre los últimos 5
    segundos y no como media desde el principio. Al empezar, el navegador
    vuelca de golpe unos megas en el búfer del socket y los da por enviados:
    la media arrancaba disparada y bajaba durante el resto de la subida, que
    se leía como que el servidor iba frenando. No frenaba; era el contador.

### Añadido
- Ajuste **«Sin límite de tamaño desde la red local»** (activado de fábrica).
  El tope por subida está pensado para un servidor expuesto a internet; dentro
  de casa cortaba subidas grandes sin proteger de nada. Desde una IP privada
  (192.168.x, 10.x, 172.16-31.x) o desde la propia máquina se sube sin tope, y
  desde fuera se sigue aplicando el límite configurado.
  - Con un proxy inverso delante y `BEHIND_PROXY` sin poner a `true`, todas las
    peticiones llegan con la IP privada del proxy y parecerían venir de la LAN.
    La pantalla de Ajustes lo advierte.
- La pantalla de Ajustes admite ajustes de sí/no, con interruptor.

### Arreglado
- Los campos numéricos de Ajustes salían con el fondo blanco del navegador: el
  CSS solo daba estilo a los de texto y contraseña.
- Rediseño de la pantalla de Ajustes: cada ajuste es una fila con su
  explicación a la izquierda y el campo alineado a la derecha, agrupados en
  tarjetas. Antes era un bloque estrecho con todo el texto centrado.

## [1.1.0] - 2026-09-04

### Añadido
- Pantalla de **Ajustes**, enlazada desde la barra superior: límites de subida,
  cuota por usuario, reserva de disco, política de contraseñas, bloqueo por
  intentos fallidos, y el interruptor del HTTPS. Antes había que entrar al
  servidor a editar ficheros para cambiar cualquiera de estas cosas.
  - Se aplican **al momento**, sin reiniciar. Los valores se leen en cada
    petición: con varios workers de gunicorn, un ajuste guardado en memoria
    solo lo vería el que atendió el formulario, y el resultado dependería de a
    qué worker cayera cada petición.
  - Lo guardado vive en el volumen de datos, que es el único sitio donde el
    contenedor puede escribir: `config.ini` se monta en solo lectura y `.env`
    ni siquiera está dentro del contenedor.
  - Precedencia `.env` > Ajustes > `config.ini`. Un ajuste fijado en `.env`
    sale **bloqueado** en la pantalla diciendo por qué, en vez de dejar guardar
    algo que luego se ignoraría.
- El número de versión se muestra en el pie de todas las páginas, para poder
  comprobar de un vistazo qué versión está sirviendo el servidor.

### Cambiado
- El HTTPS se activa y desactiva desde **Ajustes**. Estaba escondido detrás de
  Administración y no se encontraba.
- El contenedor y el volumen de Docker pasan a llamarse `httpWebServer`. La
  imagen se queda en `httpwebserver` (minúsculas) porque Docker no admite
  mayúsculas en el nombre de una imagen.
- El volumen de datos se declara `external`, y lo crean los scripts en vez de
  Compose. Así `docker compose down -v` **no puede** borrar los archivos
  subidos, la base de datos ni los certificados: Compose se niega a tocar un
  volumen que no gestiona él. De paso desaparece el aviso «volume already
  exists but was not created by Docker Compose».
- La salida de `docker-up.sh` indica si el servidor quedó en `http` o `https`.

### Corregido
- Una errata en `config.ini` (`min_free_disk_mb = mil`) tumbaba **todas** las
  peticiones. El `fallback` de `getint()` solo cubre que la opción no esté, no
  que sea inválida, y el valor se resolvía en cada petición. Como también
  afectaba a `/api/health`, `docker-update.sh` habría revertido una versión que
  funcionaba por un simple error tipográfico. Ahora se resuelve una sola vez al
  importar: avisa por el log y sigue con el valor por defecto.
- `./docker-update.sh` se negaba a actualizar diciendo «hay cambios locales sin
  confirmar» cuando lo único que había cambiado era el bit de ejecución. Los
  scripts se publicaban sin él, así que en Linux había que hacerles `chmod +x`
  para poder usarlos — y ese `chmod` era justo lo que bloqueaba la
  actualización: hacer el script ejecutable impedía ejecutarlo. Ahora se
  publican ya como ejecutables, y la comprobación ignora los permisos.
- `/data/tls` se creaba con permisos `0755` en instalaciones que ya tenían
  datos. El Dockerfile lo deja en `700`, pero eso solo llega al volumen la
  primera vez que Docker lo crea; si el volumen no está vacío, no copia nada.
  Las claves ya se escribían con `0600`, así que nunca estuvieron expuestas,
  pero el directorio no decía lo mismo que el Dockerfile.

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
- Activable desde la interfaz, desactivado por defecto. Crea una autoridad
  certificadora propia y firma con ella el certificado del servidor,
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
- Imágenes etiquetadas por versión, que es lo que hace posible volver atrás en
  segundos en vez de reconstruyendo.
- Endpoint `/api/health`: comprueba la base de datos y que el directorio de
  subidas sea escribible. Devuelve 503 si algo falla.
- Configuración en dos capas: `config.ini` son los valores de fábrica y viaja
  con el código; `.env` es la configuración de cada instalación, no se versiona
  y siempre manda.
- SQLite en modo WAL con `busy_timeout`, para que una subida en curso no tumbe
  otra petición con «database is locked».
- Rotación de los logs de Docker, y batería de tests con CI en GitHub Actions.
