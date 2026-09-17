# app-movil — Detector de Placas (Expo)

App de React Native / Expo que fotografia una placa, la envia a la API de YOLOv8 + EasyOCR y
muestra el resultado leyendolo en voz alta.

Pensada para correr en **iPhone con Expo Go**, sin Mac ni cuenta de Apple Developer.
Funciona igual en Android.

## Arranque rapido

```bash
npm install
npx expo start          # o: npx expo start --tunnel  (redes que aislan dispositivos)
```

Escanea el QR con la **app Camara** del iPhone (en iOS el escaner no esta dentro de Expo Go).

Guia completa, incluido el paso de abrir el puerto 8080 en AWS:
**[../docs/DESPLIEGUE_IPHONE.md](../docs/DESPLIEGUE_IPHONE.md)**

## Estructura

```
src/app/_layout.tsx    Stack de expo-router, sin cabecera (la camara va a pantalla completa)
src/app/index.tsx      Pantalla unica: camara, captura, llamada a la API, resultado y voz
app.json               Config de Expo: permisos iOS/Android, ATS y la IP del servidor
```

## Configuracion del servidor

La IP y el puerto por defecto salen de `app.json`:

```json
"extra": { "servidor": { "ip": "34.225.169.137", "puerto": "8080" } }
```

En la app se cambian tocando la pildora de arriba a la derecha, y quedan guardados en el
dispositivo (`AsyncStorage`), asi que no hay que reescribirlos en cada demo.

## Dependencias de Expo Go

Todas las nativas que usa la app vienen incluidas en Expo Go, por eso no hace falta compilar
nada: `expo-camera`, `expo-speech`, `expo-haptics`, `expo-router`, `@react-native-async-storage/async-storage`.

## Comprobaciones

```bash
npx tsc --noEmit                # tipos
npx expo-doctor                 # config del proyecto (21/21)
npx expo export --platform ios  # empaqueta el bundle de iOS
```
