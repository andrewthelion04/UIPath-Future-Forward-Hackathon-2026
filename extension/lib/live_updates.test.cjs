// Testăm politica reală de abonare, cu WebSocket și timer-e controlate.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { stripTypeScriptTypes } = require('node:module');
const source = fs.readFileSync(__dirname + '/api.ts', 'utf8');
const fn = source.slice(source.indexOf('export function subscribeUpdates'), source.indexOf('export class ApiError'));
const js = stripTypeScriptTypes(fn.replace('export function', 'function'));
let nextId = 0;
const timers = new Map();
const later = (fn, delay) => { const id = ++nextId; timers.set(id,{fn,delay}); return id; };
const cancel = id => timers.delete(id);
const flush = delay => { for (const [id,t] of [...timers]) if(t.delay === delay) { timers.delete(id); t.fn(); } };
const sockets = [];
class FakeSocket {
  constructor(url) { this.url=url; this.sent=[]; sockets.push(this); }
  send(text) { this.sent.push(JSON.parse(text)); }
  close() { this.onclose?.(); }
  receive(data) { this.onmessage({data:JSON.stringify(data)}); }
}
const subscribe = new Function('API_URL','API_TOKEN','WebSocket','setTimeout','clearTimeout',
  js+';return subscribeUpdates;')('https://example.test/api','test-token',FakeSocket,later,cancel);
let refreshes=0;
const statuses=[];
const stop=subscribe(()=>refreshes++,s=>statuses.push(s));
assert.equal(sockets[0].url,'wss://example.test/api/ws/updates');
sockets[0].onopen();
assert.deepEqual(sockets[0].sent,[{type:'authenticate',token:'test-token'}]);
sockets[0].receive({type:'snapshot',version:'epoch:1'});
sockets[0].receive({type:'changed',version:'epoch:2'});
sockets[0].receive({type:'changed',version:'epoch:3'});
flush(200);
assert.equal(refreshes,1); // O singură recitire pentru o rafală.
sockets[0].receive({type:'heartbeat',version:'epoch:3'});
flush(200);
assert.equal(refreshes,1);
sockets[0].close();
flush(1000);
assert.equal(sockets.length,2);
sockets[1].onopen();
sockets[1].receive({type:'snapshot',version:'new-epoch:1'});
flush(200);
assert.equal(refreshes,2);
stop();
flush(1000);
assert.equal(sockets.length,2);
assert.equal(timers.size,0);
assert.ok(statuses.includes('Live') && statuses.includes('Reconnecting'));
console.log('WebSocket subscription: auth, batching, reconnect and cleanup passed.');
