"""Local HTTPS receiver for live ARKit face frames from a Quest in immersive MR."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import shutil
import socket
import ssl
import subprocess
import time
from threading import Lock
from PySide6.QtCore import QThread, Signal
from .face_capture import ARKIT_NAMES, canonicalize
from .face_monitor import QuestMonitor
from .phone_stream import VIDEO_PORT, PhoneStream

DEFAULT_PORT = 8765


def _eye_summary(eyes):
    out = {}
    if not isinstance(eyes, dict):
        return out
    for side in ('left', 'right'):
        item = eyes.get(side)
        if not isinstance(item, dict) or item.get('confidence') is None:
            continue
        try:
            out[side] = round(float(item['confidence']), 3)
        except (TypeError, ValueError):
            continue
    return out


class FaceHub:
    def __init__(self):
        self.lock = Lock()
        self.recording = False
        self.started = None
        self.frames = []
        self.latest = None
        self.error = None
        self.received = None
        self.client = None
        self.arrivals = []
        self.epoch = None
        self.clients = {}
        self.latest_eyes = None
        self.eye_frames = []
        self.preview = None
        self.preview_at = None
        self.preview_generation = 0

    def _arm(self):
        self.recording = True
        self.started = time.monotonic()
        self.epoch = time.time()
        self.frames = []
        self.eye_frames = []
        self.latest = None
        self.latest_eyes = None
        self.error = None

    def start(self):
        with self.lock:
            self._arm()
            self.received = None
            self.client = None
            self.arrivals = []

    def begin_take(self):
        with self.lock:
            self._arm()

    def take_frames(self):
        with self.lock:
            frames = list(self.frames)
            self.frames = []
            self.started = time.monotonic()
            return frames

    def stop(self):
        with self.lock:
            self.recording = False
            return list(self.frames)

    def ingest(self, payload, client=None):
        if not isinstance(payload, dict):
            raise ValueError('Face frame must be a JSON object.')
        if isinstance(payload.get('face'), dict):
            nested = dict(payload['face'])
            nested.setdefault('t', payload.get('t'))
            payload = nested
        values = payload.get('values')
        names = payload.get('names')
        coeffs = (payload.get('coeffs') or payload.get('blendshapes')
                  or payload.get('blendShapes') or payload.get('coefficients'))
        row = [0.0]*len(ARKIT_NAMES)
        if isinstance(values, list):
            if names:
                for name, value in zip(names, values):
                    index = canonicalize(name)
                    if index is not None:
                        row[index] = max(row[index], float(value))
            else:
                for i, value in enumerate(values[:len(ARKIT_NAMES)]):
                    row[i] = float(value)
        elif isinstance(coeffs, dict):
            for name, value in coeffs.items():
                index = canonicalize(name)
                if index is not None:
                    row[index] = max(row[index], float(value))
        else:
            raise ValueError('Face frame needs values, names, or blendshapes.')
        with self.lock:
            t = payload.get('t')
            if t is None:
                origin = self.started if self.started is not None else time.monotonic()
                t = time.monotonic() - origin
            compact = [round(float(t), 4)] + [round(float(v), 4) for v in row]
            now = time.monotonic()
            self.latest = compact
            self.received = now
            self.client = client
            self.arrivals.append(now)
            while self.arrivals and now - self.arrivals[0] > 1.0:
                self.arrivals.pop(0)
            eyes = payload.get('eyes') if isinstance(payload.get('eyes'), dict) else None
            if eyes is not None:
                self.latest_eyes = eyes
            if self.recording:
                if self.started is None:
                    self.started = now
                    self.epoch = time.time()
                self.frames.append(compact)
                if eyes is not None:
                    self.eye_frames.append(dict(t=compact[0], eyes=eyes))
                if len(self.frames) > 20000:
                    self.frames = self.frames[-12000:]
                    self.eye_frames = self.eye_frames[-12000:]
            return compact

    def take_eyes(self):
        with self.lock:
            eyes = list(self.eye_frames)
            self.eye_frames = []
            return eyes

    def hello(self, role, client):
        if role not in ('quest', 'iphone'):
            raise ValueError('Role must be quest or iphone.')
        with self.lock:
            self.clients[role] = dict(address=client, seen=time.monotonic())

    def set_preview(self, data):
        with self.lock:
            self.preview = bytes(data)
            self.preview_at = time.monotonic()
            self.preview_generation += 1
            self.clients['iphone'] = dict(
                address=self.clients.get('iphone', {}).get('address'),
                seen=self.preview_at)

    def preview_jpeg(self):
        with self.lock:
            if self.preview is None or self.preview_at is None or time.monotonic() - self.preview_at > 0.6:
                return None
            return self.preview_generation, self.preview

    def latest_values(self):
        with self.lock:
            return None if self.latest is None else list(self.latest)

    def status(self):
        with self.lock:
            now = time.monotonic()
            age = None if self.received is None else now - self.received
            clients = {}
            for role, info in self.clients.items():
                seen = info.get('seen')
                if seen is not None and now - seen < 3.0:
                    clients[role] = dict(address=info.get('address'), age=round(now - seen, 3))
            eyes = self.latest_eyes or {}
            phone = getattr(self, 'phone', None)
            return dict(recording=self.recording, frames=len(self.frames),
                        latest=self.latest, error=self.error, client=self.client,
                        fps=len(self.arrivals), age=age,
                        connected=age is not None and age < 1.5,
                        epoch=self.epoch if self.recording else None,
                        server_time=time.time(),
                        clients=clients,
                        video_port=VIDEO_PORT,
                        accepting_video=bool(phone and phone.accepting()),
                        phone_connected=bool(phone and phone.connected()),
                        eyes=_eye_summary(eyes))


QUEST_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RGB MoCap</title>
<style>
  html, body { margin: 0; height: 100%; background: #1c1c1c; color: #eee; font: 18px sans-serif; }
  main { min-height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px; padding: 24px; text-align: center; }
  button { font: 22px sans-serif; padding: 16px 28px; border: 0; border-radius: 8px; background: #e6c36a; color: #1c1c1c; }
  button:disabled { opacity: 0.5; }
  p { max-width: 36rem; line-height: 1.4; }
  canvas { display: none; }
</style>
<main>
  <button id="enter" type="button">Enter MR</button>
  <p id="note">Accept the certificate warning if the browser shows one, then enter passthrough. Face tracking starts with the session.</p>
  <canvas id="gl"></canvas>
</main>
<script>
const button = document.getElementById('enter');
const note = document.getElementById('note');
const canvas = document.getElementById('gl');

function weightsFrom(face) {
  if (!face) return null;
  const bag = face.blendShapes || face.blendshapes || face.expressions
    || (typeof face.getBlendShapes === 'function' ? face.getBlendShapes() : null);
  if (!bag) return null;
  const out = {};
  if (typeof bag.forEach === 'function') {
    bag.forEach((value, key) => { out[key] = value; });
  } else if (Array.isArray(bag)) {
    bag.forEach((item) => {
      if (!item) return;
      const key = item.categoryName || item.name;
      if (key) out[key] = item.score ?? item.weight ?? item.value ?? 0;
    });
  } else if (typeof bag === 'object') {
    Object.assign(out, bag);
  }
  return Object.keys(out).length ? out : null;
}

function readFace(frame) {
  const names = ['getFace', 'getFaceExpression', 'getFacialExpression'];
  for (const name of names) {
    if (typeof frame[name] !== 'function') continue;
    try {
      const weights = weightsFrom(frame[name]());
      if (weights) return weights;
    } catch (error) {}
  }
  return null;
}

button.addEventListener('click', () => { enter(); });

async function enter() {
  if (!window.isSecureContext || !navigator.xr) {
    note.textContent = 'Immersive MR is only available over HTTPS.';
    return;
  }
  button.disabled = true;
  note.textContent = 'Starting MR…';
  let session;
  try {
    session = await navigator.xr.requestSession('immersive-ar', {
      optionalFeatures: ['local-floor', 'face-tracking', 'eye-tracking', 'oculus-face-tracking']
    });
  } catch (error) {
    button.disabled = false;
    note.textContent = 'Could not enter MR: ' + (error && error.message ? error.message : error);
    return;
  }
  const features = (session.enabledFeatures || []).join(', ') || 'none';
  let sent = 0;
  let faced = 0;
  let posted = false;
  session.addEventListener('end', () => {
    button.disabled = false;
    note.textContent = faced
      ? ('Left MR. Sent ' + sent + ' face frames. Features: ' + features)
      : ('Left MR without face data. Features: ' + features);
  });
  try {
    const gl = canvas.getContext('webgl', { alpha: true, antialias: false, xrCompatible: true });
    if (!gl) throw new Error('WebGL is unavailable');
    await gl.makeXRCompatible();
    const layer = new XRWebGLLayer(session, gl, { alpha: true });
    session.updateRenderState({ baseLayer: layer });
    const ref = await session.requestReferenceSpace(
      (session.enabledFeatures || []).indexOf('local-floor') >= 0 ? 'local-floor' : 'local'
    ).catch(() => session.requestReferenceSpace('local'));
    const onFrame = (time, frame) => {
      session.requestAnimationFrame(onFrame);
      const pose = frame.getViewerPose(ref);
      if (pose) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, layer.framebuffer);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      }
      const weights = readFace(frame);
      if (!weights || posted) return;
      faced += 1;
      posted = true;
      fetch('/frame', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ blendshapes: weights })
      }).then((response) => {
        if (response.ok) sent += 1;
      }).catch(() => {}).finally(() => { posted = false; });
    };
    session.requestAnimationFrame(onFrame);
  } catch (error) {
    button.disabled = false;
    note.textContent = 'MR setup failed: ' + (error && error.message ? error.message : error);
    session.end().catch(() => {});
  }
}
</script>
"""


def local_ips():
    ips = {'127.0.0.1'}
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(('1.1.1.1', 80))
        ips.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ips)


def find_openssl():
    # Git's OpenSSL works without a machine-wide openssl.cnf. The one on PATH may not.
    for candidate in (r'C:\Program Files\Git\usr\bin\openssl.exe',
                      r'C:\Program Files\Git\mingw64\bin\openssl.exe'):
        if Path(candidate).is_file():
            return candidate
    return shutil.which('openssl')


def ensure_certificate(directory, hosts):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cert, key, stamp = directory/'cert.pem', directory/'key.pem', directory/'hosts.txt'
    wanted = '\n'.join(hosts)
    if cert.is_file() and key.is_file() and stamp.is_file() and stamp.read_text(encoding='utf-8') == wanted:
        return cert, key
    openssl = find_openssl()
    if openssl is None:
        raise RuntimeError('OpenSSL is required to serve the Quest page over HTTPS.')
    names = ['DNS:localhost'] + ['IP:%s' % host for host in hosts]
    result = subprocess.run(
        [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-days', '825', '-nodes',
         '-keyout', str(key), '-out', str(cert), '-subj', '/CN=RGBMoCap',
         '-addext', 'subjectAltName=' + ','.join(names),
         '-addext', 'basicConstraints=CA:FALSE',
         '-addext', 'keyUsage=digitalSignature,keyEncipherment',
         '-addext', 'extendedKeyUsage=serverAuth'],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or not cert.is_file() or not key.is_file():
        detail = (result.stderr or result.stdout or 'certificate request failed').strip()
        raise RuntimeError(detail.splitlines()[-1])
    stamp.write_text(wanted, encoding='utf-8')
    return cert, key


def _handler(hub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def _send(self, code, payload, ctype='application/json'):
            body = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._send(204, b'')

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path == '/status':
                self._send(200, hub.status())
                return
            if path in ('/', '/frame', '/face'):
                self._send(200, QUEST_PAGE.encode('utf-8'), 'text/html; charset=utf-8')
                return
            self._send(404, dict(error='Not found'))

        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            if length > 8*1024**2:
                self._send(413, dict(error='Payload too large'))
                return
            raw = self.rfile.read(length) if length else b'{}'
            path = self.path.split('?', 1)[0]
            if path == '/preview':
                hub.set_preview(raw)
                self._send(200, dict(ok=True))
                return
            try:
                payload = json.loads(raw.decode('utf-8') or '{}')
                if path in ('/frame', '/face'):
                    hub.ingest(payload, client=self.client_address[0] if self.client_address else None)
                    self._send(200, dict(ok=True, frames=len(hub.frames)))
                elif path == '/recording':
                    frames = (payload.get('tracks') or {}).get('face', {}).get('frames') if payload.get('version') == 5 else payload.get('frames')
                    if not frames:
                        raise ValueError('Recording has no face frames.')
                    with hub.lock:
                        hub.frames = []
                        hub.recording = True
                    for row in frames:
                        if isinstance(row, dict):
                            hub.ingest(row)
                        elif isinstance(row, list):
                            hub.ingest(dict(t=row[0], values=row[1:1+len(ARKIT_NAMES)], names=list(ARKIT_NAMES)))
                    self._send(200, dict(ok=True, frames=len(hub.frames)))
                elif path == '/hello':
                    hub.hello(str(payload.get('role') or ''),
                              self.client_address[0] if self.client_address else None)
                    self._send(200, dict(ok=True))
                elif path == '/start':
                    hub.start()
                    self._send(200, dict(ok=True))
                elif path == '/stop':
                    frames = hub.stop()
                    self._send(200, dict(ok=True, frames=len(frames)))
                else:
                    self._send(404, dict(error='Not found'))
            except Exception as exc:
                hub.error = str(exc)
                self._send(400, dict(error=str(exc)))
    return Handler


class FaceListenServer(QThread):
    changed = Signal(object)
    failed = Signal(str)

    def __init__(self, port=DEFAULT_PORT, parent=None):
        super().__init__(parent)
        self.port = int(port)
        self.hub = FaceHub()
        self.phone = None
        self.quest = None
        self.server = None

    def run(self):
        try:
            try:
                self.phone = PhoneStream()
                self.hub.phone = self.phone
                self.phone.start()
            except OSError as exc:
                self.phone = None
                self.hub.error = 'iPhone video port %s is unavailable: %s' % (VIDEO_PORT, exc)
            self.quest = QuestMonitor(self.hub)
            self.quest.start()
            cert, key = ensure_certificate(
                Path(__file__).resolve().parents[1]/'.setup'/'face_listen', local_ips())
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            self.server = ThreadingHTTPServer(('0.0.0.0', self.port), _handler(self.hub))
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
            self.server.daemon_threads = True
            self.changed.emit(dict(listening=True, port=self.port))
            self.server.serve_forever(poll_interval=0.2)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if self.quest is not None:
                self.quest.stop()
            if self.phone is not None:
                self.phone.stop()
            if self.server:
                self.server.server_close()

    def close(self):
        if self.quest is not None:
            self.quest.stop()
        if self.phone is not None:
            self.phone.stop()
        if self.server:
            self.server.shutdown()
        self.wait(2000)
