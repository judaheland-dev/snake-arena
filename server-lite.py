#!/usr/bin/env python3
import json
import mimetypes
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('PORT', '3010'))
HOST = '0.0.0.0'
STALE_SECONDS = 12

state_lock = threading.Lock()
next_id = 1
next_seq = 0
clients = {}
# id -> {token:str, state:dict|None, last_seen:float}
# token -> id map for auth lookup
by_token = {}
events = []


def _json_bytes(payload):
    return json.dumps(payload, separators=(',', ':')).encode('utf-8')


def _add_event(ev):
    global next_seq
    next_seq += 1
    rec = {'seq': next_seq, **ev}
    events.append(rec)
    if len(events) > 4000:
        del events[:2000]


def _cleanup_stale(now=None):
    if now is None:
        now = time.time()
    stale_ids = []
    for cid, info in list(clients.items()):
      if now - info['last_seen'] > STALE_SECONDS:
          stale_ids.append(cid)
    for cid in stale_ids:
        info = clients.pop(cid, None)
        if info:
            by_token.pop(info['token'], None)
            _add_event({'type': 'leave', 'id': cid})


def _safe_join(url_path):
    if url_path == '/':
        url_path = '/games/snake-arena/snake-arena.html'
    rel = unquote(url_path.lstrip('/'))
    full = os.path.abspath(os.path.join(BASE_DIR, rel))
    if not full.startswith(BASE_DIR):
        return None
    return full


def _sanitize_state(msg):
    segs = msg.get('segs') if isinstance(msg.get('segs'), list) else []
    out_segs = []
    for s in segs[:600]:
        if isinstance(s, dict):
            out_segs.append({'x': float(s.get('x', 0) or 0), 'y': float(s.get('y', 0) or 0)})
    return {
        'name': str(msg.get('name', ''))[:20],
        'skinIdx': max(0, int(msg.get('skinIdx', 0) or 0)),
        'segs': out_segs,
        'length': max(1, float(msg.get('length', 1) or 1)),
        'dead': bool(msg.get('dead', False)),
        'inSecretMap': bool(msg.get('inSecretMap', False)),
    }


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, code=200):
        data = _json_bytes(payload)
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return {}
        raw = self.rfile.read(length) if length > 0 else b'{}'
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/events':
            q = parse_qs(parsed.query)
            try:
                after = int((q.get('after') or ['0'])[0])
            except ValueError:
                after = 0
            token = (q.get('token') or [''])[0]
            with state_lock:
                _cleanup_stale()
                cid = by_token.get(token)
                out = []
                for ev in events:
                    if ev['seq'] <= after:
                        continue
                    if cid is not None and ev.get('id') == cid and ev.get('type') == 'peer':
                        continue
                    out.append(ev)
                seq = next_seq
            self._send_json({'events': out, 'seq': seq})
            return

        full = _safe_join(parsed.path)
        if not full or not os.path.exists(full) or not os.path.isfile(full):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')
            return

        ctype, _ = mimetypes.guess_type(full)
        ctype = ctype or 'application/octet-stream'
        with open(full, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        body = self._read_json()

        if parsed.path == '/api/join':
            global next_id
            with state_lock:
                _cleanup_stale()
                cid = next_id
                next_id += 1
                token = secrets.token_hex(16)
                clients[cid] = {'token': token, 'state': None, 'last_seen': time.time()}
                by_token[token] = cid
                peers = []
                for pid, info in clients.items():
                    if pid == cid:
                        continue
                    if info['state']:
                        peers.append({'id': pid, 'state': info['state']})
                seq = next_seq
            print(f'Player {cid} connected. Online: {len(clients)}')
            self._send_json({'id': cid, 'token': token, 'peers': peers, 'seq': seq})
            return

        if parsed.path == '/api/state':
            token = str(body.get('token', ''))
            st = _sanitize_state(body.get('state', {})) if isinstance(body.get('state'), dict) else _sanitize_state(body)
            with state_lock:
                _cleanup_stale()
                cid = by_token.get(token)
                if cid is None or cid not in clients:
                    self._send_json({'ok': False, 'error': 'unauthorized'}, 401)
                    return
                clients[cid]['state'] = st
                clients[cid]['last_seen'] = time.time()
                _add_event({'type': 'peer', 'id': cid, 'state': st})
                seq = next_seq
            self._send_json({'ok': True, 'seq': seq})
            return

        if parsed.path == '/api/leave':
            token = str(body.get('token', ''))
            with state_lock:
                _cleanup_stale()
                cid = by_token.pop(token, None)
                if cid is not None:
                    clients.pop(cid, None)
                    _add_event({'type': 'leave', 'id': cid})
                    print(f'Player {cid} disconnected. Online: {len(clients)}')
            self._send_json({'ok': True})
            return

        self._send_json({'ok': False, 'error': 'not found'}, 404)

    def log_message(self, fmt, *args):
        return


def _lan_ip():
    ip = 'localhost'
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    return ip


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    lan = _lan_ip()
    print('Snake Arena multiplayer (Python fallback) running')
    print(f'Local:   http://localhost:{PORT}/games/snake-arena/snake-arena.html')
    print(f'Network: http://{lan}:{PORT}/games/snake-arena/snake-arena.html')
    print('Open the Network URL on both devices.')
    server.serve_forever()


if __name__ == '__main__':
    main()
