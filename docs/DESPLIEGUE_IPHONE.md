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

| Imagen | Esperado | Detectado | Tiempo |
|---|---|---|---|
| `JNU540_swift.jpg` | JNU540 | **JNU540** | 5.0 s |
| `GLT182_bmw.jpg` | GLT182 | **GLT182** | 3.3 s |
| `JXV321_kia.jpg` | JXV321 | **JXV321** | 1.2 s |
| `COH262_trafico.jpg` | COH262 | **COH262** | 5.3 s |
| `SMU002-SMV098_taxis.jpg` | SMU002 / SMV098 | **SMU002**, SKV098 | 4.3 s |
| `WUF62C_moto.jpg` | WUF62C | WUF262 | 1.3 s |
| `AEH01H_moto.jpg` | AEH01H | **AEH01H** | 3.1 s |
| `JNU540_carroprueba.jpg` | JNU540 | **JNU540** | 2.2 s |

**7 de 9 placas leidas exactamente.** Los dos fallos son honestos y vale la pena nombrarlos:

- `SMV098` → `SKV098`: confusion **M/K** del OCR sobre un taxi lejano y borroso. Ninguna regla de
  formato lo arregla, porque ambas son letras en una posicion de letra.
- `WUF62C` → `WUF262`: el emblema del centro de la placa de moto se lee como un `2`, y el OCR
  devuelve `IWUF262C` en una sola caja. De ahi salen dos lecturas con formato valido —`WUF262`
  (carro) y `WUF62C` (moto)— y gana la equivocada.

Se probaron dos arreglos para el caso de la moto (una segunda pasada de OCR con `width_ths` bajo
para separar letras de numeros, y generar candidatos saltando el emblema). Medidos sobre este
mismo banco, **bajaron el acierto de 7 a 5**: crean varios candidatos con formato valido y la misma
puntuacion, y el desempate termina siendo arbitrario. Se descartaron a proposito; queda anotado en
`server/app.py` para que nadie los reintente sin medir.
