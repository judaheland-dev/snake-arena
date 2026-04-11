const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { WebSocketServer } = require('ws');

const BASE_PORT = Number(process.env.PORT) || 3001;
const MAX_PORT_TRIES = 10;
const PUBLIC = __dirname;

const MIME = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

function safeFilePath(urlPath) {
  const requested = urlPath === '/' ? '/games/snake-arena/snake-arena.html' : urlPath;
  const resolved = path.resolve(path.join(PUBLIC, requested));
  if (!resolved.startsWith(path.resolve(PUBLIC))) return null;
  return resolved;
}

const httpServer = http.createServer((req, res) => {
  const filePath = safeFilePath(req.url || '/');
  if (!filePath) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end('Not found');
      return;
    }
    const ext = path.extname(filePath);
    const mime = MIME[ext] || 'application/octet-stream';
    res.writeHead(200, { 'Content-Type': mime });
    res.end(data);
  });
});

const wss = new WebSocketServer({ server: httpServer });
wss.on('error', (err) => {
  const msg = err && err.code ? `${err.code}: ${err.message}` : String(err);
  console.error('WebSocket server error:', msg);
});
let nextId = 1;
const clients = new Map();

wss.on('connection', (ws) => {
  const id = nextId++;
  clients.set(id, { ws, state: null });
  console.log(`Player ${id} connected. Online: ${clients.size}`);

  ws.send(JSON.stringify({ type: 'welcome', id }));

  for (const [cid, c] of clients) {
    if (cid !== id && c.state) {
      ws.send(JSON.stringify({ type: 'peer', id: cid, state: c.state }));
    }
  }

  ws.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    if (msg.type !== 'state') return;

    const segs = Array.isArray(msg.segs)
      ? msg.segs.slice(0, 600).map((s) => ({ x: +s.x || 0, y: +s.y || 0 }))
      : [];

    const state = {
      name: String(msg.name || '').slice(0, 20),
      skinIdx: Math.max(0, +msg.skinIdx || 0),
      segs,
      length: Math.max(1, +msg.length || 1),
      dead: !!msg.dead,
      inSecretMap: !!msg.inSecretMap,
    };

    const self = clients.get(id);
    if (!self) return;
    self.state = state;

    const payload = JSON.stringify({ type: 'peer', id, state });
    for (const [cid, c] of clients) {
      if (cid === id) continue;
      if (c.ws.readyState === 1) c.ws.send(payload);
    }
  });

  ws.on('close', () => {
    clients.delete(id);
    const payload = JSON.stringify({ type: 'leave', id });
    for (const [, c] of clients) {
      if (c.ws.readyState === 1) c.ws.send(payload);
    }
    console.log(`Player ${id} disconnected. Online: ${clients.size}`);
  });

  ws.on('error', () => {
    clients.delete(id);
  });
});

function startServer(port, triesLeft) {
  const onListening = () => {
  const nets = os.networkInterfaces();
  let lanIp = 'localhost';
  for (const name of Object.keys(nets)) {
    for (const net of nets[name] || []) {
      if (net.family === 'IPv4' && !net.internal) {
        lanIp = net.address;
        break;
      }
    }
  }

  console.log('Snake Arena multiplayer server running');
  console.log(`Local:   http://localhost:${port}/games/snake-arena/snake-arena.html`);
  console.log(`Network: http://${lanIp}:${port}/games/snake-arena/snake-arena.html`);
  console.log('Open the Network URL on any device/browser on the same WiFi.');
  };

  const onError = (err) => {
    if (err && err.code === 'EADDRINUSE' && triesLeft > 0) {
      const nextPort = port + 1;
      console.log(`Port ${port} busy, trying ${nextPort}...`);
      setTimeout(() => startServer(nextPort, triesLeft - 1), 120);
      return;
    }
    console.error('Failed to start server:', err && err.message ? err.message : err);
    process.exit(1);
  };

  httpServer.once('error', onError);
  httpServer.listen(port, '0.0.0.0', () => {
    httpServer.off('error', onError);
    onListening();
  });
}

startServer(BASE_PORT, MAX_PORT_TRIES);
