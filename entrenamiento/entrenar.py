#!/usr/bin/env python3
"""Reentrenamiento del detector de placas, corrigiendo las debilidades medidas.

El modelo que viene en `modelo/best.pt` se entreno asi (sale de sus propios
metadatos, ver `leer_metadatos()` abajo):

    model=yolov8n.pt  imgsz=640  epochs=50  batch=16  degrees=0.0  mosaic=1.0

De ahi salen los dos fallos de deteccion que se midieron con la app:

  * `degrees=0.0` -> el modelo NUNCA vio una placa inclinada ni rotada. Una foto
    girada 90 grados le da CERO detecciones. El servidor ya lo compensa
    aplicando la orientacion EXIF, pero el modelo sigue siendo fragil ante
    fotos tomadas en angulo.
  * `yolov8n` a `imgsz=640` -> el modelo mas pequeno de la familia, entrenado a
    baja resolucion. Las placas lejanas (menos de ~60 px de ancho) no las
    detecta: en la foto de trafico del banco de pruebas encuentra 3 de 5.

Este script entrena con los ajustes que atacan eso, usando YOLO11.

Si prefieres Colab, hay un notebook listo con todo el flujo (incluida la
descarga del dataset y las pruebas de rotacion):

    entrenamiento/entrenar_yolo11_colab.ipynb

Necesita GPU (en Colab: Entorno de ejecucion -> Cambiar tipo de entorno ->
T4 GPU). En CPU no vale la pena intentarlo: son dias.

    pip install ultralytics
    python entrenar.py --data /ruta/data.yaml

El dataset recomendado es `license-plate-recognition-rxg4e` de Roboflow
Universe (10.125 imagenes, exporta a YOLOv11); ver el README de esta carpeta
para el porque y para las alternativas colombianas.
"""

import argparse


def leer_metadatos(ruta_pt: str = "../modelo/best.pt") -> None:
    """Imprime con que se entreno un .pt. Util para comparar antes/despues."""
    import torch

    datos = torch.load(ruta_pt, map_location="cpu", weights_only=False)
    args = datos.get("train_args") or {}
    print(f"fecha  : {datos.get('date')}")
    print(f"clases : {getattr(datos.get('model'), 'names', None)}")
    for clave in ("model", "data", "epochs", "imgsz", "batch", "degrees", "fliplr", "mosaic"):
        if clave in args:
            print(f"{clave:<8}: {args[clave]}")


def entrenar(data: str, modelo_base: str, epocas: int, imgsz: int, batch: int) -> str:
    from ultralytics import YOLO

    modelo = YOLO(modelo_base)
    resultados = modelo.train(
        data=data,
        epochs=epocas,
        imgsz=imgsz,
        batch=batch,
        name="placas_v2",

        # --- Lo que cambia respecto al entrenamiento original ---

        # Rotacion: la causa directa de que una foto girada no detecte nada.
        # 15 grados cubre fotos tomadas en angulo; para cubrir los 90 grados
        # completos hace falta ademas que el servidor pruebe giros, que ya hace.
        degrees=15.0,

        # Perspectiva y escala: las placas se fotografian de lado y a distancias
        # muy distintas. `scale` alto es lo que ensena a ver placas pequenas.
        perspective=0.0005,
        scale=0.6,
        shear=3.0,

        # Brillo y saturacion: placas al sol, en sombra, de noche.
        hsv_h=0.015,
        hsv_s=0.7,
        hsv_v=0.4,

        # Volteo horizontal NO: una placa espejada no existe y el texto
        # invertido solo confunde al detector.
        fliplr=0.0,
        flipud=0.0,

        # Mosaic ayuda con objetos pequenos, pero conviene apagarlo al final
        # para que las ultimas epocas vean imagenes reales completas.
        mosaic=1.0,
        close_mosaic=10,

        patience=25,   # corta si deja de mejorar; el original tenia 100
        seed=0,        # reproducible
    )
    print(f"\nPesos en: {resultados.save_dir}/weights/best.pt")
    return f"{resultados.save_dir}/weights/best.pt"


def validar(pesos: str, data: str, imgsz: int) -> None:
    """mAP50-95 es la metrica a comparar contra el modelo anterior."""
    from ultralytics import YOLO

    metricas = YOLO(pesos).val(data=data, imgsz=imgsz)
    print(f"mAP50    : {metricas.box.map50:.4f}")
    print(f"mAP50-95 : {metricas.box.map:.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, help="ruta al data.yaml del dataset")
    # yolo11s tiene 9.5M parametros frente a los 3.0M del nano actual: es el salto
    # que mas rinde en objetos pequenos. Medido en la propia t3.micro, inferir a
    # 1280 le cuesta 0.78 s frente a los 0.30 s del modelo actual; si esa latencia
    # molesta, usa --modelo yolo11n.pt, que cuesta lo mismo que el actual y aun asi
    # gana la rotacion y las aumentaciones.
    parser.add_argument("--modelo", default="yolo11s.pt")
    parser.add_argument("--epocas", type=int, default=100)
    # Entrenar a 960 acerca el entrenamiento a como se infiere en el servidor
    # (imgsz=1280), que fue lo que mas subio la deteccion de placas lejanas.
    parser.add_argument("--imgsz", type=int, default=960)
    parser.add_argument("--batch", type=int, default=16)
    args = parser.parse_args()

    pesos = entrenar(args.data, args.modelo, args.epocas, args.imgsz, args.batch)
    validar(pesos, args.data, args.imgsz)
