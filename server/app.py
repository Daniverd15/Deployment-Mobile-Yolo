#!/usr/bin/env python3
"""API de deteccion de placas vehiculares -- YOLOv8 + EasyOCR.

Basado en snippet/app.py del repositorio del profesor, con los ajustes que
hicieron falta para el despliegue real en EC2 (t3.micro) y para consumirla
desde un iPhone con Expo Go:

  * limite de subida ampliado (las fotos del iPhone pesan varios MB)
  * orientacion EXIF aplicada al decodificar: cv2 la ignora y el iPhone la usa
    casi siempre, asi que el servidor recibia la foto girada y no detectaba nada
  * deteccion a imgsz=1280 en vez de los 640 por defecto de ultralytics
  * la imagen se reduce antes de inferir, pero el OCR lee sobre la original
  * OCR reforzado: recorte ampliado, variantes de preprocesado y correccion
    por formato de placa colombiana (AAA123 / AAA12A)
  * /health para el semaforo de conexion de la app y /web/ para no perder
    la demo del laboratorio anterior
  * CORS abierto: Expo Go sirve la app desde un origen distinto

Requiere: fastapi uvicorn ultralytics easyocr opencv-python-headless pillow numpy python-multipart
"""

import io
import os
import re
import base64
import logging
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

import cv2
import easyocr
import numpy as np
import requests
from PIL import Image, ImageOps
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
# request.form() devuelve el UploadFile de Starlette, no el de FastAPI
from starlette.datastructures import UploadFile as StarletteUploadFile
from ultralytics import YOLO

# -------------------------
# Config / Logging
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("yolo-plates")

MODEL_PATH = os.getenv("MODEL_PATH", "best.pt")
OCR_LANGS = os.getenv("OCR_LANGS", "en").split(",")
# Umbral de deteccion. Medido sobre pruebas/, las placas reales que se leen bien
# puntuan entre 0.546 y 0.962, asi que subirlo de 0.25 a 0.45 no cuesta ninguna
# (10 lecturas correctas con ambos valores) y descarta detecciones debiles.
# Hace falta porque cualquier rectangulo claro con texto -- una etiqueta pegada al
# marco de un televisor, por ejemplo -- se parece a una placa, y si su lectura
# encaja por casualidad en el formato colombiano se anunciaba como placa.
# No subir de 0.50: la placa FRL260 de la escena de trafico puntua 0.546.
CONF_THRESH = float(os.getenv("CONF_THRESH", "0.45"))
MAX_SIDE = int(os.getenv("MAX_SIDE", "1280"))        # lado maximo antes de inferir
# Lado maximo de la imagen que se conserva para el OCR. Una foto de iPhone de
# 4032x3024 son 36 MB en memoria solo como array, y con la decodificacion y las
# copias una peticion llegaba a reservar mas de 100 MB. En una instancia de
# 911 MB eso obliga a paginar en cada foto (se midieron 3.5 millones de paginas
# traidas de swap), y la app acaba mostrando "Sin conexion" porque el servidor
# tarda mas que su timeout.
#
# Medido con fotos de 4032 px sobre cuatro casos del banco:
#     1280 -> 15.1 s en total, 5 placas correctas
#     1920 -> 26.9 s,          5 correctas
#     2560 -> 31.9 s,          5 correctas
# Leer el OCR a mas resolucion DUPLICA el tiempo sin acertar una placa mas: los
# recortes mas grandes hacen a EasyOCR mucho mas lento. De ahi el 1280.
#
# Matiz honesto: las imagenes del banco se reescalaron a 4032 px desde
# originales de ~1200, asi que no tienen detalle real que ganar. Con una foto
# genuinamente detallada de un carro lejano podria aportar algo. Si alguna vez
# se comprueba, subir este valor (o MAX_ORIGINAL en el entorno) es todo el
# cambio necesario.
MAX_ORIGINAL = int(os.getenv("MAX_ORIGINAL", "1280"))
# Resolucion de inferencia de YOLO. El defecto de ultralytics es 640, que
# encoge una placa de 80 px a 40 y la pierde. A 1280 la escena de trafico pasa
# de 2 a 3 placas y la confianza de la moto sube de 0.62 a 0.85, por 0.2 s mas:
# nada al lado de los segundos que tarda el OCR.
IMGSZ = int(os.getenv("IMGSZ", "1280"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "32"))  # por campo del formulario
# Microservicio de PaddleOCR (ver server/ocr_paddle.py). Va aparte por conflicto
# de dependencias y por memoria. Si no responde, se usa solo EasyOCR.
PADDLE_URL = os.getenv("PADDLE_URL", "http://127.0.0.1:8091")
PADDLE_TIMEOUT = float(os.getenv("PADDLE_TIMEOUT", "20"))
# Confianza minima para hacerle caso a PaddleOCR. Medido sobre el banco, sus
# lecturas correctas puntuan entre 0.93 y 0.999, mientras que el texto impreso
# mal leido ("MEDELLIN" -> "EUELLIH") se queda en 0.63 y llegaba a colarse como
# placa valida (UEL11H). 0.80 separa los dos casos con margen.
PADDLE_CONF_MIN = float(os.getenv("PADDLE_CONF_MIN", "0.80"))
# EasyOCR como respaldo de PaddleOCR. Se puede apagar (USAR_EASYOCR=0) porque
# los tres modelos no caben en 911 MB: ver la guia para la comparativa de las
# tres configuraciones posibles.
USAR_EASYOCR = os.getenv("USAR_EASYOCR", "1") == "1"
WEB_DIR = os.getenv("WEB_DIR", "/home/ubuntu/bike")  # demo anterior, se conserva
RETURN_IMAGE = os.getenv("RETURN_IMAGE", "1") == "1"

# En CPU con 2 vCPU, mas hilos que nucleos empeora la latencia.
os.environ.setdefault("OMP_NUM_THREADS", "2")

# -------------------------
# App init
# -------------------------
app = FastAPI(
    title="Detector de Placas -- YOLOv8 + EasyOCR",
    description="API del proyecto de Ciencia de Datos (UNAB). Consumida desde Expo Go en iPhone.",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # la app movil no tiene un origen fijo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Carga de modelos (una sola vez, al arrancar)
# -------------------------
logger.info("Cargando modelo YOLOv8 desde %s ...", MODEL_PATH)
model = YOLO(MODEL_PATH)
logger.info("Modelo YOLOv8 cargado. Clases: %s", model.names)

# EasyOCR se carga solo cuando hace falta. Ocupa ~400 MB y en esta instancia de
# 911 MB eso es la diferencia entre responder rapido y irse a swap. Como
# PaddleOCR resuelve la mayoria de las placas, en una sesion normal EasyOCR no
# llega a cargarse nunca; cuando aparece una placa dificil, se carga una vez
# (~7 s) y ya se queda.
_reader = None


def obtener_reader():
    global _reader
    if _reader is None:
        logger.info("Inicializando EasyOCR bajo demanda (idiomas=%s) ...", OCR_LANGS)
        _reader = easyocr.Reader(OCR_LANGS, gpu=False)
        logger.info("EasyOCR listo.")
    return _reader

# -------------------------
# Helpers de OCR
# -------------------------
PLACA_ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# Texto impreso en las placas colombianas que no forma parte de la matricula
RUIDO = {
    "COLOMBIA", "BOGOTA", "BOGOTADC", "DC", "MEDELLIN", "CALI", "CUCUTA",
    "BUCARAMANGA", "BARRANQUILLA", "CARTAGENA", "PEREIRA", "MANIZALES",
}

# Confusiones tipicas del OCR, segun si la posicion debe ser letra o digito
A_LETRA = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A"}
A_DIGITO = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5",
            "B": "8", "G": "6", "A": "4"}

# Carro: AAA123 | Moto: AAA12A
RE_CARRO = re.compile(r"^[A-Z]{3}[0-9]{3}$")
RE_MOTO = re.compile(r"^[A-Z]{3}[0-9]{2}[A-Z]$")


def _solo_alnum(texto: str) -> str:
    return "".join(ch for ch in texto if ch.isalnum()).upper()


def _corregir_formato(texto: str) -> Tuple[str, bool]:
    """Corrige confusiones letra/digito usando el formato de placa colombiana.

    Devuelve (texto_corregido, cumple_formato).
    """
    t = _solo_alnum(texto)
    if len(t) != 6:
        return t, False

    # Intento como placa de carro: 3 letras + 3 digitos
    carro = "".join(A_LETRA.get(c, c) for c in t[:3]) + "".join(A_DIGITO.get(c, c) for c in t[3:])
    if RE_CARRO.match(carro):
        return carro, True

    # Intento como placa de moto: 3 letras + 2 digitos + 1 letra
    moto = (
        "".join(A_LETRA.get(c, c) for c in t[:3])
        + "".join(A_DIGITO.get(c, c) for c in t[3:5])
        + A_LETRA.get(t[5], t[5])
    )
    if RE_MOTO.match(moto):
        return moto, True

    return t, False


def _variantes(roi_bgr: np.ndarray) -> List[np.ndarray]:
    """Variantes de preprocesado del recorte, para darle opciones al OCR.

    Solo dos, y cada una se gano su sitio: midiendo cual era LA UNICA que
    acertaba una placa del banco, RGB aporta `IJO387` y CLAHE aporta `SMU002`.
    Habia una tercera variante (Otsu) que no fue la unica en acertar ninguna
    placa y costaba ~10 s sobre el banco completo: se quito.
    """
    gris = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gris)
    return [
        cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2RGB),
        cv2.cvtColor(clahe, cv2.COLOR_GRAY2RGB),
    ]


def _es_ruido(texto: str) -> bool:
    """`COLOMBIA` sale del OCR como COLONBIA, COLOMBLA, C0L0MBIA... hay que comparar difuso."""
    if texto in RUIDO:
        return True
    return any(SequenceMatcher(None, texto, palabra).ratio() >= 0.72 for palabra in RUIDO)


def _cajas_utiles(resultado) -> List[Tuple[float, str, float]]:
    """Filtra las cajas del OCR y deja solo las de la matricula, ordenadas izq -> der.

    Dos filtros, en este orden:
      1. altura: la matricula va en letra grande; `COLOMBIA` y la ciudad van en
         letra chica debajo. Se descarta lo que mida menos del 60% de la caja
         mas alta.
      2. ruido: comparacion difusa contra el texto impreso conocido.
    """
    cajas = []
    for caja, texto, conf in resultado:
        limpio = _solo_alnum(texto)
        if not limpio or len(limpio) > 8:
            continue
        xs = [p[0] for p in caja]
        ys = [p[1] for p in caja]
        cajas.append({
            "x": min(xs),
            "alto": max(ys) - min(ys),
            "texto": limpio,
            "conf": float(conf),
        })

    if not cajas:
        return []

    alto_max = max(c["alto"] for c in cajas)
    cajas = [c for c in cajas if c["alto"] >= 0.6 * alto_max]
    cajas = [c for c in cajas if not _es_ruido(c["texto"])]
    cajas.sort(key=lambda c: c["x"])
    return [(c["x"], c["texto"], c["conf"]) for c in cajas]


def _candidatos(textos: List[str]) -> List[Tuple[str, float]]:
    """Arma las lecturas posibles de la placa a partir de las cajas del OCR.

    Una placa colombiana es `AAA (emblema) 123`. El emblema del centro y los
    tornillos de las esquinas ensucian la lectura: `WUF (*) 62C` llega como
    `IWUF262C` (tornillo + emblema leidos como caracteres). Por eso se prueban
    varias formas de recortar los 6 caracteres reales.

    Devuelve (candidato, bonus). El bonus desempata entre lecturas que cumplen
    formato: respetar los limites de las cajas del OCR es mejor senal que
    recortar una ventana a mitad de una caja.

    Nota: se probo ademas generar candidatos saltando 1-2 caracteres en la
    frontera letras/numeros (para el emblema) y una segunda pasada de OCR con
    `width_ths` bajo. Medido sobre las 9 placas de `pruebas/`, empeoro de 7 a 5
    aciertos: crea varios candidatos con formato valido y la misma puntuacion,
    y el desempate termina siendo arbitrario. Se descarto a proposito.
    """
    candidatos: dict = {}

    def agregar(cadena: str, bonus: float) -> None:
        if cadena and bonus > candidatos.get(cadena, -1.0):
            candidatos[cadena] = bonus

    agregar("".join(textos), 0.5)                                   # todas las cajas
    agregar("".join(t for t in textos if len(t) > 1), 0.4)          # sin cajas de 1 caracter
    for i in range(len(textos)):                                    # quitando una caja
        agregar("".join(textos[:i] + textos[i + 1:]), 0.3)

    for cadena in list(candidatos):                                 # ventanas de 6
        for i in range(len(cadena) - 6 + 1):
            agregar(cadena[i:i + 6], 0.0)

    return list(candidatos.items())


def _sustituciones(original: str, corregido: str) -> int:
    return sum(1 for a, b in zip(original, corregido) if a != b)


def _cajas_de_paddle(roi_bgr: np.ndarray) -> Optional[list]:
    """Pide la lectura al microservicio de PaddleOCR.

    Devuelve las cajas en el formato de easyocr.readtext, o None si el servicio
    no esta disponible: su ausencia nunca debe tumbar una peticion.
    """
    try:
        ok, buffer = cv2.imencode(".png", roi_bgr)
        if not ok:
            return None
        respuesta = requests.post(
            f"{PADDLE_URL}/leer",
            data=buffer.tobytes(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=PADDLE_TIMEOUT,
        )
        if not respuesta.ok:
            return None
        cajas = respuesta.json().get("cajas") or []
        return [c for c in cajas if float(c[2]) >= PADDLE_CONF_MIN]
    except Exception as exc:
        logger.debug("PaddleOCR no disponible: %s", exc)
        return None


def _mejor_candidato(cajas) -> Tuple[Optional[str], float, bool]:
    """Aplica el filtro de ruido y la correccion por formato a unas cajas de OCR."""
    utiles = _cajas_utiles(cajas)
    if not utiles:
        return None, 0.0, False

    textos = [c[1] for c in utiles]
    conf = sum(c[2] for c in utiles) / len(utiles)

    mejor_texto, mejor_puntaje, mejor_valido = None, -1.0, False
    for candidato, bonus in _candidatos(textos):
        corregido, valido = _corregir_formato(candidato)
        if valido:
            puntaje = 10.0 + bonus + conf - 0.3 * _sustituciones(candidato, corregido)
        else:
            puntaje = conf - abs(len(corregido) - 6)
        if puntaje > mejor_puntaje:
            mejor_texto, mejor_puntaje, mejor_valido = corregido, puntaje, valido

    return mejor_texto, conf, mejor_valido


def leer_placa(roi_bgr: np.ndarray) -> Tuple[Optional[str], float, bool]:
    """Lee la placa del recorte y devuelve (texto, confianza, cumple_formato).

    Usa los dos motores en cascada porque fallan en placas distintas. Medido
    sobre los 12 recortes de `pruebas/`:

        EasyOCR    9 correctas, 3 erroneas
        PaddleOCR  8 correctas, 0 erroneas, 4 sin lectura
        cascada   11 correctas, 1 erronea

    PaddleOCR va primero por preciso (lee `WUF62C`, que EasyOCR nunca acerto) y
    porque cuando resuelve evita las tres pasadas de EasyOCR. Si el
    microservicio no esta levantado, `_cajas_de_paddle` devuelve None y todo
    sigue funcionando solo con EasyOCR.
    """
    if roi_bgr is None or roi_bgr.size == 0:
        return None, 0.0, False

    # Los recortes pequenos leen mal: se agrandan a ~240 px de alto.
    # Los muy grandes (placa cercana en una foto de 4032 px) se acotan a 640:
    # mas alla de ahi el OCR no mejora y si se vuelve lento.
    h = roi_bgr.shape[0]
    if h < 240:
        escala = min(240.0 / max(h, 1), 4.0)
        roi_bgr = cv2.resize(roi_bgr, None, fx=escala, fy=escala, interpolation=cv2.INTER_CUBIC)
    elif h > 640:
        escala = 640.0 / h
        roi_bgr = cv2.resize(roi_bgr, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)

    # --- 1) PaddleOCR: preciso y de una sola pasada ---
    # Medido sobre los 12 recortes del banco: 8 correctas y CERO erroneas, con
    # 4 sin lectura. Cuando devuelve algo con formato valido casi siempre
    # acierta, asi que se le hace caso y se ahorra el resto del trabajo.
    cajas_paddle = _cajas_de_paddle(roi_bgr)
    if cajas_paddle:
        texto, conf, valido = _mejor_candidato(cajas_paddle)
        if valido:
            return texto, conf, True

    # --- 2) EasyOCR: cubre justo los huecos que deja PaddleOCR ---
    if not USAR_EASYOCR:
        # Sin respaldo: se devuelve lo que dijo Paddle aunque no cumpla formato,
        # para que quede en `detalles` como diagnostico.
        if cajas_paddle:
            return _mejor_candidato(cajas_paddle)
        return None, 0.0, False

    mejor_texto: Optional[str] = None
    mejor_conf = 0.0
    mejor_puntaje = -1.0

    for variante in _variantes(roi_bgr):
        try:
            resultado = obtener_reader().readtext(variante, allowlist=PLACA_ALLOWLIST)
        except Exception as exc:  # defensivo: una variante mala no debe tumbar la peticion
            logger.warning("OCR fallo en una variante: %s", exc)
            continue

        cajas = _cajas_utiles(resultado)
        if not cajas:
            continue

        textos = [c[1] for c in cajas]
        conf = sum(c[2] for c in cajas) / len(cajas)

        for candidato, bonus in _candidatos(textos):
            corregido, valido = _corregir_formato(candidato)
            if valido:
                # +10 asegura que cualquier lectura con formato de placa le gane a
                # una sin formato; se penaliza corregir muchos caracteres.
                puntaje = 10.0 + bonus + conf - 0.3 * _sustituciones(candidato, corregido)
            else:
                # sin formato valido: sirve solo como respaldo, y cuanto mas se
                # aleje de los 6 caracteres, peor
                puntaje = conf - abs(len(corregido) - 6)

            if puntaje > mejor_puntaje:
                mejor_texto, mejor_conf, mejor_puntaje = corregido, conf, puntaje

        # Formato valido y OCR seguro: no hace falta probar mas variantes
        if mejor_puntaje >= 10.0 and mejor_conf >= 0.6:
            break

    # puntaje >= 10 significa que la lectura elegida cumple formato de placa
    return (mejor_texto or None), mejor_conf, mejor_puntaje >= 10.0


# -------------------------
# Helpers de imagen
# -------------------------
def imagen_a_base64_jpg(img_bgr: np.ndarray, calidad: int = 85) -> str:
    _, buffer = cv2.imencode(".jpg", img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), calidad])
    return base64.b64encode(buffer).decode("utf-8")


def _decodificar(datos: bytes) -> Optional[np.ndarray]:
    """Decodifica la imagen respetando la orientacion EXIF.

    `cv2.imdecode` ignora el tag de orientacion. Las fotos del iPhone casi
    siempre lo traen, asi que el servidor recibia la imagen girada 90 grados
    mientras el telefono la mostraba derecha. Y una placa girada el detector no
    la ve: medido sobre el banco, 90 grados pasa de 1 caja a **cero**.
    """
    try:
        imagen = Image.open(io.BytesIO(datos))
        imagen = ImageOps.exif_transpose(imagen)  # aplica el tag y lo elimina
        return cv2.cvtColor(np.array(imagen.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception:
        # Respaldo por si Pillow no reconoce el formato
        nparr = np.frombuffer(datos, np.uint8)
        return cv2.imdecode(nparr, cv2.IMREAD_COLOR)


def _limpiar_base64(cadena: str) -> bytes:
    if cadena.startswith("data:image"):
        cadena = cadena.split(",", 1)[1]
    cadena = cadena.strip().replace("\n", "").replace("\r", "").replace(" ", "+")
    faltante = (-len(cadena)) % 4  # padding tolerante: el cliente a veces lo recorta
    return base64.b64decode(cadena + "=" * faltante)


def _reducir(frame: np.ndarray) -> np.ndarray:
    """Las fotos del iPhone llegan a 4032 px; YOLO trabaja a 640 de todos modos."""
    h, w = frame.shape[:2]
    lado = max(h, w)
    if lado <= MAX_SIDE:
        return frame
    escala = MAX_SIDE / lado
    return cv2.resize(frame, (int(w * escala), int(h * escala)), interpolation=cv2.INTER_AREA)


# -------------------------
# Nucleo de deteccion
# -------------------------
# Si la orientacion correcta falla, se prueban los giros de 90 en 90. Con EXIF
# ya aplicado esto casi nunca hace falta, pero cubre las fotos sin tag o con el
# tag mal puesto, que es justo cuando el detector devuelve cero cajas. Cada
# intento cuesta ~0.3 s y solo se paga cuando no se encontro nada.
_GIROS = [
    (cv2.ROTATE_90_CLOCKWISE, "90 horario"),
    (cv2.ROTATE_90_COUNTERCLOCKWISE, "90 antihorario"),
    (cv2.ROTATE_180, "180"),
]


def _detectar_en_alguna_orientacion(original: np.ndarray):
    """Devuelve (original, reducida, resultados) de la primera orientacion con placas."""
    frame = _reducir(original)
    resultados = model.predict(source=frame, conf=CONF_THRESH, imgsz=IMGSZ, verbose=False)
    if resultados and len(resultados[0].boxes) > 0:
        return original, frame, resultados

    for giro, nombre in _GIROS:
        girada = cv2.rotate(original, giro)
        frame_girado = _reducir(girada)
        intento = model.predict(source=frame_girado, conf=CONF_THRESH, imgsz=IMGSZ, verbose=False)
        if intento and len(intento[0].boxes) > 0:
            logger.info("Placas encontradas tras girar la imagen %s", nombre)
            return girada, frame_girado, intento

    return original, frame, resultados


def _acotar_original(imagen: np.ndarray) -> np.ndarray:
    """Limita la imagen que se guarda para el OCR, por memoria (ver MAX_ORIGINAL)."""
    lado = max(imagen.shape[:2])
    if lado <= MAX_ORIGINAL:
        return imagen
    escala = MAX_ORIGINAL / lado
    return cv2.resize(imagen, (int(imagen.shape[1] * escala), int(imagen.shape[0] * escala)),
                      interpolation=cv2.INTER_AREA)


def detectar(original: np.ndarray) -> dict:
    """Detecta sobre la imagen reducida, pero lee el texto sobre la original.

    YOLO no gana nada con mas de 1280 px (trabaja a 640 internamente) y reducir
    la imagen es lo que mantiene la inferencia en pocos segundos en una CPU
    modesta. El OCR es justo al reves: cada pixel cuenta, y una placa lejana en
    una foto de iPhone (4032 px) pierde dos tercios de su ancho al reducirla.
    Por eso la caja se detecta en la imagen chica y se recorta de la grande.
    """
    original = _acotar_original(original)
    original, frame, resultados = _detectar_en_alguna_orientacion(original)
    # factor para llevar coordenadas de la imagen reducida a la original
    escala = original.shape[1] / frame.shape[1]

    if not resultados or len(resultados[0].boxes) == 0:
        return {
            "success": True,
            "placas": [],
            "num_placas": 0,
            "detalles": [],
            "image": imagen_a_base64_jpg(frame) if RETURN_IMAGE else None,
            "message": "No se detectaron placas",
        }

    r = resultados[0]
    cajas = r.boxes.xyxy.cpu().numpy()
    confs = r.boxes.conf.cpu().numpy()
    clases = r.boxes.cls.cpu().numpy()

    placas: List[str] = []
    detalles: List[dict] = []
    h, w = frame.shape[:2]

    for i, caja in enumerate(cajas):
        x1, y1, x2, y2 = map(int, caja)
        cls_id = int(clases[i]) if len(clases) > i else None
        etiqueta = model.names.get(cls_id, "objeto") if cls_id is not None else "objeto"
        conf_box = float(confs[i]) if len(confs) > i else 0.0

        # Se amplia el recorte: los bordes de la placa ayudan al OCR. El 8% no es
        # arbitrario: a imgsz=1280 las cajas salen mas ajustadas y con el 4%
        # anterior se recortaba el primer caracter (JNU540 se leia UNU540,
        # WUF62C se leia MUF...). Medido sobre pruebas/: 4% -> 7 aciertos,
        # 8% -> 9, 12% -> 8, 18% -> 8.
        margen_x = int((x2 - x1) * 0.08)
        margen_y = int((y2 - y1) * 0.10)
        x1c, y1c = max(0, x1 - margen_x), max(0, y1 - margen_y)
        x2c, y2c = min(w, x2 + margen_x), min(h, y2 + margen_y)

        # El recorte para el OCR sale de la imagen original, a resolucion completa
        alto_o, ancho_o = original.shape[:2]
        ox1, oy1 = max(0, int(x1c * escala)), max(0, int(y1c * escala))
        ox2, oy2 = min(ancho_o, int(x2c * escala)), min(alto_o, int(y2c * escala))
        roi = original[oy1:oy2, ox1:ox2].copy()

        texto = None
        conf_ocr = 0.0
        formato_ok = False
        if any(k in etiqueta.lower() for k in ["placa", "plate", "license", "matricula"]):
            texto, conf_ocr, formato_ok = leer_placa(roi)
            # Solo se anuncia lo que cumple formato colombiano. Una foto girada o
            # muy borrosa produce lecturas como "IHTOHJ": la app las diria en voz
            # alta como si fueran una placa. Se conservan en `detalles` para
            # diagnostico, pero fuera de `placas`.
            if texto and formato_ok and texto not in placas:
                placas.append(texto)
                cv2.putText(frame, texto, (x1, max(30, y1 - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)

        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(frame, f"{etiqueta} {conf_box:.2f}", (x1, min(h - 8, y2 + 22)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 0), 2)

        detalles.append({
            "label": etiqueta,
            "conf_deteccion": round(conf_box, 3),
            "texto": texto,
            "conf_ocr": round(conf_ocr, 3),
            "formato_valido": formato_ok,
            "bbox": [x1, y1, x2, y2],
        })

    logger.info("Placas detectadas: %s", placas)
    return {
        "success": True,
        "placas": placas,
        "num_placas": len(placas),
        "detalles": detalles,
        "image": imagen_a_base64_jpg(frame) if RETURN_IMAGE else None,
        "message": "OK" if placas else "Se detecto la placa pero la lectura no tiene formato valido",
    }


# -------------------------
# Rutas
# -------------------------
@app.get("/")
def home():
    return {
        "message": "YOLOv8 + OCR server running",
        "version": app.version,
        "endpoints": ["/predict/", "/predict_json/", "/health", "/docs", "/web/"],
    }


@app.get("/health")
def health():
    """Lo usa la app movil para el semaforo de conexion antes de disparar la foto."""
    return {
        "status": "ok",
        "modelo": os.path.basename(MODEL_PATH),
        "clases": list(model.names.values()),
        "motores": {
            "paddleocr": _cajas_de_paddle(np.zeros((32, 96, 3), np.uint8)) is not None,
            "easyocr_habilitado": USAR_EASYOCR,
            "easyocr_cargado": _reader is not None,
        },
    }


# El formulario se parsea a mano (en vez de con File()/Form()) solo para poder
# subir el limite de 1 MB por campo que trae Starlette: FastAPI llama a
# request.form() sin argumentos y no deja configurarlo por ruta. Con el limite
# por defecto, mandar una foto de celular como base64 en un formulario devuelve
# "Field exceeded maximum size of 1024KB" (una foto de iPhone a calidad alta
# son ~1.9 MB en base64). openapi_extra conserva el formulario de /docs, que se
# pierde al quitar File()/Form().
ESQUEMA_PREDICT = {
    "requestBody": {
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "format": "binary", "title": "Foto del vehiculo"},
                        "image_base64": {"type": "string", "title": "Alternativa: imagen en base64"},
                    },
                }
            },
            "application/x-www-form-urlencoded": {
                "schema": {
                    "type": "object",
                    "properties": {"image_base64": {"type": "string"}},
                }
            },
        }
    }
}


@app.post("/predict/", openapi_extra=ESQUEMA_PREDICT)
async def predict(request: Request):
    """Recibe la foto como multipart (`file`) o como base64 (`image_base64`)."""
    try:
        datos: Optional[bytes] = None

        # cubre multipart/form-data y application/x-www-form-urlencoded
        if "form" in (request.headers.get("content-type") or ""):
            async with request.form(max_part_size=MAX_UPLOAD_MB * 1024 * 1024) as formulario:
                archivo = formulario.get("file")
                texto_b64 = formulario.get("image_base64")
                if isinstance(archivo, StarletteUploadFile):
                    datos = await archivo.read()
                elif isinstance(texto_b64, str) and texto_b64:
                    datos = _limpiar_base64(texto_b64)
        else:
            # Tolerante con quien mande JSON a esta ruta en vez de a /predict_json/.
            # Un cuerpo vacio o que no sea JSON debe caer en el 400 de mas abajo,
            # no reventar con un 500.
            try:
                cuerpo = await request.json()
            except Exception:
                cuerpo = None
            texto_b64 = cuerpo.get("image_base64") if isinstance(cuerpo, dict) else None
            if texto_b64:
                datos = _limpiar_base64(texto_b64)

        if datos is None:
            return JSONResponse(status_code=400,
                                content={"success": False, "error": "No se recibio ninguna imagen"})

        frame = _decodificar(datos)
        if frame is None:
            return JSONResponse(status_code=400,
                                content={"success": False, "error": "No se pudo decodificar la imagen"})

        return detectar(frame)

    except Exception as exc:
        logger.exception("Error en /predict/")
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})


@app.post("/predict_json/")
async def predict_json(request: Request):
    """Variante JSON pura: {"image_base64": "..."} -- la que usa la app de iPhone."""
    try:
        body = await request.json()
        image_base64 = body.get("image_base64")
        if not image_base64:
            return JSONResponse(status_code=400,
                                content={"success": False, "error": "Falta image_base64"})

        frame = _decodificar(_limpiar_base64(image_base64))
        if frame is None:
            return JSONResponse(status_code=400,
                                content={"success": False, "error": "No se pudo decodificar la imagen"})

        return detectar(frame)

    except Exception as exc:
        logger.exception("Error en /predict_json/")
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})


# Demo web del laboratorio anterior: se conserva bajo /web/ para no perderla
if os.path.isdir(WEB_DIR):
    app.mount("/web", StaticFiles(directory=WEB_DIR, html=True), name="web")
    logger.info("Demo estatica montada en /web/ desde %s", WEB_DIR)


if __name__ == "__main__":
    import uvicorn

    puerto = int(os.getenv("PORT", "8080"))
    logger.info("Iniciando servidor en 0.0.0.0:%s", puerto)
    uvicorn.run("app:app", host="0.0.0.0", port=puerto, reload=False)
