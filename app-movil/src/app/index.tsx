import AsyncStorage from '@react-native-async-storage/async-storage';
import { CameraView, useCameraPermissions } from 'expo-camera';
import Constants from 'expo-constants';
import * as Haptics from 'expo-haptics';
import * as ImageManipulator from 'expo-image-manipulator';
import * as Speech from 'expo-speech';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Image,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

// Valores por defecto: vienen de app.json -> expo.extra.servidor
const SERVIDOR_POR_DEFECTO = (Constants.expoConfig?.extra as any)?.servidor ?? {
  ip: '',
  puerto: '8080',
};
const CLAVE_ALMACEN = 'detector-placas:servidor';

// Lado largo al que se reduce la foto antes de subirla (ver prepararParaSubir)
const LADO_SUBIDA = 2048;

// La inferencia en la EC2 (CPU, t3.micro) tarda entre 1 y 6 s; se suma la
// subida de la foto desde datos moviles.
const TIMEOUT_ANALISIS_MS = 45000;
// 12 s y no 6: la EC2 es modesta y cuando esta ocupada tarda en contestar el
// /health. Con 6 s la app marcaba "Sin conexion" con el servidor perfectamente
// vivo, que asusta en mitad de una demo.
const TIMEOUT_SALUD_MS = 12000;

type Estado = 'verificando' | 'conectado' | 'sin_conexion';

type Respuesta = {
  success?: boolean;
  placas?: string[];
  num_placas?: number;
  image?: string | null;
  message?: string;
  error?: string;
};

/** fetch con corte por tiempo: sin esto la app se queda colgada si la EC2 no responde. */
async function fetchConTimeout(url: string, opciones: RequestInit, ms: number) {
  const control = new AbortController();
  const temporizador = setTimeout(() => control.abort(), ms);
  try {
    return await fetch(url, { ...opciones, signal: control.signal });
  } finally {
    clearTimeout(temporizador);
  }
}

/** "JNU540" -> "J N U 5 4 0" para que la voz deletree en vez de leer una palabra rara. */
function deletrear(placa: string) {
  return placa.split('').join(' ');
}

/** Reduce la foto en el telefono antes de subirla, y la devuelve en base64.
 *
 * El iPhone toma fotos de 4032 px, pero el servidor detecta sobre 1280: subir
 * la foto entera es gastar red a cambio de nada. Medido contra el banco de
 * pruebas, con el lado largo a 2048 px se aciertan las mismas placas que con
 * 4032 (7 de 8) pero el envio pasa de ~1.19 MB a ~466 KB, o sea 2.6 veces menos.
 * Bajar mas si cuesta: a 1600 y a 1280 se pierde una placa.
 *
 * La compresion se aplica una sola vez, aqui, con la imagen ya reducida: la
 * compresion JPEG dana el texto pequeno de las placas lejanas, asi que conviene
 * hacerla lo mas tarde posible y una sola vez.
 */
async function prepararParaSubir(uri: string, ancho?: number, alto?: number): Promise<string> {
  // El lado largo es el que se acota, sin importar si la foto salio vertical
  const esHorizontal = (ancho ?? 0) >= (alto ?? 0);
  const ladoLargo = Math.max(ancho ?? 0, alto ?? 0);
  const acciones =
    ladoLargo > LADO_SUBIDA
      ? [{ resize: esHorizontal ? { width: LADO_SUBIDA } : { height: LADO_SUBIDA } }]
      : []; // ya es pequena: no tiene sentido reescalarla hacia arriba

  const resultado = await ImageManipulator.manipulateAsync(uri, acciones, {
    compress: 0.8,
    format: ImageManipulator.SaveFormat.JPEG,
    base64: true,
  });

  if (!resultado.base64) throw new Error('No se pudo preparar la imagen para enviar.');
  return resultado.base64;
}

export default function PantallaDetector() {
  const camara = useRef<CameraView>(null);
  const insets = useSafeAreaInsets();
  const [permiso, pedirPermiso] = useCameraPermissions();

  const [ip, setIp] = useState<string>(SERVIDOR_POR_DEFECTO.ip);
  const [puerto, setPuerto] = useState<string>(SERVIDOR_POR_DEFECTO.puerto);
  const [ajustesVisibles, setAjustesVisibles] = useState(false);
  const [ipBorrador, setIpBorrador] = useState(ip);
  const [puertoBorrador, setPuertoBorrador] = useState(puerto);

  const [estado, setEstado] = useState<Estado>('verificando');
  const [analizando, setAnalizando] = useState(false);
  const [fotoCongelada, setFotoCongelada] = useState<string | null>(null);
  const [imagenProcesada, setImagenProcesada] = useState<string | null>(null);
  const [placas, setPlacas] = useState<string[]>([]);
  const [mensaje, setMensaje] = useState<string | null>(null);
  const [verAnalizada, setVerAnalizada] = useState(false);

  const urlBase = ip ? `http://${ip}:${puerto || '8080'}` : '';

  // --- Persistencia de la IP: no tener que reescribirla en cada demo ---
  useEffect(() => {
    AsyncStorage.getItem(CLAVE_ALMACEN)
      .then((guardado) => {
        if (!guardado) return;
        const datos = JSON.parse(guardado);
        if (datos.ip) setIp(datos.ip);
        if (datos.puerto) setPuerto(datos.puerto);
      })
      .catch(() => {
        /* si el almacenamiento falla se usan los valores de app.json */
      });
  }, []);

  useEffect(() => {
    if (!permiso?.granted && permiso?.canAskAgain) pedirPermiso();
  }, [permiso, pedirPermiso]);

  // --- Semaforo de conexion: se comprueba antes de gastar una foto ---
  const verificarServidor = useCallback(async () => {
    if (!ip) {
      setEstado('sin_conexion');
      return;
    }
    setEstado('verificando');
    try {
      const respuesta = await fetchConTimeout(`${urlBase}/health`, { method: 'GET' }, TIMEOUT_SALUD_MS);
      setEstado(respuesta.ok ? 'conectado' : 'sin_conexion');
    } catch {
      setEstado('sin_conexion');
    }
  }, [ip, urlBase]);

  useEffect(() => {
    verificarServidor();
  }, [verificarServidor]);

  const guardarAjustes = async () => {
    const nuevaIp = ipBorrador.trim();
    const nuevoPuerto = puertoBorrador.trim() || '8080';
    setIp(nuevaIp);
    setPuerto(nuevoPuerto);
    setAjustesVisibles(false);
    try {
      await AsyncStorage.setItem(CLAVE_ALMACEN, JSON.stringify({ ip: nuevaIp, puerto: nuevoPuerto }));
    } catch {
      /* que no se guarde no impide usar la app en esta sesion */
    }
  };

  const capturar = async () => {
    if (!camara.current || analizando) return;
    if (!ip) {
      setMensaje('Falta la IP del servidor: tocala arriba para configurarla.');
      setAjustesVisibles(true);
      return;
    }

    setAnalizando(true);
    setPlacas([]);
    setImagenProcesada(null);
    setMensaje(null);
    setVerAnalizada(false);

    try {
      // Se captura sin comprimir y sin base64: la imagen que se sube se prepara
      // abajo en un solo paso, para no comprimir dos veces.
      const foto = await camara.current.takePictureAsync({ quality: 1 });
      if (!foto?.uri) throw new Error('La camara no devolvio la imagen.');
      setFotoCongelada(foto.uri);

      const base64 = await prepararParaSubir(foto.uri, foto.width, foto.height);

      const respuesta = await fetchConTimeout(
        `${urlBase}/predict_json/`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ image_base64: base64 }),
        },
        TIMEOUT_ANALISIS_MS,
      );

      if (!respuesta.ok) {
        setEstado('sin_conexion');
        setMensaje(`El servidor respondio HTTP ${respuesta.status}.`);
        return;
      }

      setEstado('conectado');
      const datos: Respuesta = await respuesta.json();

      if (datos.error) {
        setMensaje(datos.error);
        return;
      }

      const detectadas = datos.placas ?? [];
      setPlacas(detectadas);
      if (datos.image) setImagenProcesada(`data:image/jpeg;base64,${datos.image}`);

      if (detectadas.length > 0) {
        Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
        const texto =
          detectadas.length === 1
            ? `Placa ${deletrear(detectadas[0])}`
            : `${detectadas.length} placas: ${detectadas.map(deletrear).join(', ')}`;
        Speech.speak(texto, { language: 'es-CO', rate: 0.9 });
      } else {
        Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning);
        setMensaje(datos.message ?? 'No se detectaron placas.');
        Speech.speak('No se detectaron placas', { language: 'es-CO' });
      }
    } catch (error: any) {
      const abortada = error?.name === 'AbortError';
      setEstado('sin_conexion');
      setMensaje(
        abortada
          ? 'El servidor tardo demasiado en responder. Revisa la conexion y vuelve a intentar.'
          : `No se pudo contactar el servidor en ${urlBase}. Revisa la IP y que el puerto este abierto.`,
      );
    } finally {
      setAnalizando(false);
    }
  };

  const reiniciar = () => {
    Speech.stop();
    setFotoCongelada(null);
    setImagenProcesada(null);
    setPlacas([]);
    setMensaje(null);
    setVerAnalizada(false);
  };

  // --- Permisos ---
  if (!permiso) {
    return (
      <View style={estilos.centrado}>
        <ActivityIndicator color="#FACC15" />
      </View>
    );
  }

  if (!permiso.granted) {
    return (
      <View style={estilos.centrado}>
        <Text style={estilos.tituloPermiso}>Se necesita la camara</Text>
        <Text style={estilos.textoPermiso}>
          La app fotografia la placa y envia la imagen al servidor de deteccion. Si ya rechazaste el
          permiso, habilitalo en Ajustes {'>'} Expo Go {'>'} Camara.
        </Text>
        <Pressable style={estilos.botonPrimario} onPress={pedirPermiso}>
          <Text style={estilos.textoBotonPrimario}>Conceder permiso</Text>
        </Pressable>
      </View>
    );
  }

  const colorEstado =
    estado === 'conectado' ? '#22C55E' : estado === 'verificando' ? '#FACC15' : '#EF4444';
  const etiquetaEstado =
    estado === 'conectado' ? 'Servidor en linea' : estado === 'verificando' ? 'Verificando...' : 'Sin conexion';

  return (
    <View style={estilos.contenedor}>
      {/* Visor: camara en vivo, o la foto congelada mientras se analiza / hay resultado */}
      {fotoCongelada ? (
        <Image source={{ uri: fotoCongelada }} style={estilos.visor} resizeMode="cover" />
      ) : (
        <CameraView ref={camara} style={estilos.visor} facing="back" />
      )}

      {/* Cabecera */}
      <View style={[estilos.cabecera, { paddingTop: insets.top + 8 }]}>
        <View>
          <Text style={estilos.titulo}>Detector de Placas</Text>
          <Pressable style={estilos.filaEstado} onPress={verificarServidor}>
            <View style={[estilos.punto, { backgroundColor: colorEstado }]} />
            <Text style={estilos.textoEstado}>{etiquetaEstado}</Text>
          </Pressable>
        </View>
        <Pressable
          style={estilos.pildora}
          onPress={() => {
            setIpBorrador(ip);
            setPuertoBorrador(puerto);
            setAjustesVisibles(true);
          }}>
          <Text style={estilos.textoPildora}>{ip ? `${ip}:${puerto}` : 'Configurar IP'}</Text>
        </Pressable>
      </View>

      {/* Capa de analisis sobre la foto congelada */}
      {analizando && (
        <View style={estilos.capaAnalisis}>
          <ActivityIndicator size="large" color="#FACC15" />
          <Text style={estilos.textoAnalisis}>Analizando esta foto...</Text>
        </View>
      )}

      {/* Panel inferior */}
      <View style={[estilos.panel, { paddingBottom: insets.bottom + 16 }]}>
        <ScrollView
          contentContainerStyle={estilos.panelContenido}
          showsVerticalScrollIndicator={false}>
          {placas.length > 0 && (
            <View style={estilos.grupoPlacas}>
              {placas.map((placa) => (
                <Pressable
                  key={placa}
                  style={estilos.placa}
                  onPress={() => Speech.speak(`Placa ${deletrear(placa)}`, { language: 'es-CO', rate: 0.9 })}>
                  <Text style={estilos.textoPlaca}>{placa}</Text>
                  <Text style={estilos.subtextoPlaca}>COLOMBIA</Text>
                </Pressable>
              ))}
            </View>
          )}

          {mensaje && <Text style={estilos.mensaje}>{mensaje}</Text>}

          {verAnalizada && imagenProcesada && (
            <Image source={{ uri: imagenProcesada }} style={estilos.imagenAnalizada} resizeMode="contain" />
          )}

          <View style={estilos.filaAcciones}>
            {fotoCongelada ? (
              <>
                <Pressable style={estilos.botonSecundario} onPress={reiniciar} disabled={analizando}>
                  <Text style={estilos.textoBotonSecundario}>Nueva foto</Text>
                </Pressable>
                {imagenProcesada && (
                  <Pressable
                    style={estilos.botonSecundario}
                    onPress={() => setVerAnalizada((v) => !v)}>
                    <Text style={estilos.textoBotonSecundario}>
                      {verAnalizada ? 'Ocultar deteccion' : 'Ver deteccion'}
                    </Text>
                  </Pressable>
                )}
              </>
            ) : (
              <Pressable
                style={[estilos.obturador, analizando && estilos.obturadorInactivo]}
                onPress={capturar}
                disabled={analizando}>
                <View style={estilos.obturadorInterior} />
              </Pressable>
            )}
          </View>
        </ScrollView>
      </View>

      {/* Ajustes de servidor */}
      <Modal visible={ajustesVisibles} transparent animationType="slide" onRequestClose={() => setAjustesVisibles(false)}>
        <View style={estilos.fondoModal}>
          <View style={estilos.tarjetaModal}>
            <Text style={estilos.tituloModal}>Servidor</Text>
            <Text style={estilos.etiqueta}>IP publica de la EC2</Text>
            <TextInput
              style={estilos.entrada}
              value={ipBorrador}
              onChangeText={setIpBorrador}
              placeholder="34.225.169.137"
              placeholderTextColor="#64748B"
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType={Platform.OS === 'ios' ? 'numbers-and-punctuation' : 'default'}
            />
            <Text style={estilos.etiqueta}>Puerto</Text>
            <TextInput
              style={estilos.entrada}
              value={puertoBorrador}
              onChangeText={setPuertoBorrador}
              placeholder="8080"
              placeholderTextColor="#64748B"
              keyboardType="number-pad"
            />
            <View style={estilos.filaModal}>
              <Pressable style={estilos.botonSecundario} onPress={() => setAjustesVisibles(false)}>
                <Text style={estilos.textoBotonSecundario}>Cancelar</Text>
              </Pressable>
              <Pressable style={estilos.botonPrimario} onPress={guardarAjustes}>
                <Text style={estilos.textoBotonPrimario}>Guardar</Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>
    </View>
  );
}

const estilos = StyleSheet.create({
  contenedor: { flex: 1, backgroundColor: '#0B1220' },
  // absoluteFillObject salio de los tipos en React Native 0.86: se escribe explicito
  visor: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 },
  centrado: {
    flex: 1,
    backgroundColor: '#0B1220',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
    gap: 12,
  },
  tituloPermiso: { color: '#F8FAFC', fontSize: 20, fontWeight: '700' },
  textoPermiso: { color: '#94A3B8', textAlign: 'center', lineHeight: 20 },

  cabecera: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingBottom: 12,
    backgroundColor: 'rgba(11,18,32,0.75)',
  },
  titulo: { color: '#F8FAFC', fontSize: 18, fontWeight: '700' },
  filaEstado: { flexDirection: 'row', alignItems: 'center', gap: 6, marginTop: 4 },
  punto: { width: 8, height: 8, borderRadius: 4 },
  textoEstado: { color: '#94A3B8', fontSize: 12 },
  pildora: {
    backgroundColor: 'rgba(148,163,184,0.2)',
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: 999,
  },
  textoPildora: { color: '#E2E8F0', fontSize: 12, fontWeight: '600' },

  capaAnalisis: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: 'rgba(11,18,32,0.55)',
    gap: 12,
  },
  textoAnalisis: { color: '#F8FAFC', fontSize: 15, fontWeight: '600' },

  panel: {
    marginTop: 'auto',
    backgroundColor: 'rgba(11,18,32,0.9)',
    borderTopLeftRadius: 24,
    borderTopRightRadius: 24,
    maxHeight: '55%',
  },
  panelContenido: { padding: 16, gap: 14, alignItems: 'center' },

  grupoPlacas: { gap: 10, alignItems: 'center' },
  placa: {
    backgroundColor: '#FACC15',
    borderRadius: 10,
    borderWidth: 3,
    borderColor: '#111827',
    paddingHorizontal: 22,
    paddingVertical: 8,
    alignItems: 'center',
  },
  textoPlaca: { fontSize: 34, fontWeight: '800', color: '#111827', letterSpacing: 3 },
  subtextoPlaca: { fontSize: 10, fontWeight: '700', color: '#111827', letterSpacing: 2 },

  mensaje: { color: '#FCA5A5', textAlign: 'center', fontSize: 14, lineHeight: 20 },
  imagenAnalizada: { width: '100%', height: 200, borderRadius: 12, backgroundColor: '#020617' },

  filaAcciones: { flexDirection: 'row', gap: 12, alignItems: 'center', justifyContent: 'center' },
  obturador: {
    width: 76,
    height: 76,
    borderRadius: 38,
    borderWidth: 4,
    borderColor: '#F8FAFC',
    alignItems: 'center',
    justifyContent: 'center',
  },
  obturadorInactivo: { opacity: 0.4 },
  obturadorInterior: { width: 58, height: 58, borderRadius: 29, backgroundColor: '#FACC15' },

  botonPrimario: { backgroundColor: '#FACC15', paddingHorizontal: 20, paddingVertical: 12, borderRadius: 12 },
  textoBotonPrimario: { color: '#111827', fontWeight: '700' },
  botonSecundario: {
    backgroundColor: 'rgba(148,163,184,0.2)',
    paddingHorizontal: 20,
    paddingVertical: 12,
    borderRadius: 12,
  },
  textoBotonSecundario: { color: '#E2E8F0', fontWeight: '600' },

  fondoModal: { flex: 1, backgroundColor: 'rgba(2,6,23,0.7)', justifyContent: 'flex-end' },
  tarjetaModal: {
    backgroundColor: '#0F172A',
    padding: 20,
    borderTopLeftRadius: 24,
    borderTopRightRadius: 24,
    gap: 8,
  },
  tituloModal: { color: '#F8FAFC', fontSize: 18, fontWeight: '700', marginBottom: 4 },
  etiqueta: { color: '#94A3B8', fontSize: 12, marginTop: 8 },
  entrada: {
    backgroundColor: '#1E293B',
    borderRadius: 10,
    paddingHorizontal: 14,
    paddingVertical: 12,
    color: '#F8FAFC',
    fontSize: 16,
  },
  filaModal: { flexDirection: 'row', gap: 12, justifyContent: 'flex-end', marginTop: 16 },
});
