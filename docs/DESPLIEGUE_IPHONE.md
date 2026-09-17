# Despliegue en iPhone con Expo Go

Guia para correr el detector de placas en un **iPhone** usando **Expo Go**, contra la API
de YOLOv8 + EasyOCR desplegada en una instancia EC2.

Lo importante de esta ruta: **no hace falta una Mac, ni Xcode, ni cuenta de Apple Developer
(99 USD/ano)**. Expo Go es una app gratuita de la App Store que ejecuta el codigo JavaScript
de la app; el PC solo sirve el paquete durante el desarrollo.

---

## 1. Arquitectura

```
   iPhone                        PC (Windows)                  AWS EC2 (Ubuntu 24.04)
┌──────────────┐   QR / LAN   ┌────────────────┐            ┌────────────────────────┐
│   Expo Go    │◄────────────►│  npx expo start │            │  systemd: yolo-plates  │
│              │  el paquete  │  (Metro bundler)│            │  uvicorn :8080         │
│  foto (JPEG) │              └────────────────┘            │  FastAPI               │
│      │       │                                            │   ├─ YOLOv8 (best.pt)   │
│      └───────┼──── POST /predict_json/ (HTTP, internet) ──►│   └─ EasyOCR           │
│  placa + voz │◄─── JSON {placas, image, detalles} ─────────┤                        │
└──────────────┘                                            └────────────────────────┘
```

Dos conexiones distintas, y conviene no confundirlas:

| Conexion | Para que | Requisito |
|---|---|---|
| iPhone ↔ PC | Descargar el codigo de la app (solo en desarrollo) | Misma red Wi-Fi, o modo `--tunnel` |
| iPhone ↔ EC2 | Enviar la foto y recibir la placa | Solo internet (datos moviles sirven) |

---

## 2. Requisitos

- iPhone con **Expo Go** instalado ([App Store](https://apps.apple.com/app/expo-go/id982107779)).
- PC con **Node.js 20+** (probado con Node 24.13.1 y npm 11.8.0).
- La API corriendo en la EC2 (ver [`server/`](../server) y la seccion 5).
- **Puerto 8080 abierto** en el Security Group de la instancia (seccion 3).

> **Version del SDK:** Expo Go solo ejecuta apps del SDK mas reciente. Esta app usa
> **Expo SDK 57** (`expo ~57.0.23`). Si en la App Store tienes un Expo Go mas nuevo que el SDK
> del proyecto, la app no cargara: actualiza el proyecto con `npx expo install --fix`.

---

## 3. Abrir el puerto en AWS (se hace una sola vez)

La instancia acepta SSH (22) desde el inicio, pero el 8080 viene cerrado. Sin esta regla el
iPhone recibe *"No se pudo contactar el servidor"* aunque la API este perfecta por dentro.

1. Consola AWS → **EC2** → **Instances** → selecciona tu instancia.
2. Pestana **Security** → clic en el security group (`launch-wizard-1`).
3. **Edit inbound rules** → **Add rule**:
   - Type: `Custom TCP`
   - Port range: `8080`
   - Source: `0.0.0.0/0` *(para la demo; si quieres restringir, usa tu IP publica)*
4. **Save rules**.

Verificacion desde el PC:

```bash
curl http://34.225.169.137:8080/health
```

Debe responder `{"status":"ok","modelo":"best.pt","clases":["pl_license_plate"]}`.

> La IP publica **cambia cada vez que se detiene y arranca la instancia**. Si eso pasa, actualiza
> la IP en la app (pildora de arriba a la derecha) o asocia una Elastic IP.

---

## 4. Correr la app en el iPhone

```bash
cd app-movil
npm install
npx expo start
```

Metro abre una terminal con un **codigo QR**.

### Escanear desde el iPhone

En iOS **no se escanea desde dentro de Expo Go**: se usa la **app Camara** del sistema.

1. Abre la app **Camara** y apunta al QR de la terminal.
2. Toca la notificacion amarilla que aparece arriba → abre Expo Go.
3. Espera a que descargue el paquete (la primera vez tarda mas).
4. Concede el permiso de **camara** cuando iOS lo pida.

### Si el QR no conecta

Redes universitarias o corporativas suelen aislar los dispositivos entre si (client isolation),
y entonces el iPhone no ve al PC. Solucion:

```bash
npx expo start --tunnel
```

El tunel enruta el paquete por internet en vez de por la red local: funciona incluso con el
iPhone en datos moviles. Es mas lento al cargar, pero es la opcion confiable en la universidad.

### Usar la app

1. La IP del servidor viene precargada desde `app.json` (`expo.extra.servidor`).
   Para cambiarla, toca la **pildora `34.225.169.137:8080`** arriba a la derecha.
2. El **punto de color** junto al titulo es el estado del servidor (verde = responde `/health`).
   Tocalo para volver a verificar.
3. Apunta a la placa y presiona el **obturador**.
4. Mientras analiza, la pantalla **congela la foto** que se esta procesando — asi sabes que el
   resultado corresponde a esa toma y no a lo que la camara ve ahora.
5. La placa aparece dibujada como placa colombiana y el iPhone **la deletrea en voz alta**.
   Toca la placa para repetir la voz. **Ver deteccion** muestra la imagen con las cajas de YOLO.

---

## 5. Notas especificas de iOS

| Tema | Detalle |
|---|---|
| **HTTP sin cifrar (ATS)** | iOS bloquea `http://` por App Transport Security. **Expo Go trae ATS desactivado**, asi que la API por HTTP funciona sin tocar nada. Para un build propio (dev build o TestFlight) haria falta `NSAllowsArbitraryLoads`, que ya esta declarado en `app.json` → `ios.infoPlist.NSAppTransportSecurity`. Lo correcto en produccion es poner HTTPS delante de la API. |
| **Permiso de camara** | `NSCameraUsageDescription` en `app.json`. Si se rechaza, iOS no vuelve a preguntar: hay que ir a **Ajustes → Expo Go → Camara**. |
| **Peso de la foto** | El iPhone toma fotos de ~4 MB. La app las captura con `quality: 0.5` (~700 KB) y el servidor las reduce a 1280 px. Sin eso, la subida por datos moviles domina el tiempo total. |
| **Limite de subida** | Starlette corta cada campo de formulario en 1 MB y devuelve `Field exceeded maximum size of 1024KB`: una foto de iPhone en base64 lo pasa de largo. FastAPI llama a `request.form()` sin argumentos, asi que `/predict/` parsea el formulario a mano para subir el limite a 32 MB. Verificado con un payload de 1.87 MB en los cuatro modos de envio. |
| **Voz** | `expo-speech` con `language: 'es-CO'`. Deletrea la placa (`J N U 5 4 0`) porque leerla de corrido suena a palabra inventada. |
| **Timeout** | 45 s para el analisis. La inferencia en CPU tarda 1–6 s, pero una subida lenta puede sumar bastante. |
| **Sin Mac** | Expo Go alcanza para el desarrollo y la demo. Solo se necesita Mac/cuenta de Apple si se quiere distribuir un `.ipa` propio. |

---

## 6. Desplegar la API desde cero

Si hay que reconstruir el servidor en otra instancia:

```bash
# desde el PC, en la raiz del repo
scp -i llave.pem server/app.py server/desplegar.sh server/yolo-plates.service modelo/best.pt ubuntu@IP:/home/ubuntu/
ssh -i llave.pem ubuntu@IP "bash /home/ubuntu/desplegar.sh"
```

El script crea el swap de 4 GB (obligatorio en t3.micro: 1 GB de RAM no alcanza para torch +
EasyOCR), instala torch **CPU** (el paquete normal arrastra 2.5 GB de CUDA inutil aqui), deja la
API como servicio systemd con reinicio automatico y verifica que responda.

Comandos utiles en el servidor:

```bash
sudo systemctl status yolo-plates      # estado
sudo journalctl -u yolo-plates -f      # logs en vivo
sudo systemctl restart yolo-plates     # reiniciar
```

---

## 7. Resultados medidos

`python pruebas/probar_api.py` contra las 8 fotos de `pruebas/` (9 placas en total,
incluye motos y una escena de trafico con varios vehiculos):

| Imagen | Esperado | Detectado | |
|---|---|---|---|
| `JNU540_swift.jpg` | JNU540 | **JNU540** | ok |
| `GLT182_bmw.jpg` | GLT182 | **GLT182** | ok |
| `JXV321_kia.jpg` | JXV321 | **JXV321** | ok |
| `COH262-IJO387-WCT308-FRL260-VCU458_trafico.jpg` | 5 placas | **COH262**, **IJO387**, **FRL260** | 3/5 |
| `SMU002-SMV098_taxis.jpg` | SMU002 / SMV098 | **SMU002**, SHV098 | 1/2 |
| `WUF62C_moto.jpg` | WUF62C | MUF282 | 0/1 |
| `AEH01H_moto.jpg` | AEH01H | **AEH01H** | ok |
| `JNU540_carroprueba.jpg` | JNU540 | **JNU540** | ok |

**9 lecturas correctas de 13 placas visibles**, entre 2 y 6 s por foto.

Antes de los ajustes de deteccion eran 7. Las dos que se ganaron (`IJO387` y `FRL260`) ni
siquiera se detectaban: aparecieron al subir `imgsz`. El denominador tambien cambio, porque la
foto de trafico ahora declara sus 5 placas reales en el nombre y no solo una.

### Los tres ajustes de deteccion que mas valieron

| Ajuste | Por que | Efecto medido |
|---|---|---|
| **Orientacion EXIF** al decodificar | `cv2.imdecode` ignora el tag y el iPhone lo usa casi siempre: el servidor recibia la foto acostada | Una imagen girada 90 grados pasa de **0 cajas** a deteccion normal |
| **`imgsz=1280`** en vez de los 640 por defecto | A 640 una placa de 80 px se encoge a 40 y se pierde | Escena de trafico: **2 -> 3 placas**; confianza de la moto 0.62 -> 0.85; coste 0.2 s |
| **Margen de recorte 4% -> 8%** | Las cajas a 1280 salen mas ajustadas y cortaban el primer caracter (`JNU540` se leia `UNU540`) | 4% -> 7 aciertos, **8% -> 9**, 12% -> 8, 18% -> 8 |

Ademas, **solo se anuncian lecturas con formato de placa colombiana**. Una foto girada producia
cosas como `IHTOHJ` y la app las leia en voz alta como si fueran una matricula; ahora quedan en
`detalles` con `formato_valido: false` y fuera de `placas`.

> **Nota sobre resolucion:** YOLO detecta sobre la imagen reducida a 1280 px (mas alla de eso no
> gana nada y la CPU se dispara), pero el recorte de la placa para el OCR se toma de la imagen
> **original**. En una foto de iPhone (4032 px) una placa lejana perderia dos tercios de su ancho
> si se leyera sobre la version reducida. Las fotos de este banco miden todas menos de 1280 px,
> asi que la tabla de arriba no refleja esa ganancia: se nota en las fotos tomadas con el celular.

Los dos fallos son honestos y vale la pena nombrarlos:

- `SMV098` → `SKV098`: confusion **M/K** del OCR sobre un taxi lejano y borroso. Ninguna regla de
  formato lo arregla, porque ambas son letras en una posicion de letra.
- `WUF62C` → `WUF262`: el emblema del centro de la placa de moto se lee como un `2`, y el OCR
  devuelve `IWUF262C` en una sola caja. De ahi salen dos lecturas con formato valido —`WUF262`
  (carro) y `WUF62C` (moto)— y gana la equivocada.

### Que se probo para subir de 7/9 (y por que no se quedo)

| Intento | Resultado medido |
|---|---|
| Segunda pasada de OCR con `width_ths` bajo + candidatos saltando el emblema | **7 -> 5**. Crea varias lecturas con formato valido y la misma puntuacion; el desempate acaba siendo arbitrario. |
| Realce de nitidez (unsharp) y recorte ampliado a 480 px sobre la placa del taxi | La segunda letra sale `K`, `H` o `V` segun la variante, **nunca `M`**. La informacion no esta en la foto. |
| Decodificador `beamsearch` en vez del voraz | Lecturas identicas al voraz en todo el banco. |
| Bajar el umbral de deteccion de 0.25 a 0.05 | De 2 a 3 cajas en la escena de trafico, y la tercera es un duplicado. Las placas lejanas el detector no las ve. |
| Partir el recorte en dos mitades (letras / numeros) | Acierta la estructura de la moto pero lee `MUF62C`; y rompe `JNU540` y `AEH01H`. |

Todo esto queda anotado en `server/app.py` para que nadie lo reintente sin medir.

### Por que no hay un 100%

No es una cuestion de ajustar un parametro mas:

- **`SMV098`** es un limite de la imagen. En el recorte (79x33 px) esa letra ocupa unos 8 pixeles;
  ninguna variante de preprocesado la recupera. Y no es solo tamano: `COH262` se lee bien con solo
  **58 px** de ancho porque esta nitida, mientras que la del taxi esta movida.
- **`WUF62C`** es una ambiguedad real: el OCR entrega `IWUF262C` en una sola caja, y de ahi salen
  `WUF262` (carro) y `WUF62C` (moto), **las dos con formato colombiano valido**. Sin geometria a
  nivel de caracter no hay forma de decidir cual es.

Subir de ahi no se logra afinando EasyOCR, que es un OCR de proposito general: pide un
reconocedor entrenado especificamente con placas colombianas (un CRNN pequeno sobre recortes de
placa), que es un proyecto en si mismo.

**En la practica** el sistema acierta de forma consistente cuando la placa se ve nitida y ocupa una
parte razonable del encuadre. Para la demo: acercarse lo suficiente y evitar el movimiento importa
mucho mas que cualquier ajuste del servidor.
