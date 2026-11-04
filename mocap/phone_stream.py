"""Full-resolution video from the iPhone app, on the same clock as Quest face frames.

The phone connects to TCP port 8766 and sends length-prefixed HEVC or H.264
access units. Each unit carries a presentation time in microseconds from the
take epoch published by /status. Frames are wrapped in MPEG-TS so ffmpeg can
remux them without re-encoding.
"""
from pathlib import Path
import socket
import struct
import threading
import time

VIDEO_PORT = 8766
_HEADER = b'RGBV'
_MAX_FRAME = 8 * 1024 * 1024


def _crc(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def _pts_bytes(pts):
    pts &= (1 << 33) - 1
    return bytes((
        0x20 | (((pts >> 30) & 7) << 1) | 1,
        (pts >> 22) & 0xFF,
        (((pts >> 15) & 0x7F) << 1) | 1,
        (pts >> 7) & 0xFF,
        ((pts & 0x7F) << 1) | 1,
    ))


class TsMuxer:
    def __init__(self, codec):
        self.stream_type = 0x24 if codec == 'hevc' else 0x1B
        self.cc = {0: 0, 0x1000: 0, 0x0100: 0}
        self._tables = self._pat() + self._pmt()

    def _section_packet(self, pid, section):
        return self._packets(pid, b'\x00' + section, True)

    def _pat(self):
        body = struct.pack('>HH', 1, 0xE000 | 0x1000)
        section = self._table(0x00, struct.pack('>H', 1) + bytes((0xC1, 0x00, 0x00)) + body)
        return self._section_packet(0, section)

    def _pmt(self):
        es = bytes((self.stream_type,)) + struct.pack('>HH', 0xE000 | 0x0100, 0xF000)
        body = struct.pack('>HH', 0xE000 | 0x0100, 0xF000) + es
        section = self._table(0x02, struct.pack('>H', 1) + bytes((0xC1, 0x00, 0x00)) + body)
        return self._section_packet(0x1000, section)

    def _table(self, table_id, after_length):
        payload = after_length
        length = len(payload) + 4
        raw = bytes((table_id, 0xB0 | ((length >> 8) & 0x0F), length & 0xFF)) + payload
        return raw + struct.pack('>I', _crc(raw))

    def _packets(self, pid, data, start):
        out = bytearray()
        offset = 0
        first = start
        while offset < len(data):
            packet = bytearray(188)
            packet[0] = 0x47
            packet[1] = (pid >> 8) & 0x1F
            packet[2] = pid & 0xFF
            if first:
                packet[1] |= 0x40
                first = False
            left = len(data) - offset
            if left < 184:
                stuff = 184 - left
                packet[3] = 0x30 | (self.cc[pid] & 0x0F)
                if stuff == 1:
                    packet[4] = 0
                    packet[5:5 + left] = data[offset:]
                else:
                    packet[4] = stuff - 1
                    packet[5] = 0
                    packet[4 + stuff:4 + stuff + left] = data[offset:]
                offset = len(data)
            else:
                packet[3] = 0x10 | (self.cc[pid] & 0x0F)
                packet[4:188] = data[offset:offset + 184]
                offset += 184
            self.cc[pid] = (self.cc[pid] + 1) & 0x0F
            out += packet
        return bytes(out)

    def _pes(self, data, pts90):
        pes_len = 3 + 5 + len(data)
        if pes_len > 65535:
            pes_len = 0
        return b'\x00\x00\x01\xe0' + struct.pack('>H', pes_len) + b'\x80\x80\x05' + _pts_bytes(pts90) + data

    def tables(self):
        return self._tables

    def access_unit(self, annexb, pts_seconds):
        pts90 = int(max(0.0, pts_seconds) * 90000)
        return self._packets(0x0100, self._pes(annexb, pts90), True)


class PhoneStream(threading.Thread):
    def __init__(self, port=VIDEO_PORT):
        super().__init__(daemon=True)
        self.port = int(port)
        self.lock = threading.Lock()
        self.stopping = False
        self.server = None
        self.client = None
        self.client_address = None
        self.seen = None
        self._file = None
        self._muxer = None
        self._codec = None
        self.frames = 0
        self.first_pts = None
        self.bytes_written = 0
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('0.0.0.0', self.port))
        self.server.listen(1)
        self.server.settimeout(0.4)

    def accepting(self):
        with self.lock:
            return self._file is not None

    def open_take(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open('wb')
        with self.lock:
            self._close_file()
            self._file = handle
            self._muxer = None
            self._codec = None
            self.frames = 0
            self.first_pts = None
            self.bytes_written = 0
        return path

    def close_take(self):
        with self.lock:
            frames, first = self.frames, self.first_pts
            self._close_file()
            return frames, first

    def _close_file(self):
        if self._file is not None:
            self._file.close()
        self._file = None
        self._muxer = None

    def connected(self):
        with self.lock:
            return self.client is not None

    def run(self):
        server = self.server
        try:
            while not self.stopping:
                try:
                    client, address = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._serve(client, address)
        finally:
            server.close()
            self.close_take()

    def stop(self):
        self.stopping = True
        if self.client is not None:
            try:
                self.client.close()
            except OSError:
                pass
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass

    def _serve(self, client, address):
        client.settimeout(2.0)
        with self.lock:
            self.client = client
            self.client_address = address[0]
        try:
            magic = _read(client, 5)
            if magic != _HEADER + b'\x01':
                return
            while not self.stopping:
                size = struct.unpack('>I', _read(client, 4))[0]
                if size == 0 or size > _MAX_FRAME + 10:
                    break
                pts_us, flags, codec = struct.unpack('>QBB', _read(client, 10))
                payload = _read(client, size)
                self._write(codec, flags, pts_us / 1_000_000.0, payload)
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            client.close()
            with self.lock:
                if self.client is client:
                    self.client = None

    def _write(self, codec, flags, pts, payload):
        name = 'hevc' if codec == 1 else 'h264' if codec == 2 else None
        if name is None or not payload:
            return
        with self.lock:
            self.seen = time.monotonic()
            if self._file is None:
                return
            if self._muxer is None:
                self._codec = name
                self._muxer = TsMuxer(name)
                self._file.write(self._muxer.tables())
            if name != self._codec:
                return
            if self.first_pts is None:
                self.first_pts = pts
            blob = self._muxer.access_unit(payload, pts)
            self._file.write(blob)
            self._file.flush()
            self.frames += 1
            self.bytes_written += len(blob)


def _read(sock, size):
    chunks = []
    remaining = size
    while remaining:
        piece = sock.recv(remaining)
        if not piece:
            raise ConnectionError('phone stream closed')
        chunks.append(piece)
        remaining -= len(piece)
    return b''.join(chunks)
