#!/usr/bin/env python3
"""Prueba la API de deteccion contra el banco de imagenes de esta carpeta.

La placa esperada va en el nombre del archivo (`JNU540_swift.jpg`, y
`SMU002-SMV098_taxis.jpg` cuando la foto tiene varias), asi que el script se
mantiene solo: agregar una foto nueva es agregar un caso de prueba.

    python pruebas/probar_api.py                       # contra la EC2 por defecto
    python pruebas/probar_api.py --url http://localhost:8080
"""

import argparse
import pathlib
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Falta requests: pip install requests")

URL_POR_DEFECTO = "http://34.225.169.137:8080"
CARPETA = pathlib.Path(__file__).parent


def esperadas(archivo: pathlib.Path) -> list:
    """`SMU002-SMV098_taxis.jpg` -> ['SMU002', 'SMV098']"""
    return archivo.stem.split("_")[0].split("-")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=URL_POR_DEFECTO, help="URL base de la API")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    base = args.url.rstrip("/")

    try:
        salud = requests.get(f"{base}/health", timeout=10)
        salud.raise_for_status()
        print(f"Servidor OK: {salud.json()}\n")
    except Exception as exc:
        print(f"No responde {base}/health -> {exc}")
        print("Revisa que el servicio este arriba y que el puerto este abierto en el Security Group.")
        return 1

    imagenes = sorted(p for p in CARPETA.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if not imagenes:
        print("No hay imagenes de prueba en esta carpeta.")
        return 1

    aciertos = total = 0
    print(f"{'imagen':<28} {'esperado':<16} {'detectado':<26} {'tiempo':>7}  resultado")
    print("-" * 92)

    for imagen in imagenes:
        objetivo = esperadas(imagen)
        inicio = time.time()
        try:
            with imagen.open("rb") as fh:
                respuesta = requests.post(f"{base}/predict/", files={"file": fh}, timeout=args.timeout)
            detectadas = respuesta.json().get("placas", [])
        except Exception as exc:
            detectadas = [f"ERROR: {exc}"]
        demora = time.time() - inicio

        encontradas = sum(1 for placa in objetivo if placa in detectadas)
        aciertos += encontradas
        total += len(objetivo)
        marca = "OK" if encontradas == len(objetivo) else f"{encontradas}/{len(objetivo)}"

        print(f"{imagen.name:<28} {'/'.join(objetivo):<16} {str(detectadas):<26} {demora:>6.1f}s  {marca}")

    print("-" * 92)
    print(f"Placas leidas exactamente: {aciertos}/{total}")
    return 0 if aciertos == total else 2


if __name__ == "__main__":
    raise SystemExit(main())
