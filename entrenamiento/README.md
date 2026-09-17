# Reentrenamiento del detector con YOLO11

**Empieza aquí: [`entrenar_yolo11_colab.ipynb`](entrenar_yolo11_colab.ipynb)** — ábrelo en Google
Colab con GPU T4 y ejecuta las celdas en orden. Son ~1-2 horas de entrenamiento.

`entrenar.py` es el mismo entrenamiento como script, para correr desde línea de comandos.

---

## Con qué se entrenó el modelo actual

Leído de los metadatos de `modelo/best.pt`:

```
model=yolov8n.pt   imgsz=640   epochs=50   batch=16
degrees=0.0        fliplr=0.5  mosaic=1.0
data=/content/car_detect-1/data.yaml    (Roboflow, en Colab)
clases={0: 'pl_license_plate'}          3.0M parametros
```

Ni el repositorio del profesor ni el del compañero incluyen ese dataset ni el notebook: solo
el `.pt`. Por eso este entrenamiento parte de un dataset público.

## Por qué reentrenar

Las dos debilidades no son opiniones, salieron de medir el modelo actual contra la app:

| Debilidad | Evidencia | Causa |
|---|---|---|
| Una foto rotada da **cero detecciones** | girando una imagen del banco 90°: 1 caja → **0 cajas** | `degrees=0.0`: nunca vio una placa inclinada |
| Pierde las placas lejanas | en la escena de tráfico detecta **3 de 5** placas visibles | `yolov8n` (3.0M params) entrenado a 640 px |

El servidor ya compensa lo que se puede sin tocar el modelo: aplica la orientación EXIF, infiere
a `imgsz=1280` y reintenta girando la imagen. Subir de ahí sí pide reentrenar.

## El dataset, y por qué ese

| Dataset | Imágenes | Estado | Veredicto |
|---|---|---|---|
| [license-plate-recognition-rxg4e](https://universe.roboflow.com/roboflow-universe-projects/license-plate-recognition-rxg4e/dataset/4) | 10.125 | 13 versiones, export YOLOv11, CC BY 4.0, 722★ / 37.9k descargas | **el elegido** |
| [placas-colombianas](https://universe.roboflow.com/licenseplates-gk27i/placas-colombianas) | 1.770 colombianas | **0 versiones generadas → no descargable por API** | descartado |
| [itm-mprof/placas-colombia](https://universe.roboflow.com/itm-mprof/placas-colombia/dataset/2) | ~100 colombianas | v2 disponible | ajuste fino opcional (sección 7) |
| [reconocimiento_de_placas](https://universe.roboflow.com/reconocimiento-de-placas-vehiculares/reconocimiento_de_placas/dataset/1) | 1.071 | v1, export YOLOv11, clases sin verificar | candidato para la etapa de caracteres |

Buscamos datasets colombianos primero. El más grande (1.770 imágenes) **no tiene ninguna versión
generada**, así que Roboflow no lo sirve por API; los que sí se pueden bajar son de ~100 imágenes,
muy poco para entrenar desde cero.

Y detectar el *rectángulo* de la placa depende poco del país: la forma es la misma. Lo específico
de Colombia —placa amarilla, formato `AAA123`— vive en el OCR y en la corrección de formato del
servidor, no en el detector. De ahí la decisión: entrenar con las 10.000 imágenes y dejar un
ajuste fino colombiano como paso opcional.

## Qué cambia respecto al entrenamiento original

| Parámetro | Antes | Ahora | Por qué |
|---|---|---|---|
| modelo base | `yolov8n` (3.0M) | `yolo11s` (9.5M) | el salto que más rinde en objetos pequeños |
| `degrees` | 0.0 | **15.0** | ataca directo el fallo por rotación |
| `scale` | por defecto | 0.6 | enseña a ver placas a distancias muy distintas |
| `perspective` / `shear` | 0 | 0.0005 / 3.0 | placas fotografiadas de lado |
| `fliplr` | 0.5 | **0.0** | una placa espejada no existe; el texto invertido confunde |
| `close_mosaic` | — | 10 | las últimas épocas ven imágenes reales completas |
| `epochs` | 50 | 100 (`patience=25`) | corta solo si deja de mejorar |

## Por qué YOLO11 y no YOLO12

Dos razones concretas, no preferencia:

1. El servidor corre `ultralytics 8.4.154`. **Verificado**: carga y ejecuta `yolo11s.pt` sin
   problema (9.458.752 parámetros).
2. YOLO12 usa capas de atención que son **más lentas en CPU**, y la inferencia de este proyecto
   corre en CPU (`t3.micro`, sin GPU).

### Coste en CPU, medido en la propia instancia

Mediana de 3 inferencias, con warmup previo:

| modelo | parámetros | 640 | 960 | 1280 |
|---|---|---|---|---|
| `yolov8n` (el actual) | 3.0M | 0.08 s | 0.15 s | 0.30 s |
| `yolo11n` | 2.6M | 0.08 s | 0.16 s | 0.35 s |
| **`yolo11s`** | 9.5M | 0.17 s | 0.42 s | **0.78 s** |

El servidor infiere a 1280, así que `yolo11s` añade ~0.5 s por foto. Dentro de los 2.6-5.3 s de
una petición completa es asumible. **Si la latencia molesta, entrena `yolo11n`**: cuesta lo mismo
que el modelo actual y aun así gana la rotación y las aumentaciones, que es el fallo más grave.

## Cómo saber si quedó mejor

No te fíes solo del `mAP` que imprime el entrenamiento: mídelo contra el banco real.

```bash
# 1. subir el modelo nuevo SIN borrar el que funciona
scp -i llaveVype.pem best.pt ubuntu@34.225.169.137:/home/ubuntu/proyecto/best_v2.pt

# 2. cambiarlo, guardando el anterior
ssh -i llaveVype.pem ubuntu@34.225.169.137 "cd /home/ubuntu/proyecto && \
  cp best.pt best_v1.pt && cp best_v2.pt best.pt && sudo systemctl restart yolo-plates"

# 3. medir con el mismo banco de siempre
python pruebas/probar_api.py --url http://34.225.169.137:8080
```

La referencia a batir es **10 lecturas correctas de 13 placas**. Si el modelo nuevo no llega ahí,
vuelve al anterior (`cp best_v1.pt best.pt`) — el `.pt` viejo queda guardado a propósito.

Y la prueba específica de rotación está en la sección 6 del notebook: las cuatro orientaciones
deben detectar la placa.

> **Lo que reentrenar el detector NO arregla:** que el OCR confunda `M` con `H` en una placa
> borrosa (`SMV098` se lee `SHV098`). Eso es el reconocedor de texto. La vía para eso es entrenar
> un YOLO11 de **caracteres** (36 clases: A-Z y 0-9) sobre recortes de placa y sustituir a
> EasyOCR/PaddleOCR — otro proyecto, apuntado al final del notebook.
