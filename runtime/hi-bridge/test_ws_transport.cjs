// Local fake Hi endpoint: no real credentials, messages, or external network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const readline = require('node:readline');
const {WebSocketServer} = require('/usr/local/lib/node_modules/@xhs/claude-hi/node_modules/ws');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(),'hi-ws-test-'));
const server = http.createServer((req,res) => {
  res.setHeader('Content-Type','application/json');
  res.end(JSON.stringify({data:{appAccessToken:'local-test-token'}}));
});
const wss = new WebSocketServer({server});
let child, socket, acks=0, gotEvent=false, gotPong=false;
const timer=setTimeout(() => done(new Error('transport test timed out')),10000);
let finished=false;
function done(error) {
  if(finished) return; finished=true;
  clearTimeout(timer);
  child?.kill(); socket?.terminate();
  wss.close(); server.close();
  fs.rmSync(tmp,{recursive:true,force:true});
  if(error){console.error(error.message);process.exitCode=1;}
  else console.log('PASS: raw integer preservation, explicit ACK, heartbeat, stdin shutdown');
}
wss.on('connection', ws => {
  socket=ws;
  ws.send(JSON.stringify({type:'CONNECTED'}));
  ws.send('{"type":"EVENT","eventId":"e1","eventType":"asn_bot_callback_msg","payload":{"seq":9007199254740993}}');
  ws.on('message',raw => {
    const obj=JSON.parse(String(raw));
    if(obj.type==='PING') ws.send(JSON.stringify({type:'PONG'}));
    if(obj.type==='ACK_EVENT') {
      try { assert.equal(obj.eventId,'e1'); acks++; } catch(e){done(e);}
    }
  });
});
server.listen(0,'127.0.0.1',() => {
  const file=path.join(tmp,'config.json');
  fs.writeFileSync(file,JSON.stringify({hi:{appId:'fake',appSecret:'fake',baseUrl:`http://127.0.0.1:${server.address().port}`}}));
  child=spawn(process.execPath,[path.join(__dirname,'ws-transport.cjs')],{env:{...process.env,HI_WS_CONFIG:file},stdio:['pipe','pipe','pipe']});
  readline.createInterface({input:child.stdout}).on('line',line=>{
    try {
      const obj=JSON.parse(line);
      if(obj.kind==='event') {
        gotEvent=true;
        assert.ok(obj.raw.includes('9007199254740993'));
        setTimeout(()=>{
          try {
            assert.equal(acks,0,'transport must not ACK on its own');
            child.stdin.write(JSON.stringify({type:'ACK_EVENT',eventId:'e1',eventType:'asn_bot_callback_msg'})+'\n');
          } catch(e){done(e);}
        },100);
      }
      if(obj.kind==='pong') {gotPong=true; child.stdin.end();}
    } catch(e){done(e);}
  });
  child.on('exit',code=>{
    if(finished) return;
    try {assert.equal(code,0);assert.ok(gotEvent);assert.ok(gotPong);assert.equal(acks,1);done();}
    catch(e){done(e);}
  });
});
