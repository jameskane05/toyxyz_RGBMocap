"""Safari camera page. Portrait 1080p H.264, or JPEG when the browser cannot encode."""
import base64
import hashlib
import struct

_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
_MAX_MESSAGE = 4 * 1024 * 1024

CAMERA_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>RGB MoCap Camera</title>
<style>
  html, body { margin: 0; height: 100%; background: #111; color: #eee; font: 16px sans-serif; }
  video { position: fixed; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  #stage { position: fixed; inset: 0; z-index: 1; display: flex; align-items: center; justify-content: center; background: #111; }
  #screen { height: 100%; width: auto; max-width: 100%; background: #111; }
  .bar { position: fixed; left: 0; right: 0; bottom: 0; z-index: 2; display: flex; flex-direction: column; align-items: center; gap: 8px;
    padding: 12px 16px calc(12px + env(safe-area-inset-bottom));
    background: linear-gradient(transparent, rgba(0,0,0,.78)); }
  .row { display: flex; flex-direction: row; justify-content: center; align-items: center; gap: 18px; width: 100%; }
  button { width: 64px; height: 64px; padding: 0; border: 0; border-radius: 50%; background: #e6c36a; color: #1c1c1c;
    display: flex; align-items: center; justify-content: center; }
  button svg { width: 30px; height: 30px; display: block; }
  button:disabled { opacity: .45; }
  #note { margin: 0; min-height: 1.2em; font-size: 13px; line-height: 1.3; text-align: center; }
</style>
<video id="view" autoplay muted playsinline></video>
<div id="stage"><canvas id="screen"></canvas></div>
<div class="bar">
  <p id="note"></p>
  <div class="row">
    <button id="start" type="button" aria-label="Start camera"></button>
    <button id="framing" type="button" aria-label="Upright"></button>
    <button id="record" type="button" aria-label="Record" disabled></button>
  </div>
</div>
<script>
const video = document.getElementById('view');
const note = document.getElementById('note');
const startButton = document.getElementById('start');
const recordButton = document.getElementById('record');
const framingButton = document.getElementById('framing');
const forceJpeg = new URLSearchParams(location.search).get('jpeg') === '1';
const canvas = document.getElementById('screen');
const context = canvas.getContext('2d', { alpha: false });
const preview = document.createElement('canvas');
const previewContext = preview.getContext('2d', { alpha: false });

let cameraOn = false;
let front = false;
let media = null;
let track = null;
let socket = null;
let polling = false;
let scheduled = false;
let streaming = false;
let serverRecording = false;
let mode = null;
let encoder = null;
let parameterSets = null;
let lengthSize = 4;
let encodeWidth = 0;
let encodeHeight = 0;
let offset = 0;
let epoch = null;
let lastPts = 0;
let nextKey = true;
let encoded = 0;
let sentVideo = false;
let jpegBusy = false;
let previewBusy = false;
let previewAt = 0;
let preparing = null;
let captureBusy = false;
let framing = 'upright';
let audioContext = null;
let audioSource = null;
let audioProcessor = null;
let audioMute = null;
let sendAudio = false;
try { framing = sessionStorage.getItem('rgb-framing') === 'rotated' ? 'rotated' : 'upright'; } catch (error) {}

const ICONS = {
  camera: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="M4 8h3l1.5-2h7L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.2"/></svg>',
  flip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8a8 8 0 0 1 13.5-3L20 7"/><path d="M20 3v4h-4"/><path d="M20 16a8 8 0 0 1-13.5 3L4 17"/><path d="M4 21v-4h4"/></svg>',
  upright: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="7" y="3" width="10" height="18" rx="2"/></svg>',
  rotated: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="7" width="18" height="10" rx="2"/></svg>',
  record: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="7" fill="currentColor"/></svg>',
  stop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor"/></svg>'
};

function paintButton(button, icon, label) {
  if (button.dataset.icon === label) return;
  button.dataset.icon = label;
  button.innerHTML = icon;
  button.setAttribute('aria-label', label);
}

function paintControls() {
  paintButton(startButton, cameraOn ? ICONS.flip : ICONS.camera, cameraOn ? 'Flip camera' : 'Start camera');
  paintButton(framingButton, framing === 'rotated' ? ICONS.rotated : ICONS.upright, framing === 'rotated' ? 'Rotated' : 'Upright');
  paintButton(recordButton, serverRecording ? ICONS.stop : ICONS.record, serverRecording ? 'Stop' : 'Record');
}

video.muted = true;
video.playsInline = true;

function even(value) {
  return Math.max(2, Math.round(value / 2) * 2);
}

function frameSize(width, height) {
  const scale = 1920 / Math.max(width, height);
  return [even(width * scale), even(height * scale)];
}

function useFrame(size) {
  if (canvas.width !== size[0] || canvas.height !== size[1]) {
    canvas.width = size[0];
    canvas.height = size[1];
    if (encodeWidth !== size[0] || encodeHeight !== size[1]) mode = null;
    encodeWidth = size[0];
    encodeHeight = size[1];
  }
  const ratio = size[0] / size[1];
  if (window.innerWidth / window.innerHeight > ratio) {
    canvas.style.height = window.innerHeight + 'px';
    canvas.style.width = Math.round(window.innerHeight * ratio) + 'px';
  } else {
    canvas.style.width = window.innerWidth + 'px';
    canvas.style.height = Math.round(window.innerWidth / ratio) + 'px';
  }
}

function paintUpright(bitmap) {
  const size = frameSize(bitmap.width, bitmap.height);
  useFrame(size);
  context.drawImage(bitmap, 0, 0, size[0], size[1]);
}

function paintRotated(bitmap) {
  const srcW = bitmap.width;
  const srcH = bitmap.height;
  if (!(srcW > srcH && window.innerHeight >= window.innerWidth)) {
    const size = frameSize(srcW, srcH);
    useFrame(size);
    context.drawImage(bitmap, 0, 0, size[0], size[1]);
    return;
  }
  const size = frameSize(srcH, srcW);
  useFrame(size);
  if (front) {
    context.translate(0, size[1]);
    context.rotate(-Math.PI / 2);
  } else {
    context.translate(size[0], 0);
    context.rotate(Math.PI / 2);
  }
  context.drawImage(bitmap, 0, 0, size[1], size[0]);
}

function paint(bitmap) {
  context.setTransform(1, 0, 0, 1, 0, 0);
  if (framing === 'rotated') paintRotated(bitmap);
  else paintUpright(bitmap);
  context.setTransform(1, 0, 0, 1, 0, 0);
}

function clockUs() {
  const origin = epoch || 0;
  return Math.max(0, Math.round((Date.now() / 1000 + offset - origin) * 1e6));
}

function ptsUs() {
  let value = clockUs();
  if (value <= lastPts) value = lastPts + 1;
  lastPts = value;
  return value;
}

function concat(parts) {
  let size = 0;
  parts.forEach((part) => { size += part.length; });
  const out = new Uint8Array(size);
  let cursor = 0;
  parts.forEach((part) => { out.set(part, cursor); cursor += part.length; });
  return out;
}

function startsAnnexB(data) {
  return data.length > 3 && data[0] === 0 && data[1] === 0 && (data[2] === 1 || (data[2] === 0 && data[3] === 1));
}

function nalTypes(data) {
  const types = [];
  let index = 0;
  while (index + 3 < data.length) {
    let typeAt = -1;
    if (index + 4 < data.length && data[index] === 0 && data[index + 1] === 0 && data[index + 2] === 0 && data[index + 3] === 1) typeAt = index + 4;
    else if (data[index] === 0 && data[index + 1] === 0 && data[index + 2] === 1) typeAt = index + 3;
    if (typeAt < 0 || typeAt >= data.length) { index += 1; continue; }
    types.push(data[typeAt] & 31);
    index = typeAt + 1;
  }
  return types;
}

function remember(description) {
  const data = new Uint8Array(description);
  if (startsAnnexB(data)) { parameterSets = data; return; }
  if (data.length < 7) return;
  lengthSize = (data[4] & 3) + 1;
  let cursor = 5;
  const parts = [];
  const spsCount = data[cursor] & 31;
  cursor += 1;
  for (let i = 0; i < spsCount && cursor + 2 <= data.length; i += 1) {
    const size = (data[cursor] << 8) | data[cursor + 1];
    cursor += 2;
    parts.push(new Uint8Array([0, 0, 0, 1]), data.subarray(cursor, cursor + size));
    cursor += size;
  }
  if (cursor >= data.length) { parameterSets = concat(parts); return; }
  const ppsCount = data[cursor];
  cursor += 1;
  for (let i = 0; i < ppsCount && cursor + 2 <= data.length; i += 1) {
    const size = (data[cursor] << 8) | data[cursor + 1];
    cursor += 2;
    parts.push(new Uint8Array([0, 0, 0, 1]), data.subarray(cursor, cursor + size));
    cursor += size;
  }
  parameterSets = parts.length ? concat(parts) : null;
}

function lengthPrefixed(data) {
  const parts = [];
  let cursor = 0;
  while (cursor + lengthSize <= data.length) {
    let size = 0;
    for (let i = 0; i < lengthSize; i += 1) size = (size << 8) | data[cursor + i];
    cursor += lengthSize;
    if (size <= 0 || cursor + size > data.length) break;
    parts.push(new Uint8Array([0, 0, 0, 1]), data.subarray(cursor, cursor + size));
    cursor += size;
  }
  return parts.length ? concat(parts) : data;
}

function sendAt(kind, payload, key, pts) {
  if (!socket || socket.readyState !== WebSocket.OPEN || !payload.length) return;
  const header = new Uint8Array(10);
  new DataView(header.buffer).setBigUint64(1, BigInt(pts), false);
  header[0] = kind;
  header[9] = key ? 1 : 0;
  const out = new Uint8Array(10 + payload.length);
  out.set(header, 0);
  out.set(payload, 10);
  socket.send(out);
  sentVideo = true;
}

function onChunk(chunk, meta) {
  if (meta && meta.decoderConfig && meta.decoderConfig.description) remember(meta.decoderConfig.description);
  const raw = new Uint8Array(chunk.byteLength);
  chunk.copyTo(raw);
  let unit = startsAnnexB(raw) ? raw : lengthPrefixed(raw);
  if (chunk.type === 'key' && parameterSets && nalTypes(unit).indexOf(7) < 0) unit = concat([parameterSets, unit]);
  sendAt(1, unit, chunk.type === 'key', Math.max(0, Math.round(chunk.timestamp)));
}

async function supported(config) {
  if (!VideoEncoder.isConfigSupported) return false;
  try {
    const result = await VideoEncoder.isConfigSupported(config);
    return !!(result && result.supported);
  } catch (error) {
    return false;
  }
}

async function pickConfig(width, height) {
  const codecs = ['avc1.640028', 'avc1.4d0028', 'avc1.420028'];
  for (const codec of codecs) {
    const configs = [
      { codec, width, height, bitrate: 8000000, framerate: 30, latencyMode: 'realtime', hardwareAcceleration: 'prefer-hardware', avc: { format: 'annexb' } },
      { codec, width, height, bitrate: 8000000, framerate: 30, latencyMode: 'realtime', hardwareAcceleration: 'prefer-hardware' }
    ];
    for (const config of configs) if (await supported(config)) return config;
  }
  return null;
}

async function prepareEncoder() {
  if (!encodeWidth || !encodeHeight) return;
  if (!forceJpeg && typeof VideoEncoder === 'function') {
    const config = await pickConfig(encodeWidth, encodeHeight);
    if (config) {
      try {
        if (encoder) encoder.close();
        parameterSets = null;
        lengthSize = 4;
        encoder = new VideoEncoder({
          output: onChunk,
          error: () => {
            if (!sentVideo) mode = 'jpeg';
            note.textContent = sentVideo ? 'Encoder stopped. Stop the take and record again.' : 'H.264 unavailable. Sending JPEG.';
          }
        });
        encoder.configure(config);
        mode = 'h264';
        return;
      } catch (error) {}
    }
  }
  mode = 'jpeg';
}

function grab() {
  return !!(encodeWidth && canvas.width === encodeWidth && canvas.height === encodeHeight);
}

function encodeFrame() {
  if (!encoder || encoder.encodeQueueSize > 3) { nextKey = true; return; }
  if (!grab()) return;
  let frame = null;
  try {
    frame = new VideoFrame(canvas, { timestamp: ptsUs() });
    encoder.encode(frame, { keyFrame: nextKey });
    nextKey = false;
    encoded += 1;
    if (encoded % 30 === 0) nextKey = true;
  } catch (error) {
    if (!sentVideo) mode = 'jpeg';
  } finally {
    if (frame) frame.close();
  }
}

function sendJpeg() {
  if (jpegBusy || !grab()) return;
  jpegBusy = true;
  const pts = ptsUs();
  canvas.toBlob((blob) => {
    jpegBusy = false;
    if (!blob || !streaming) return;
    blob.arrayBuffer().then((buffer) => sendAt(2, new Uint8Array(buffer), true, pts)).catch(() => {});
  }, 'image/jpeg', 0.72);
}

function maybePreview() {
  const now = performance.now();
  if (previewBusy || !canvas.width || now - previewAt < 100) return;
  previewAt = now;
  previewBusy = true;
  const longSide = Math.max(canvas.width, canvas.height);
  const scale = Math.min(1, 640 / longSide);
  preview.width = Math.max(2, Math.round(canvas.width * scale));
  preview.height = Math.max(2, Math.round(canvas.height * scale));
  previewContext.drawImage(canvas, 0, 0, preview.width, preview.height);
  preview.toBlob((blob) => {
    previewBusy = false;
    if (!blob) return;
    fetch('/preview', { method: 'POST', headers: { 'Content-Type': 'image/jpeg' }, body: blob }).catch(() => {});
  }, 'image/jpeg', 0.6);
}

async function pump() {
  if (!cameraOn || !video.videoWidth) return;
  let bitmap = null;
  try {
    bitmap = await createImageBitmap(video);
    paint(bitmap);
  } catch (error) {
    return;
  } finally {
    if (bitmap) bitmap.close();
  }
  maybePreview();
  if (mode === null && !preparing) preparing = prepareEncoder().finally(() => { preparing = null; });
  if (!streaming || !socket || socket.readyState !== WebSocket.OPEN || mode === null) return;
  if (socket.bufferedAmount > 1500000) { nextKey = true; return; }
  if (mode === 'h264') encodeFrame();
  else sendJpeg();
}

function schedule() {
  if (!cameraOn) { scheduled = false; return; }
  const run = () => {
    if (!cameraOn) return;
    schedule();
    if (captureBusy) return;
    captureBusy = true;
    pump().finally(() => { captureBusy = false; });
  };
  if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(() => run());
  else setTimeout(run, 33);
}

function sendPcm(input, rate) {
  if (!socket || socket.readyState !== WebSocket.OPEN || !input.length) return;
  const pcm = new Int16Array(input.length);
  for (let i = 0; i < input.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, input[i]));
    pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  const body = new Uint8Array(4 + pcm.byteLength);
  new DataView(body.buffer).setUint32(0, rate, false);
  body.set(new Uint8Array(pcm.buffer), 4);
  const pts = Math.max(0, clockUs() - Math.round(input.length / rate * 1e6));
  sendAt(3, body, false, pts);
}

function stopAudio() {
  sendAudio = false;
  if (audioProcessor) audioProcessor.onaudioprocess = null;
  [audioProcessor, audioSource, audioMute].forEach((node) => { if (node) node.disconnect(); });
  audioProcessor = null;
  audioSource = null;
  audioMute = null;
  if (audioContext) audioContext.close().catch(() => {});
  audioContext = null;
}

function startAudio(stream) {
  const audioTracks = stream.getAudioTracks();
  if (!audioTracks.length || audioContext) return;
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) return;
  try {
    audioContext = new Context();
    audioSource = audioContext.createMediaStreamSource(stream);
    audioProcessor = audioContext.createScriptProcessor(4096, 1, 1);
    audioMute = audioContext.createGain();
    audioMute.gain.value = 0;
    audioProcessor.onaudioprocess = (event) => {
      if (!sendAudio) return;
      const buffer = event.inputBuffer;
      sendPcm(buffer.getChannelData(0), buffer.sampleRate || audioContext.sampleRate || 48000);
    };
    audioSource.connect(audioProcessor);
    audioProcessor.connect(audioMute);
    audioMute.connect(audioContext.destination);
    audioContext.resume().catch(() => {});
  } catch (error) {
    stopAudio();
  }
}

function ensureSocket() {
  if (!cameraOn) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  socket = new WebSocket(location.origin.replace(/^http/, 'ws') + '/camera-stream');
  socket.binaryType = 'arraybuffer';
  socket.onclose = () => { if (cameraOn) setTimeout(ensureSocket, 400); };
}

async function poll() {
  if (!cameraOn) { polling = false; return; }
  const started = performance.now();
  try {
    const response = await fetch('/status', { cache: 'no-store' });
    const status = await response.json();
    const rtt = (performance.now() - started) / 1000;
    if (typeof status.server_time === 'number') offset = status.server_time - Date.now() / 1000 - rtt / 2;
    if (status.epoch !== epoch) { epoch = status.epoch; lastPts = 0; nextKey = true; }
    serverRecording = !!status.recording;
    const accepting = !!status.accepting_video;
    const want = serverRecording && accepting;
    if (want !== streaming) { streaming = want; nextKey = true; }
    sendAudio = status.monitor === 'phone' || (want && status.audio === 'phone');
    if (want || sendAudio) ensureSocket();
    const shape = framing === 'rotated' ? 'rotated' : 'upright';
    let line = (encodeWidth && encodeHeight) ? (encodeWidth + '×' + encodeHeight + ' ' + shape) : 'camera';
    if (mode === 'h264') line += ' · H.264';
    if (mode === 'jpeg') line += ' · JPEG';
    if (status.camera_error) line += ' · ' + status.camera_error;
    else if (streaming) line += ' · recording';
    else if (serverRecording) line += ' · waiting for the take';
    else line += ' · ready';
    note.textContent = line;
    paintControls();
  } catch (error) {
    note.textContent = 'Cannot reach RGB MoCap.';
  }
  setTimeout(poll, 200);
}

async function preferWidest(next) {
  const attempts = [
    { width: { ideal: 1920 }, height: { ideal: 1440 }, aspectRatio: { ideal: 4 / 3 }, resizeMode: 'none', frameRate: { ideal: 30, max: 30 } },
    { width: { ideal: 1920 }, height: { ideal: 1440 }, aspectRatio: { ideal: 4 / 3 }, frameRate: { ideal: 30, max: 30 } },
    { frameRate: { ideal: 30, max: 30 } }
  ];
  for (const attempt of attempts) {
    try {
      await next.applyConstraints(attempt);
      return;
    } catch (error) {}
  }
}

function stopTracks() {
  stopAudio();
  if (media) media.getTracks().forEach((item) => item.stop());
  media = null;
}

async function startCamera() {
  if (cameraOn) front = !front;
  startButton.disabled = true;
  const videoConstraints = {
    facingMode: front ? 'user' : 'environment',
    width: { ideal: 1920 },
    height: { ideal: 1440 },
    aspectRatio: { ideal: 4 / 3 },
    frameRate: { ideal: 30, max: 30 }
  };
  const audioConstraints = { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 };
  let stream;
  try {
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints, video: Object.assign({ resizeMode: 'none' }, videoConstraints) });
    } catch (error) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints, video: videoConstraints });
      } catch (again) {
        stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: videoConstraints });
      }
    }
  } catch (error) {
    startButton.disabled = false;
    note.textContent = 'Camera blocked. ' + (error && error.message ? error.message : error);
    return;
  }
  stopTracks();
  media = stream;
  track = stream.getVideoTracks()[0];
  startAudio(stream);
  await preferWidest(track);
  if (encoder) { encoder.close(); encoder = null; }
  mode = null;
  nextKey = true;
  video.srcObject = stream;
  await video.play();
  cameraOn = true;
  paintControls();
  startButton.disabled = false;
  recordButton.disabled = false;
  fetch('/hello', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"role":"iphone"}' }).catch(() => {});
  ensureSocket();
  if (!scheduled) { scheduled = true; schedule(); }
  if (!polling) { polling = true; poll(); }
  if (navigator.wakeLock) navigator.wakeLock.request('screen').catch(() => {});
}

startButton.addEventListener('click', () => { startCamera(); });
framingButton.addEventListener('click', () => {
  framing = framing === 'rotated' ? 'upright' : 'rotated';
  paintControls();
  try { sessionStorage.setItem('rgb-framing', framing); } catch (error) {}
  nextKey = true;
  previewAt = 0;
});
paintControls();
recordButton.addEventListener('click', () => {
  if (audioContext) audioContext.resume().catch(() => {});
  const path = serverRecording ? '/stop' : '/start';
  fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
});
</script>
"""


def _read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining:
        piece = stream.read(remaining)
        if not piece:
            raise ConnectionError('camera stream closed')
        chunks.append(piece)
        remaining -= len(piece)
    return b''.join(chunks)


def _send_frame(stream, opcode, payload):
    header = bytearray([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header.append(size)
    elif size < 65536:
        header.append(126)
        header += struct.pack('>H', size)
    else:
        header.append(127)
        header += struct.pack('>Q', size)
    stream.write(header)
    stream.write(payload)
    stream.flush()


def _accept_value(key):
    digest = hashlib.sha1((key.strip() + _GUID).encode('ascii')).digest()
    return base64.b64encode(digest).decode('ascii')


def _read_message(stream):
    fragments = bytearray()
    opcode = None
    while True:
        b0, b1 = _read_exact(stream, 2)
        fin = b0 & 0x80
        current = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack('>H', _read_exact(stream, 2))[0]
        elif length == 127:
            length = struct.unpack('>Q', _read_exact(stream, 8))[0]
        if length > _MAX_MESSAGE:
            raise ValueError('Camera frame is too large.')
        mask = _read_exact(stream, 4) if masked else b''
        payload = bytearray(_read_exact(stream, length)) if length else bytearray()
        if mask:
            for index, value in enumerate(payload):
                payload[index] = value ^ mask[index % 4]
        if current == 8:
            return None
        if current == 9:
            _send_frame(stream, 0xA, payload)
            continue
        if current == 0xA:
            continue
        if current == 0:
            fragments += payload
        else:
            opcode = current
            fragments = payload
        if fin:
            message = bytes(fragments) if opcode == 2 else None
            fragments = bytearray()
            opcode = None
            if message is not None:
                return message
        if len(fragments) > _MAX_MESSAGE:
            raise ValueError('Camera frame is too large.')


def _ingest(hub, message, role):
    if len(message) < 11:
        return
    kind = message[0]
    pts = struct.unpack_from('>Q', message, 1)[0] / 1_000_000
    flags = message[9]
    payload = message[10:]
    if not payload:
        return
    if kind == 3 and len(payload) >= 6:
        source = 'phone' if role == 'iphone' else 'quest'
        record = getattr(hub, 'audio_source', '')
        monitor = getattr(hub, 'monitor', '')
        if source == record or source == monitor:
            pcm = payload[4:]
            hub.note_pcm(pcm)
            if source == record:
                rate = struct.unpack_from('>I', payload, 0)[0]
                hub.ingest_pcm(pts, rate, pcm)
        return
    phone = getattr(hub, 'phone', None)
    if phone is None or role != 'iphone':
        return
    if kind == 1:
        phone.ingest_access_unit(2, flags, pts, payload)
    elif kind == 2:
        phone.ingest_jpeg(pts, payload)


def serve_camera_socket(rfile, wfile, hub, client, role='iphone'):
    hub.hello(role, client)
    count = 0
    while True:
        message = _read_message(rfile)
        if message is None:
            return
        count += 1
        if count % 15 == 1:
            hub.hello(role, client)
        _ingest(hub, message, role)
