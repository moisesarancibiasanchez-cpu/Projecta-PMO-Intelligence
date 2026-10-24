/**
 * Motor de captura de Projecta Capture.
 *
 * Este fichero implementa la captura en el navegador con una regla que
 * gobierna todas las demás: **nada se pide sin una acción explícita**.
 *
 * Al cargar la página no se pide el micrófono ni la pantalla. Ni una
 * sola llamada a `getUserMedia` o `getDisplayMedia` ocurre sin que la
 * persona pulse un botón. Y no es un detalle de estilo: pedir un
 * permiso de micrófono al abrir una página es la forma más rápida de
 * que la gente deje de aceptar permisos de micrófono en todas partes.
 *
 * El motor distingue tres cosas que suelen confundirse:
 *
 * - **El modo solicitado**: el que la persona pidió.
 * - **El modo efectivo**: el que se consiguió. Puede ser distinto, y
 *   cuando lo es, la captura **no** es completa.
 * - **La señal real**: lo que el navegador ha dado de verdad. Se
 *   comprueba que las pistas existen y que tienen audio; no se
 *   assume que haber pedido una pantalla compartida trae su audio.
 *
 * La captura se graba **por fragmentos** y cada fragmento se sube con
 * su número de secuencia. El servidor deduplica por índice, así que
 * reenviar un fragmento no rompe nada, y la grabación se puede
 * reconstruir aunque la red falle a mitad.
 *
 * Todo el texto de la interfaz está en español, porque es la lengua en
 * la que se usa el producto.
 */

/* eslint-env browser */
'use strict';

/** Los modos de captura del MVP. */
const MODOS = Object.freeze({
  MIC_ONLY: 'MIC_ONLY',
  SHARED_AUDIO_ONLY: 'SHARED_AUDIO_ONLY',
  MIC_AND_SHARED_AUDIO: 'MIC_AND_SHARED_AUDIO',
});

/** Los estados de la captura. Coinciden con los del servidor. */
const ESTADOS = Object.freeze({
  DRAFT: 'DRAFT',
  READY: 'READY',
  RECORDING: 'RECORDING',
  PAUSED: 'PAUSED',
  STOPPED: 'STOPPED',
  CANCELLED: 'CANCELLED',
  FAILED: 'FAILED',
});

/** Códecs de audio que se prueban, en orden de preferencia. */
const FORMATOS_A_PROBAR = Object.freeze([
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/ogg;codecs=opus',
]);

/** Duración de cada fragmento de grabación, en milisegundos. */
const DURACION_FRAGMENTO_MS = 5000;

/** Segundos que se espera antes de reintentar una subida. */
const ESPERA_REINTENTO_MS = 3000;

/** Número máximo de reintentos por fragmento. */
const MAXIMO_REINTENTOS = 4;

/**
 * Estado completo del motor de captura.
 *
 * Se mantiene en un único objeto para que no haya estado repartido por
 * el código, que es donde aparecen los errores de «la pista se paró
 * pero el botón sigue diciendo que está grabando».
 */
const estado = {
  sesionId: null,
  artefactoId: null,
  modoSolicitado: MODOS.MIC_ONLY,
  modoEfectivo: null,
  estadoCaptura: ESTADOS.DRAFT,

  pistaMicrofono: null,
  pistaRemota: null,
  flujoMicrofono: null,
  flujoRemoto: null,
  grabadorMicrofono: null,
  grabadorRemoto: null,

  contextoAudio: null,
  analizadorMicrofono: null,
  analizadorRemoto: null,
  dibujoMicrofono: null,
  dibujoRemoto: null,

  fragmentos: [],
  indiceSiguiente: 0,
  subiendo: false,
  desconexion: false,
  iniciadoEn: null,
  duracionAcumuladaMs: 0,
  cronometro: null,
  eventoFinalizado: null,
};

/* ─────────────────────────── utilidades ─────────────────────────── */

/** Convierte milisegundos en `mm:ss`. */
function formatearMmss(milisegundos) {
  const total = Math.floor(milisegundos / 1000);
  const minutos = Math.floor(total / 60);
  const segundos = total % 60;
  return `${String(minutos).padStart(2, '0')}:${String(segundos).padStart(2, '0')}`;
}

/** Escribe un mensaje en el registro visible de la interfaz. */
function registrar(mensaje, nivel) {
  const registro = document.getElementById('registro');
  if (!registro) {
    return;
  }
  const linea = document.createElement('li');
  linea.className = `linea-${nivel || 'info'}`;
  linea.textContent = `${formatearMmss(estado.duracionAcumuladaMs)} · ${mensaje}`;
  registro.appendChild(linea);
  registro.scrollTop = registro.scrollHeight;
}

/** Actualiza el indicador de estado visible y persistente. */
function refrescarEstado() {
  const indicador = document.getElementById('indicador-estado');
  if (indicador) {
    const activo = estado.estadoCaptura === ESTADOS.RECORDING;
    indicador.classList.toggle('activo', activo);
    indicador.classList.toggle('pausado', estado.estadoCaptura === ESTADOS.PAUSED);
    const textos = {
      DRAFT: 'Sin iniciar',
      READY: 'Listo para grabar',
      RECORDING: 'GRABANDO',
      PAUSED: 'EN PAUSA',
      STOPPED: 'Detenida',
      CANCELLED: 'Cancelada',
      FAILED: 'Error',
    };
    indicador.textContent = textos[estado.estadoCaptura] || estado.estadoCaptura;
    indicador.setAttribute('aria-live', 'polite');
  }

  const duracion = document.getElementById('duracion');
  if (duracion) {
    duracion.textContent = formatearMmss(estado.duracionAcumuladaMs);
  }

  const conexion = document.getElementById('estado-conexion');
  if (conexion) {
    conexion.textContent = estado.desconexion ? 'Sin conexión' : 'Conectado';
    conexion.classList.toggle('sin-conexion', estado.desconexion);
  }

  const botonIniciar = document.getElementById('boton-iniciar');
  const botonPausar = document.getElementById('boton-pausar');
  const botonReanudar = document.getElementById('boton-reanudar');
  const botonDetener = document.getElementById('boton-detener');
  const botonCancelar = document.getElementById('boton-cancelar');

  if (botonIniciar) botonIniciar.disabled = estado.estadoCaptura === ESTADOS.RECORDING;
  if (botonPausar) botonPausar.disabled = estado.estadoCaptura !== ESTADOS.RECORDING;
  if (botonReanudar) botonReanudar.disabled = estado.estadoCaptura !== ESTADOS.PAUSED;
  if (botonDetener) botonDetener.disabled = !['RECORDING', 'PAUSED'].includes(estado.estadoCaptura);
  if (botonCancelar) botonCancelar.disabled = !['DRAFT', 'READY'].includes(estado.estadoCaptura);
}

/** Pide confirmación al usuario antes de una acción con consecuencias. */
function confirmar(mensaje) {
  return window.confirm(mensaje);
}

/* ─────────────────────────── permisos ─────────────────────────── */

/**
 * Enumera los dispositivos de entrada de audio.
 *
 * Se llama **después** de que la persona pulse el botón, nunca al
 * cargar la página: enumerar dispositivos pide permiso en algunos
 * navegadores.
 */
async function enumerarDispositivos() {
  try {
    const dispositivos = await navigator.mediaDevices.enumerateDevices();
    const microfonos = dispositivos.filter((d) => d.kind === 'audioinput');
    const selector = document.getElementById('selector-microfono');
    if (selector) {
      selector.innerHTML = '';
      microfonos.forEach((dispositivo, indice) => {
        const opcion = document.createElement('option');
        opcion.value = dispositivo.deviceId;
        opcion.textContent = dispositivo.label || `Micrófono ${indice + 1}`;
        selector.appendChild(opcion);
      });
      if (microfonos.length === 0) {
        const opcion = document.createElement('option');
        opcion.value = '';
        opcion.textContent = 'No se ha detectado ningún micrófono';
        selector.appendChild(opcion);
      }
    }
    registrar(
      microfonos.length > 0
        ? `Se han detectado ${microfonos.length} micrófono(s).`
        : 'No se ha detectado ningún micrófono.',
      microfonos.length > 0 ? 'info' : 'aviso',
    );
    return microfonos;
  } catch (error) {
    registrar(`No se pudieron enumerar los dispositivos: ${error.message}`, 'error');
    return [];
  }
}

/**
 * Pide el micrófono y comprueba que trae audio de verdad.
 *
 * La comprobación de la pista no es un adorno: hay navegadores que
 * devuelven una pista de audio **sin ninguna pista de audio** cuando
 * el permiso se deniega o no hay dispositivo. Grabar eso produce un
 * archivo de silencio que parece una sesión válida.
 */
async function pedirMicrofono(deviceId) {
  const restricciones = {
    audio: deviceId ? { deviceId: { exact: deviceId } } : true,
    video: false,
  };
  try {
    const flujo = await navigator.mediaDevices.getUserMedia(restricciones);
    const pistas = flujo.getAudioTracks();
    if (pistas.length === 0) {
      flujo.getTracks().forEach((p) => p.stop());
      throw new Error('El navegador no ha entregado ninguna pista de audio.');
    }
    const pista = pistas[0];
    if (pista.readyState !== 'live') {
      pista.stop();
      throw new Error('La pista de audio no está activa.');
    }
    estado.flujoMicrofono = flujo;
    estado.pistaMicrofono = pista;
    registrar('Micrófono concedido.', 'info');
    return true;
  } catch (error) {
    registrar(`No se pudo obtener el micrófono: ${error.message}`, 'error');
    return false;
  }
}

/**
 * Pide una superficie compartida y comprueba si trae audio.
 *
 * **Compartir una ventana no garantiza capturar el audio de Teams.**
 * Depende del navegador, del sistema operativo y de cómo se comparta.
 * Por eso se comprueba la pista y, si no hay audio, se avisa con
 * claridad en vez de fingir que se ha conseguido.
 */
async function pedirAudioRemoto() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
    registrar('Este navegador no permite compartir pantalla con audio.', 'aviso');
    return false;
  }
  try {
    // Se pide una **pestaña**, no la pantalla completa. En Chromium, el
    // audio de una pestaña compartida funciona en todas las plataformas,
    // mientras que el audio del sistema solo está implementado en
    // algunas, y compartir la pantalla completa no lo ofrece. La pista
    // de vídeo es obligatoria: `getDisplayMedia` rechaza la promesa si
    // se pide `video: false`. Por eso se pide y se detiene de inmediato.
    const opciones = {
      audio: true,
      video: { displaySurface: 'browser' },
      systemAudio: 'include',
      selfBrowserSurface: 'exclude',
    };
    const flujo = await navigator.mediaDevices.getDisplayMedia(opciones);
    const pistasAudio = flujo.getAudioTracks();
    const pistasVideo = flujo.getVideoTracks();

    // La pista de vídeo se pide porque algunos navegadores solo
    // habilitan el audio si se comparte con vídeo. Se **detiene de
    // inmediato** y no se graba ni se almacena: no se persiste ni se
    // transmite contenido visual de la pantalla.
    pistasVideo.forEach((p) => p.stop());

    if (pistasAudio.length === 0) {
      flujo.getTracks().forEach((p) => p.stop());
      registrar(
        'La superficie compartida se ha concedido pero NO trae audio. '
          + 'Comparte una pestaña de la aplicación de reuniones, no la '
          + 'pantalla completa, y comprueba que el botón de compartir audio '
          + 'esté activado. Compartir una pantalla completa no ofrece audio '
          + 'de sistema en Chrome, y compartir el audio del sistema solo '
          + 'está implementado en algunas plataformas.',
        'aviso',
      );
      return false;
    }
    estado.flujoRemoto = flujo;
    estado.pistaRemota = pistasAudio[0];
    registrar('Audio remoto compartido correctamente.', 'info');
    return true;
  } catch (error) {
    registrar(`No se pudo compartir la pantalla: ${error.message}`, 'aviso');
    return false;
  }
}

/* ─────────────────────────── niveles ─────────────────────────── */

/**
 * Monta los medidores de nivel, uno por fuente.
 *
 * Son **dos medidores separados**, y no uno mezclado: si solo hubiera
 * uno, no se podría distinguir «no entra nada por el micrófono» de
 * «no entra nada del audio remoto», que son fallos distintos y con
 * causas distintas.
 */
function montarMedidores() {
  const Contexto = window.AudioContext || window.webkitAudioContext;
  if (!Contexto) {
    registrar('Este navegador no tiene Web Audio API: no hay medidores de nivel.', 'aviso');
    return;
  }
  try {
    estado.contextoAudio = new Contexto();
    const crear = (flujo, canvasId) => {
      if (!flujo) {
        return null;
      }
      const fuente = estado.contextoAudio.createMediaStreamSource(flujo);
      const analizador = estado.contextoAudio.createAnalyser();
      analizador.fftSize = 1024;
      fuente.connect(analizador);
      const lienzo = document.getElementById(canvasId);
      return { analizador, lienzo };
    };
    const micro = crear(estado.flujoMicrofono, 'medidor-microfono');
    const remoto = crear(estado.flujoRemoto, 'medidor-remoto');
    estado.analizadorMicrofono = micro ? micro.analizador : null;
    estado.analizadorRemoto = remoto ? remoto.analizador : null;
    estado.dibujoMicrofono = micro ? micro.lienzo : null;
    estado.dibujoRemoto = remoto ? remoto.lienzo : null;
  } catch (error) {
    registrar(`No se pudieron montar los medidores: ${error.message}`, 'aviso');
  }
}

/** Dibuja el nivel de una fuente en su lienzo. */
function dibujarNivel(analizador, lienzo) {
  if (!analizador || !lienzo) {
    return;
  }
  const contexto = lienzo.getContext('2d');
  const datos = new Uint8Array(analizador.fftSize);
  analizador.getByteTimeDomainData(datos);

  let suma = 0;
  for (let i = 0; i < datos.length; i += 1) {
    const valor = (datos[i] - 128) / 128;
    suma += valor * valor;
  }
  const rms = Math.sqrt(suma / datos.length);

  const ancho = lienzo.width;
  const alto = lienzo.height;
  contexto.clearRect(0, 0, ancho, alto);
  const nivel = Math.min(1, rms * 4);
  contexto.fillStyle = nivel > 0.02 ? '#2f9e44' : '#868e96';
  const alturaBarra = Math.max(2, nivel * alto);
  contexto.fillRect(0, alto - alturaBarra, ancho, alturaBarra);
}

/** El bucle que refresca los dos medidores. */
function bucleDeNiveles() {
  dibujarNivel(estado.analizadorMicrofono, estado.dibujoMicrofono);
  dibujarNivel(estado.analizadorRemoto, estado.dibujoRemoto);
  window.requestAnimationFrame(bucleDeNiveles);
}

/* ─────────────────────────── grabación ─────────────────────────── */

/** Elige el primer formato de audio que el navegador soporta. */
function elegirFormato() {
  if (typeof MediaRecorder === 'undefined') {
    return null;
  }
  const soportado = FORMATOS_A_PROBAR.find((formato) => {
    try {
      return MediaRecorder.isTypeSupported(formato);
    } catch (error) {
      return false;
    }
  });
  return soportado || '';
}

/**
 * Empieza a grabar una fuente.
 *
 * Cada `MediaRecorder` emite un fragmento cada `DURACION_FRAGMENTO_MS`.
 * Los fragmentos se numeran y se suben por separado.
 */
function iniciarGrabador(flujo, origen) {
  if (!flujo) {
    return null;
  }
  const formato = elegirFormato();
  if (!formato) {
    registrar('Este navegador no admite ningún formato de audio compatible.', 'error');
    return null;
  }
  let grabador;
  try {
    grabador = new MediaRecorder(flujo, formato ? { mimeType: formato } : undefined);
  } catch (error) {
    registrar(`No se pudo crear el grabador: ${error.message}`, 'error');
    return null;
  }

  grabador.addEventListener('dataavailable', (evento) => {
    if (!evento.data || evento.data.size === 0) {
      return;
    }
    const indice = estado.indiceSiguiente;
    estado.indiceSiguiente += 1;
    const fragmento = {
      indice,
      blob: evento.data,
      enviado: false,
      intentos: 0,
      origen,
    };
    estado.fragmentos.push(fragmento);
    subirFragmento(fragmento);
  });

  grabador.addEventListener('error', (evento) => {
    registrar(
      `Error de grabación en la fuente ${origen}: ${evento.error || 'desconocido'}`,
      'error',
    );
  });

  grabador.start(DURACION_FRAGMENTO_MS);
  return grabador;
}

/* ─────────────────────────── subida ─────────────────────────── */

/** Convierte un fragmento a base64, sin comillas ni el prefijo. */
function convertirABase64(blob) {
  return new Promise((resolver, rechazar) => {
    const lector = new FileReader();
    lector.onerror = () => rechazar(new Error('No se pudo leer el fragmento.'));
    lector.onload = () => {
      const resultado = String(lector.result || '');
      const coma = resultado.indexOf(',');
      resolver(coma >= 0 ? resultado.slice(comma + 1) : resultado);
    };
    lector.readAsDataURL(blob);
  });
}

/** Sube un fragmento, con reintentos. */
async function subirFragmento(fragmento) {
  if (fragmento.enviado) {
    return;
  }
  if (!estado.artefactoId) {
    registrar('No hay artefacto de audio: el fragmento se descarta.', 'error');
    return;
  }
  fragmento.intentos += 1;
  try {
    const contenido = await convertirABase64(fragmento.blob);
    const respuesta = await fetch(
      `/captura/artefactos/${estado.artefactoId}/fragmentos`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ indice: fragmento.indice, contenido_base64: contenido }),
        credentials: 'same-origin',
      },
    );
    if (respuesta.ok) {
      fragmento.enviado = true;
      estado.desconexion = false;
      refrescarEstado();
      return;
    }
    throw new Error(`El servidor respondió ${respuesta.status}`);
  } catch (error) {
    if (fragmento.intentos < MAXIMO_REINTENTOS) {
      estado.desconexion = true;
      refrescarEstado();
      registrar(
        `Fallo al subir el fragmento ${fragmento.indice} (intento ${fragmento.intentos}): `
          + `${error.message}. Se reintentará.`,
        'aviso',
      );
      window.setTimeout(() => subirFragmento(fragmento), ESPERA_REINTENTO_MS);
      return;
    }
    estado.desconexion = true;
    refrescarEstado();
    registrar(
      `El fragmento ${fragmento.indice} no se ha podido subir tras `
        + `${MAXIMO_REINTENTOS} intentos. La grabación puede estar incompleta.`,
      'error',
    );
  }
}

/* ─────────────────────────── ciclo de vida ─────────────────────────── */

/** Cambia el estado en el servidor. */
async function cambiarEstado(destino, extra) {
  if (!estado.sesionId) {
    return false;
  }
  try {
    const respuesta = await fetch(`/captura/sesiones/${estado.sesionId}/estado`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ estado: destino }, extra || {})),
      credentials: 'same-origin',
    });
    if (!respuesta.ok) {
      const cuerpo = await respuesta.json().catch(() => ({}));
      throw new Error(cuerpo.detail || `El servidor respondió ${respuesta.status}`);
    }
    const cuerpo = await respuesta.json();
    estado.estadoCaptura = cuerpo.estado;
    if (cuerpo.modo_efectivo) {
      estado.modoEfectivo = cuerpo.modo_efectivo;
    }
    refrescarEstado();
    return true;
  } catch (error) {
    registrar(`No se pudo cambiar el estado a ${destino}: ${error.message}`, 'error');
    return false;
  }
}

/** Libera todas las pistas y todos los recursos. */
function liberarRecursos() {
  [estado.pistaMicrofono, estado.pistaRemoto].forEach((pista) => {
    if (pista && typeof pista.stop === 'function') {
      pista.stop();
    }
  });
  [estado.flujoMicrofono, estado.flujoRemoto].forEach((flujo) => {
    if (flujo && typeof flujo.getTracks === 'function') {
      flujo.getTracks().forEach((p) => p.stop());
    }
  });
  estado.pistaMicrofono = null;
  estado.pistaRemoto = null;
  estado.flujoMicrofono = null;
  estado.flujoRemoto = null;
  estado.grabadorMicrofono = null;
  estado.grabadorRemoto = null;
  if (estado.contextoAudio && estado.contextoAudio.close) {
    estado.contextoAudio.close().catch(() => {});
  }
  estado.contextoAudio = null;
  estado.analizadorMicrofono = null;
  estado.analizadorRemoto = null;
}

/** Arranca el cronómetro. */
function arrancarCronometro() {
  if (estado.cronometro) {
    window.clearInterval(estado.cronometro);
  }
  let ultimo = Date.now();
  estado.cronometro = window.setInterval(() => {
    const ahora = Date.now();
    if (estado.estadoCaptura === ESTADOS.RECORDING) {
      estado.duracionAcumuladaMs += ahora - ultimo;
    }
    ultimo = ahora;
    refrescarEstado();
  }, 500);
}

/** Pide la autorización al usuario y la registra en el servidor. */
async function registrarAutorizacion() {
  if (!estado.sesionId) {
    return false;
  }
  try {
    const respuesta = await fetch(`/captura/sesiones/${estado.sesionId}/autorizacion`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tipo: 'declaracion_autorizacion' }),
      credentials: 'same-origin',
    });
    if (!respuesta.ok) {
      throw new Error(`El servidor respondió ${respuesta.status}`);
    }
    registrar('Declaración de autorización registrada.', 'info');
    return true;
  } catch (error) {
    registrar(`No se pudo registrar la autorización: ${error.message}`, 'error');
    return false;
  }
}

/**
 * Prepara la captura: pide los permisos que la persona haya elegido.
 *
 * Es la única función que pide permisos, y **solo** se llama desde el
 * manejador del botón de iniciar. Si el audio remoto no está
 * disponible, se avisa y se ofrece el modo «solo micrófono», que
 * requiere una confirmación explícita y deja la sesión marcada como
 * **no completa**.
 */
async function prepararCaptura() {
  const selector = document.getElementById('selector-microfono');
  const deviceId = selector && selector.value ? selector.value : '';

  const quiereRemoto = estado.modoSolicitado !== MODOS.MIC_ONLY;
  const quiereMicro = estado.modoSolicitado !== MODOS.SHARED_AUDIO_ONLY;

  let microfonoOk = false;
  let remotoOk = false;

  if (quiereMicro) {
    microfonoOk = await pedirMicrofono(deviceId);
  }
  if (quiereRemoto) {
    remotoOk = await pedirAudioRemoto();
  }

  if (!microfonoOk && !remotoOk) {
    registrar('No se ha obtenido ninguna fuente de audio. No se puede grabar.', 'error');
    liberarRecursos();
    return false;
  }

  // Modo efectivo: lo que **de verdad** se consiguió.
  let efectivo = MODOS.MIC_ONLY;
  if (microfonoOk && remotoOk) {
    efectivo = MODOS.MIC_AND_SHARED_AUDIO;
  } else if (remotoOk) {
    efectivo = MODOS.SHARED_AUDIO_ONLY;
  }

  if (efectivo !== estado.modoSolicitado) {
    const aviso = document.getElementById('aviso-audio-remoto');
    if (aviso) {
      aviso.hidden = false;
      aviso.textContent =
        `Se pidió el modo ${estado.modoSolicitado} y se ha conseguido el `
        + `modo ${efectivo}. La captura **no es completa**.`;
    }
    if (!confirmar(
      'No se ha obtenido todo el audio solicitado. Si continúa, la sesión '
        + 'quedará registrada como captura incompleta. ¿Continuar?',
    )) {
      liberarRecursos();
      return false;
    }
  }

  estado.modoEfectivo = efectivo;
  montarMedidores();
  bucleDeNiveles();
  return true;
}

/** Inicia la grabación. */
async function iniciar() {
  if (!estado.sesionId) {
    return;
  }
  const preparado = await prepararCaptura();
  if (!preparado) {
    return;
  }

  try {
    const respuesta = await fetch(`/captura/sesiones/${estado.sesionId}/artefactos`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ formato: (elegirFormato() || 'audio/webm').split(';')[0] }),
      credentials: 'same-origin',
    });
    if (!respuesta.ok) {
      throw new Error(`El servidor respondió ${respuesta.status}`);
    }
    const cuerpo = await respuesta.json();
    estado.artefactoId = cuerpo.id;
  } catch (error) {
    registrar(`No se pudo abrir la carga de audio: ${error.message}`, 'error');
    liberarRecursos();
    return;
  }

  estado.indiceSiguiente = 0;
  estado.fragmentos = [];
  estado.grabadorMicrofono = iniciarGrabador(estado.flujoMicrofono, 'microfono');
  estado.grabadorRemoto = iniciarGrabador(estado.flujoRemoto, 'remoto');
  estado.iniciadoEn = Date.now();

  const ok = await cambiarEstado(ESTADOS.RECORDING, {
    modo_efectivo: estado.modoEfectivo,
  });
  if (!ok) {
    liberarRecursos();
    return;
  }
  arrancarCronometro();
  registrar('Captura iniciada.', 'info');
}

/** Pausa la grabación. */
async function pausar() {
  if (estado.grabadorMicrofono && estado.grabadorMicrofono.state === 'recording') {
    estado.grabadorMicrofono.pause();
  }
  if (estado.grabadorRemoto && estado.grabadorRemoto.state === 'recording') {
    estado.grabadorRemoto.pause();
  }
  await cambiarEstado(ESTADOS.PAUSED);
  registrar('Captura en pausa.', 'info');
}

/** Reanuda la grabación. */
async function reanudar() {
  if (estado.grabadorMicrofono && estado.grabadorMicrofono.state === 'paused') {
    estado.grabadorMicrofono.resume();
  }
  if (estado.grabadorRemoto && estado.grabadorRemoto.state === 'paused') {
    estado.grabadorRemoto.resume();
  }
  await cambiarEstado(ESTADOS.RECORDING, { modo_efectivo: estado.modoEfectivo });
  registrar('Captura reanudada.', 'info');
}

/** Detiene la grabación y finaliza la carga. */
async function detener() {
  [estado.grabadorMicrofono, estado.grabadorRemoto].forEach((grabador) => {
    if (grabador && grabador.state !== 'inactive') {
      grabador.stop();
    }
  });
  if (estado.cronometro) {
    window.clearInterval(estado.cronometro);
    estado.cronometro = null;
  }
  await cambiarEstado(ESTADOS.STOPPED, {
    duracion_ms: estado.duracionAcumuladaMs,
  });
  liberarRecursos();

  // El último fragmento se emite al detener; hay que esperar a que
  // llegue antes de finalizar la carga, o se declararía incompleta.
  window.setTimeout(async () => {
    if (!estado.artefactoId) {
      return;
    }
    try {
      const respuesta = await fetch(
        `/captura/artefactos/${estado.artefactoId}/finalizar`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ duracion_ms: estado.duracionAcumuladaMs }),
          credentials: 'same-origin',
        },
      );
      const cuerpo = await respuesta.json();
      if (cuerpo.completa) {
        registrar('Carga finalizada y completa.', 'info');
      } else {
        registrar(
          `La carga se ha finalizado con huecos: faltan los fragmentos `
            + `${cuerpo.faltantes.join(', ')}.`,
          'aviso',
        );
      }
    } catch (error) {
      registrar(`No se pudo finalizar la carga: ${error.message}`, 'error');
    }
  }, 800);
}

/** Cancela la sesión y libera todo. */
async function cancelar() {
  if (!confirmar('¿Cancelar la sesión? Se perderá lo grabado hasta ahora.')) {
    return;
  }
  [estado.grabadorMicrofono, estado.grabadorRemoto].forEach((grabador) => {
    if (grabador && grabador.state !== 'inactive') {
      grabador.stop();
    }
  });
  if (estado.cronometro) {
    window.clearInterval(estado.cronometro);
    estado.cronometro = null;
  }
  await cambiarEstado(ESTADOS.CANCELLED);
  liberarRecursos();
  registrar('Sesión cancelada.', 'aviso');
}

/* ─────────────────────────── notas ─────────────────────────── */

/** Añade una nota manual con su marca de tiempo. */
async function anadirNota() {
  const campo = document.getElementById('campo-nota');
  const lista = document.getElementById('lista-notas');
  if (!campo || !campo.value.trim() || !estado.sesionId) {
    return;
  }
  const texto = campo.value.trim();
  try {
    const respuesta = await fetch(`/captura/sesiones/${estado.sesionId}/notas`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ texto, marca_ms: estado.duracionAcumuladaMs }),
      credentials: 'same-origin',
    });
    if (!respuesta.ok) {
      throw new Error(`El servidor respondió ${respuesta.status}`);
    }
    const cuerpo = await respuesta.json();
    if (lista) {
      const item = document.createElement('li');
      item.textContent = `${formatearMmss(cuerpo.marca_ms)} · ${cuerpo.texto}`;
      lista.appendChild(item);
    }
    campo.value = '';
    registrar('Nota añadida.', 'info');
  } catch (error) {
    registrar(`No se pudo añadir la nota: ${error.message}`, 'error');
  }
}

/* ─────────────────────────── arranque ─────────────────────────── */

/** Avisa antes de que la pestaña se cierre con contenido sin enviar. */
function avisarAlCerrar(evento) {
  const pendientes = estado.fragmentos.filter((f) => !f.enviado);
  if (estado.estadoCaptura === ESTADOS.RECORDING || pendientes.length > 0) {
    evento.preventDefault();
    // Chrome exige returnValue para mostrar el aviso.
    evento.returnValue = 'Hay contenido de la captura que todavía no se ha enviado.';
    return evento.returnValue;
  }
  return undefined;
}

/** Engancha los manejadores. */
function iniciarInterfaz() {
  const sesionId = document.body.getAttribute('data-sesion-id');
  const modo = document.body.getAttribute('data-modo-solicitado');
  estado.sesionId = sesionId;
  estado.modoSolicitado = modo || MODOS.MIC_ONLY;

  const botonPermisos = document.getElementById('boton-permisos');
  if (botonPermisos) {
    botonPermisos.addEventListener('click', () => {
      enumerarDispositivos();
    });
  }

  const botonAutorizar = document.getElementById('boton-autorizar');
  if (botonAutorizar) {
    botonAutorizar.addEventListener('click', async () => {
      const ok = await registrarAutorizacion();
      if (ok) {
        await cambiarEstado(ESTADOS.READY);
        botonAutorizar.disabled = true;
      }
    });
  }

  const botonIniciar = document.getElementById('boton-iniciar');
  if (botonIniciar) {
    botonIniciar.addEventListener('click', () => {
      iniciar();
    });
  }
  const botonPausar = document.getElementById('boton-pausar');
  if (botonPausar) {
    botonPausar.addEventListener('click', () => pausar());
  }
  const botonReanudar = document.getElementById('boton-reanudar');
  if (botonReanudar) {
    botonReanudar.addEventListener('click', () => reanudar());
  }
  const botonDetener = document.getElementById('boton-detener');
  if (botonDetener) {
    botonDetener.addEventListener('click', () => detener());
  }
  const botonCancelar = document.getElementById('boton-cancelar');
  if (botonCancelar) {
    botonCancelar.addEventListener('click', () => cancelar());
  }
  const botonNota = document.getElementById('boton-nota');
  if (botonNota) {
    botonNota.addEventListener('click', () => anadirNota());
  }
  const selectorModo = document.getElementById('selector-modo');
  if (selectorModo) {
    selectorModo.value = estado.modoSolicitado;
    selectorModo.addEventListener('change', (evento) => {
      estado.modoSolicitado = evento.target.value;
      registrar(`Modo solicitado: ${estado.modoSolicitado}.`, 'info');
    });
  }

  window.addEventListener('beforeunload', avisarAlCerrar);
  refrescarEstado();
  registrar(
    'Interfaz lista. No se ha solicitado ningún permiso: los permisos se '
      + 'piden al pulsar el botón correspondiente.',
    'info',
  );
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', iniciarInterfaz);
} else {
  iniciarInterfaz();
}

window.ProjectaCaptura = {
  estado,
  MODOS,
  ESTADOS,
  elegirFormato,
  enumerarDispositivos,
  pedirMicrofono,
  pedirAudioRemoto,
  liberarRecursos,
  formatearMmss,
};
