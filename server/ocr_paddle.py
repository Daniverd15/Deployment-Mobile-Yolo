#!/usr/bin/env python3
"""Microservicio de OCR con PaddleOCR, en localhost:8091.

Va aparte del servidor principal por dos razones concretas:

1. Dependencias. Instalar paddleocr en el venv del servicio bajaria numpy de
   2.5.2 a 2.3.5 y metaria un segundo OpenCV (contrib 4.10 junto al headless
   5.0). Eso puede romper torch, ultralytics y EasyOCR.
2. Memoria. PaddleOCR ocupa ~600 MB de RSS. Aislado se puede apagar
   (`systemctl stop ocr-paddle`) si la instancia se queda corta: el servidor
   principal sigue funcionando solo con EasyOCR.

Por que se anadio, medido sobre los 12 recortes del banco de pruebas:

    EasyOCR    9 correctas, 3 erroneas
    PaddleOCR  8 correctas, 0 erroneas, 4 sin lectura
    Fusion    11 correctas, 1 erronea

Los dos motores fallan en placas distintas. PaddleOCR es mas preciso pero lee
menos: cuando devuelve algo con formato valido casi siempre acierta (incluida
`WUF62C`, la placa de moto que EasyOCR nunca leyo bien), y cuando no esta
seguro no devuelve nada. EasyOCR cubre justo esos huecos.

Devuelve las cajas en el mismo formato que `easyocr.readtext` --
`[[poligono, texto, confianza], ...]`-- para que el servidor principal les
aplique su filtro de ruido y su correccion por formato sin logica duplicada.

Se ejecuta con el venv propio:
    /home/ubuntu/exp-venv/bin/uvicorn ocr_paddle:app --host 127.0.0.1 --port 8091
"""

import logging
import os

import numpy as np
from fastapi import FastAPI, Request
from paddleocr import PaddleOCR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ocr-paddle")

app = FastAPI(title="OCR PaddleOCR (interno)", version="1.0.0")

logger.info("Cargando PaddleOCR ...")
# enable_mkldnn=False es obligatorio en esta instancia: con oneDNN activo la
# inferencia revienta con "ConvertPirAttribute2RuntimeAttribute not support".
_ocr = PaddleOCR(
    enable_mkldnn=False,
    use_doc_orientation_classify=False,  # el recorte ya viene derecho
    use_doc_unwarping=False,
    use_textline_orientation=False,
    lang=os.getenv("PADDLE_LANG", "en"),
)
logger.info("PaddleOCR listo.")


@app.get("/health")
def health():
    return {"status": "ok", "motor": "paddleocr"}


@app.post("/leer")
async def leer(request: Request):
    """Recibe el recorte de la placa (PNG/JPEG crudo) y devuelve las cajas de texto."""
    try:
        datos = await request.body()
        if not datos:
            return {"cajas": []}

        # PaddleOCR 3.x acepta un array numpy directamente
        import cv2  # import local: solo existe en este venv

        imagen = cv2.imdecode(np.frombuffer(datos, np.uint8), cv2.IMREAD_COLOR)
        if imagen is None:
            return {"cajas": []}

        resultado = _ocr.predict(imagen)
        if not resultado:
            return {"cajas": []}

        bloque = resultado[0]
        textos = bloque.get("rec_texts", []) or []
        scores = bloque.get("rec_scores", []) or []
        poligonos = bloque.get("rec_polys", []) or bloque.get("dt_polys", []) or []

        cajas = []
        for i, texto in enumerate(textos):
            conf = float(scores[i]) if i < len(scores) else 0.0
            if i < len(poligonos):
                poligono = [[float(p[0]), float(p[1])] for p in poligonos[i]]
            else:
                poligono = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
            cajas.append([poligono, texto, conf])

        return {"cajas": cajas}

    except Exception as exc:
        logger.exception("Error leyendo el recorte")
        return {"cajas": [], "error": str(exc)}
