"""Comprobación de salud del contenedor.

Está en un fichero y no en una línea de docker-compose.yml porque el esquema
depende de si el HTTPS está activado, y meter esa lógica en un `python -c`
dentro de una cadena YAML da algo que nadie puede leer ni depurar.

Sale con 0 si la aplicación responde y con 1 si no. Docker lo usa para marcar
el contenedor como healthy o unhealthy.
"""

import os
import ssl
import sys
import urllib.request

PUERTO = os.environ.get("PORT", "8000")

# El certificado no se valida a propósito: está firmado por la CA local, que no
# está en el almacén del contenedor, y lo que se comprueba aquí es que la
# aplicación responde — no a quién pertenece el certificado. La conexión es al
# propio 127.0.0.1, así que no hay nada que un atacante pudiera interceptar.
CONTEXTO = ssl._create_unverified_context()

# Se prueban los dos esquemas en vez de leer el estado del TLS: así la
# comprobación sigue funcionando durante el reinicio en el que se activa o se
# desactiva el HTTPS, que es justo cuando más falta hace.
URLS = (
    f"https://127.0.0.1:{PUERTO}/api/health",
    f"http://127.0.0.1:{PUERTO}/api/health",
)


def main():
    for url in URLS:
        try:
            with urllib.request.urlopen(url, timeout=5, context=CONTEXTO) as respuesta:
                if respuesta.status == 200:
                    return 0
        except Exception:
            continue
    return 1


if __name__ == "__main__":
    sys.exit(main())
