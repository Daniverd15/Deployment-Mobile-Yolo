# Reentrenamiento del detector

Con qué se entrenó el modelo actual (`modelo/best.pt`), leído de sus propios metadatos:

```
model=yolov8n.pt   imgsz=640   epochs=50   batch=16
degrees=0.0        fliplr=0.5  mosaic=1.0
data=/content/car_detect-1/data.yaml    (Roboflow, en Colab)
clases={0: 'pl_license_plate'}          3.0M parametros
```

Ni el repositorio del profesor ni el del compañero incluyen el dataset ni el notebook: solo
el `.pt`. Para reentrenar hay que conseguir `car_detect-1` (pedírselo al profesor o exportarlo
de Roboflow en formato YOLOv8).

## Por qué reentrenar

Las dos debilidades no son opiniones, salieron de medir el modelo actual:

| Debilidad | Evidencia | Qué lo causa |
|---|---|---|
| Una foto rotada da **cero detecciones** | girando una imagen del banco 90°: 1 caja → 0 cajas | `degrees=0.0`: el modelo nunca vio una placa inclinada |
| Pierde las placas lejanas | en la escena de tráfico detecta **3 de 5** placas visibles | `yolov8n` a `imgsz=640` es el modelo más pequeño, entrenado en baja resolución |

El servidor ya compensa lo que se puede sin tocar el modelo: aplica la orientación EXIF, infiere
a `imgsz=1280` y reintenta girando la imagen. Subir de ahí sí pide reentrenar.

## Qué cambia este entrenamiento

| Parámetro | Antes | Ahora | Por qué |
|---|---|---|---|
| modelo base | `yolov8n` (3.0M) | `yolov8s` (11M) | el salto que más rinde para objetos pequeños |
| `imgsz` | 640 | 960 | acerca el entrenamiento a cómo se infiere (1280) |
| `degrees` | 0.0 | **15.0** | ataca directo el fallo por rotación |
| `scale` | por defecto | 0.6 | enseña a ver placas a distancias muy distintas |
| `perspective` / `shear` | 0 | 0.0005 / 3.0 | placas fotografiadas de lado |
| `fliplr` | 0.5 | **0.0** | una placa espejada no existe; el texto invertido confunde |
| `close_mosaic` | — | 10 | las últimas épocas ven imágenes reales completas |
| `epochs` | 50 | 100 (`patience=25`) | corta solo si deja de mejorar |

## Cómo correrlo (Google Colab, GPU gratis)

En CPU no vale la pena: son días. En Colab con T4 son ~1-2 horas.

1. Colab → *Entorno de ejecución* → *Cambiar tipo de entorno* → **T4 GPU**.
2. En una celda:

```python
!pip install -q ultralytics roboflow
# opcion A: exportar el dataset desde Roboflow
from roboflow import Roboflow
rf = Roboflow(api_key="TU_API_KEY")
ds = rf.workspace("...").project("car_detect").version(1).download("yolov8")
# opcion B: subir el zip del dataset y descomprimirlo en /content
```

3. Entrenar:

```python
!python entrenar.py --data /content/car_detect-1/data.yaml
```

4. Descargar `runs/detect/placas_v2/weights/best.pt`.

## Cómo saber si quedó mejor

No te fíes solo del `mAP` que imprime el entrenamiento: mídelo contra el banco real.

```bash
# 1. subir el modelo nuevo al servidor
scp -i llave.pem best.pt ubuntu@IP:/home/ubuntu/proyecto/best_v2.pt
ssh -i llave.pem ubuntu@IP "sudo systemctl stop yolo-plates && \
  cd /home/ubuntu/proyecto && cp best.pt best_v1.pt && cp best_v2.pt best.pt && \
  sudo systemctl start yolo-plates"

# 2. medir con el mismo banco de siempre
python pruebas/probar_api.py --url http://IP:8080
```

La referencia a batir es **9 lecturas correctas de 13 placas**. Si el modelo nuevo no llega ahí,
vuelve al anterior (`cp best_v1.pt best.pt`) — el `.pt` viejo queda guardado a propósito.

> El OCR es un problema aparte: reentrenar el detector no arregla que EasyOCR confunda `M` con
> `K` en una placa borrosa. Eso pide un reconocedor entrenado con placas colombianas, que es
> otro proyecto.
