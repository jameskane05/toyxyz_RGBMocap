"""Answer the Quest Face Mocap app. It broadcasts FM1, then streams one JSON face frame per line."""
import json
import select
import socket
import threading

DISCOVERY_PORT = 45454
STREAM_PORT = 45455


class QuestMonitor(threading.Thread):
    def __init__(self, hub):
        super().__init__(daemon=True, name='quest-monitor')
        self.hub = hub
        self._halt = threading.Event()
        self._armed = False
        self.udp = None
        self.tcp = None

    def stop(self):
        self._halt.set()
        for sock in (self.udp, self.tcp):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass

    def run(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            udp.bind(('0.0.0.0', DISCOVERY_PORT))
        except OSError as exc:
            self.hub.error = 'Quest discovery port %s is unavailable: %s' % (DISCOVERY_PORT, exc)
            udp.close()
            return
        udp.setblocking(False)
        self.udp = udp
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            tcp.bind(('0.0.0.0', STREAM_PORT))
            tcp.listen(1)
        except OSError as exc:
            self.hub.error = 'Quest stream port %s is unavailable: %s' % (STREAM_PORT, exc)
            tcp.close()
            udp.close()
            return
        tcp.setblocking(False)
        self.tcp = tcp
        client = None
        pending = b''
        while not self._halt.is_set():
            watch = [udp, tcp] if client is None else [udp, client]
            try:
                ready, _, _ = select.select(watch, [], [], 0.4)
            except (OSError, ValueError):
                break
            if udp in ready:
                while True:
                    try:
                        data, addr = udp.recvfrom(64)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError:
                        data = None
                        break
                    if data and data.startswith(b'FM1'):
                        try:
                            udp.sendto(b'FM1 %d' % STREAM_PORT, addr)
                        except OSError:
                            pass
            if client is not None:
                try:
                    self.hub.hello('quest', client.getpeername()[0])
                except OSError:
                    pass
            if client is None:
                if tcp not in ready:
                    continue
                try:
                    client, remote = tcp.accept()
                except (BlockingIOError, OSError):
                    continue
                client.setblocking(False)
                pending = b''
                self.hub.hello('quest', remote[0])
                continue
            if client not in ready:
                continue
            try:
                chunk = client.recv(65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                chunk = b''
            if not chunk:
                try:
                    client.close()
                except OSError:
                    pass
                client = None
                continue
            pending += chunk
            if len(pending) > 1024 * 1024:
                pending = pending[-65536:]
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                self._frame(line, client)

    def _frame(self, line, client):
        try:
            payload = json.loads(line.decode('utf-8'))
        except (UnicodeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        weights = payload.get('w')
        if not isinstance(weights, dict):
            return
        address = None
        try:
            address = client.getpeername()[0]
        except OSError:
            pass
        recording = bool(payload.get('rec'))
        if recording and not self._armed and not self.hub.recording:
            self.hub.start()
            self._armed = True
        elif not recording and self._armed:
            self.hub.stop()
            self._armed = False
        try:
            self.hub.ingest(dict(blendshapes=weights), client=address)
        except (TypeError, ValueError) as exc:
            self.hub.error = str(exc)
