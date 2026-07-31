# FTP-Server
---
Servidor web para compartir archivos en tu red local: subes, decides quién puede
ver cada archivo (solo tú, usuarios concretos o cualquiera) y lo levantas entero
con un solo comando gracias a Docker.

![Vista principal: listado de archivos con su visibilidad y acciones](img/mainMenu.png)

## ✨ Características ✨
---

- 🔐 **Usuarios y sesiones**: el primer arranque te lleva a crear la cuenta de
  administrador. Desde `/admin` das de alta más usuarios, reseteas contraseñas o
  eliminas cuentas. Los usuarios nuevos están obligados a cambiar su contraseña
  al entrar por primera vez.
- 👁️ **Permisos por archivo**: público (descargable sin iniciar sesión), privado
  solo para ti, o privado compartido con una lista concreta de usuarios. Se puede
  cambiar en cualquier momento desde la vista de permisos.
- 📤 **Subidas sin límite de tamaño**: el tope real lo pone tu disco, no la app.
  Se pueden subir varios archivos a la vez.
- 🔍 **Vista previa en el navegador** de imágenes, PDF, audio, vídeo y texto,
  sin tener que descargar nada. La lista de tipos permitidos es una allowlist
  explícita, para que no se pueda ejecutar HTML o SVG malicioso.
- 🧹 **Borrado de metadatos** de imágenes y PDFs con un clic, útil antes de
  compartir algo públicamente.
- 🛡️ **Seguridad**: protección CSRF en todos los formularios, contraseñas
  hasheadas y nombres de archivo saneados en disco.
- 📜 **Logs por consola**: cada petición queda registrada con IP, método, ruta,
  código de respuesta, usuario y tiempo de proceso.

## 🖥️ Requisitos
---

- 🐳 **Docker** y **Docker Compose** (método recomendado).
- 🐍 **Python 3** en el host: lo usa `docker-up.sh` para leer `config.ini` y
  generar el `.env` inicial. No hace falta instalar nada más, solo el intérprete.

Si prefieres ejecutarlo sin Docker, necesitas Python 3 y las dependencias de
[`requirements.txt`](requirements.txt) (Flask, Flask-Login, Flask-WTF, Pillow,
pypdf y waitress).

## 📦 Guía de instalación ⚙️
---

### 🚀 Con Docker (recomendado)

```bash
git clone https://github.com/FranciscoFdez05/FTP-Server.git
cd FTP-Server
chmod +x docker-up.sh   # solo la primera vez
./docker-up.sh
```

El script se encarga de todo:

1. 🔑 Crea el `.env` a partir de `.env.example` con una `SECRET_KEY` aleatoria,
   si todavía no existe en esta máquina.
2. 🔌 Lee el puerto de `config.ini` (`[server] port`) y lo exporta como `PORT`,
   para que el mapeo de puertos del contenedor use siempre ese mismo valor.
3. 🏗️ Construye la imagen y levanta el contenedor en segundo plano.

### 🔧 A mano (sin el script)

```bash
cp .env.example .env                                     # 1. secretos
python3 -c "import secrets; print(secrets.token_hex(32))" # pega el valor en SECRET_KEY
export PORT=8000                                          # 2. el puerto de config.ini
docker compose up -d --build                              # 3. arrancar
```

Si te saltas el `export PORT`, el mapeo usa `8000` por defecto, así que solo
falla si tu `config.ini` tiene un puerto distinto.

### 🐍 Sin Docker

```bash
pip install -r requirements.txt
python app.py
```

Arranca con **waitress** (multihilo, para que las subidas grandes por LAN no se
atasquen) y muestra por consola la URL local y la de la red:

```
[INFO] ftp-server: Servidor escuchando en http://0.0.0.0:8000
[INFO] ftp-server:   local: http://127.0.0.1:8000
[INFO] ftp-server:   LAN:   http://192.168.1.152:8000
```

## 📋 Guía de uso 🕹️
---

### 🏁 Primer arranque

Entra a `http://<ip-del-servidor>:<puerto>` (por defecto el puerto `8000`). Como
todavía no hay ningún usuario, la app te redirige a `/setup` para que crees la
cuenta de administrador. A partir de ahí ya puedes iniciar sesión.

### 📂 Día a día

| Quiero... | Dónde |
| --- | --- |
| Ver los archivos a los que tengo acceso | `/` (portada) |
| Subir archivos y elegir su visibilidad | **Subir archivo** |
| Cambiar quién puede ver un archivo ya subido | Botón **Permisos** de su fila |
| Ver un archivo sin descargarlo | Botón **Vista previa** |
| Limpiar los metadatos de una imagen o PDF | Botón **Quitar metadatos** |
| Crear usuarios o resetear contraseñas | **Administración** (solo admin) |
| Cambiar mi propia contraseña | **Cambiar contraseña** |

Quien no ha iniciado sesión solo ve los archivos marcados como públicos.

### ⚙️ Configuración

- **Puerto** → `config.ini` (`[server] port`). Es la única fuente de verdad: lo
  leen tanto waitress como gunicorn. Tras cambiarlo, relanza `./docker-up.sh`.
- **SECRET_KEY** → `.env` (nunca se sube a git). Firma las cookies de sesión y
  los tokens CSRF; si la cambias, se cierra la sesión de todo el mundo.
- **Datos** → volumen Docker `ftp_data`, montado en `/data` dentro del
  contenedor (`/data/uploads` para los archivos y `/data/app.db` para la base de
  datos). Sobrevive a `docker compose down` y a reconstruir la imagen.

### 🧰 Comandos útiles

```bash
docker compose ps            # estado del contenedor
docker logs -f ftp-web       # logs en vivo
docker compose down          # parar (sin borrar datos)
./docker-up.sh               # (re)construir y levantar
```

### ⚠️ Nota de seguridad

Está pensado para una red local de confianza: va por HTTP plano, sin
rate-limiting ni 2FA. Si lo expones a internet, ponlo detrás de un proxy inverso
con TLS.

## 🤝 Contribuciones 🤝
---

Las contribuciones son bienvenidas. Si encuentras un fallo o se te ocurre una
mejora:

1. 🍴 Haz un fork del repositorio.
2. 🌿 Crea una rama para tu cambio (`git checkout -b mejora/nueva-funcion`).
3. 💾 Haz commit de tus cambios (`git commit -m "Añade nueva función"`).
4. 🚀 Sube la rama (`git push origin mejora/nueva-funcion`).
5. 📬 Abre un Pull Request explicando qué cambia y por qué.

Para errores o ideas sueltas, abrir un *issue* también vale.

## 📜 Licencia
---
📄 Este proyecto está licenciado bajo la Licencia MIT. Consulta el archivo `LICENSE` para más detalles..

---

**Developed with ❤️ by [Francisco](https://github.com/FranciscoFdez05)**
