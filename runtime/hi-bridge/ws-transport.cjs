// Transport only: raw event JSON goes to Python; ACKs only come from Python
// after SQLite commit. Do not run the old claude-hi task executor.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const WebSocket = require('ws');
const emit = obj => process.stdout.write(JSON.stringify(obj) + '\n');
let ws, heartbeat;
let finished = false;
function finish(code) {
  if (finished) return;
  finished = true;
  clearInterval(heartbeat);
  if (ws) ws.terminate();
  process.exit(code);
}
process.on('SIGTERM', () => finish(0));
process.on('SIGINT', () => finish(0));
process.stdin.on('end', () => finish(0));
process.stdout.on('error', () => finish(1));
readline.createInterface({input: process.stdin}).on('line', line => {
  try {
    const ack = JSON.parse(line);
    if (ack.type === 'ACK_EVENT' && typeof ack.eventId === 'string' && ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(ack));
    }
  } catch { emit({kind:'error', code:'INVALID_ACK'}); }
});
(async () => {
  const file = process.env.HI_WS_CONFIG || path.join(os.homedir(), '.config/agent-workspace/credentials.json');
  const c = JSON.parse(fs.readFileSync(file, 'utf8')).hi;
  const response = await fetch(c.baseUrl + '/openapis/open/token/createAppAccessToken/v2', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({appId:c.appId, appSecret:c.appSecret}),
    signal:AbortSignal.timeout(15000),
  });
  const body = await response.json();
  const token = body.data?.appAccessToken ?? body.appAccessToken;
  if (!response.ok || !token) throw new Error('TOKEN_FAILED');
  const url = c.baseUrl.replace(/^http/, 'ws') + '/event/ws/endpoint?' +
    new URLSearchParams({appId:c.appId, appAccessToken:token});
  ws = new WebSocket(url, {handshakeTimeout:10000, maxPayload:2 * 1024 * 1024});
  let lastPong = Date.now();
  ws.on('open', () => {
    lastPong = Date.now();
    heartbeat = setInterval(() => {
      if (Date.now() - lastPong > 15000) {
        emit({kind:'error', code:'HEARTBEAT_TIMEOUT'}); finish(1); return;
      }
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'PING'}));
    }, 5000);
  });
  ws.on('message', raw => {
    try {
      const packet = JSON.parse(String(raw));
      if (packet.type === 'CONNECTED') emit({kind:'connected'});
      else if (packet.type === 'PONG') {lastPong = Date.now(); emit({kind:'pong'});}
      // Forward the original JSON, not JS-reencoded payload: preserve long integers.
      else if (packet.type === 'EVENT') emit({kind:'event', raw:String(raw)});
    } catch {emit({kind:'error', code:'INVALID_ENVELOPE'});}
  });
  ws.on('error', () => {emit({kind:'error', code:'WEBSOCKET_ERROR'}); finish(1);});
  ws.on('close', code => {emit({kind:'closed', code}); finish(1);});
})().catch(() => {emit({kind:'error', code:'CONNECT_FAILED'}); finish(1);});
