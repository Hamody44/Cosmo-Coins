const root = document.body;
const tournamentId = root.dataset.tournament;
const clientId = root.dataset.clientId;
let authToken = root.dataset.initialToken || "";
let socket = null;
let identity = null;
let state = null;
let currentMatch = null;

const statusEl = document.querySelector("#connection-status");
const identityEl = document.querySelector("#identity-label");
const helpEl = document.querySelector("#identity-help");
const connectButton = document.querySelector("#connect-button");
const overlay = document.querySelector("#table-overlay");
const shootButton = document.querySelector("#shoot-button");
const power = document.querySelector("#power");
const powerValue = document.querySelector("#power-value");
const canvas = document.querySelector("#pool-table");
const ctx = canvas.getContext("2d");
const W = canvas.width, H = canvas.height;
const table = { x: 48, y: 42, w: W - 96, h: H - 84, r: 28 };
const pockets = [
  [table.x, table.y], [W / 2, table.y - 2], [table.x + table.w, table.y],
  [table.x, table.y + table.h], [W / 2, table.y + table.h + 2], [table.x + table.w, table.y + table.h],
];
let cue = { x: table.x + table.w * .28, y: table.y + table.h / 2, vx: 0, vy: 0, color: "#f5f5e8", number: 0, pocketed: false };
let balls = [];
let aim = { x: cue.x + 120, y: cue.y };

function toast(message) {
  const el = document.querySelector("#toast");
  el.textContent = message; el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
}
function setStatus(text, connected = false) { statusEl.textContent = text; statusEl.classList.toggle("connected", connected); }
function wsUrl() { return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/game-ws`; }
function send(message) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message)); }

function initRack() {
  const palette = ["#f2d35b", "#4e78c7", "#d75a54", "#8d59b8", "#e9a13a", "#4da77e", "#ad4d58", "#161b1a", "#f0d66b", "#608cd3", "#cf6159", "#9761bd", "#e8a845", "#54aa83", "#bd5862"];
  const startX = table.x + table.w * .68, startY = table.y + table.h / 2, r = 13;
  balls = [];
  let n = 1;
  for (let row = 0; row < 5; row++) {
    for (let col = 0; col <= row; col++) {
      balls.push({ x: startX + row * r * 1.75, y: startY + (col - row / 2) * r * 2.1, vx: 0, vy: 0, color: palette[n - 1], number: n++, pocketed: false });
    }
  }
  cue = { x: table.x + table.w * .28, y: startY, vx: 0, vy: 0, color: "#f5f5e8", number: 0, pocketed: false };
}
function drawTable() {
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#070b09"; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = "#5a321e"; roundRect(table.x - 17, table.y - 17, table.w + 34, table.h + 34, 33); ctx.fill();
  ctx.fillStyle = "#125c45"; roundRect(table.x, table.y, table.w, table.h, table.r); ctx.fill();
  ctx.strokeStyle = "#218264"; ctx.lineWidth = 3; ctx.stroke();
  for (const [x, y] of pockets) { ctx.beginPath(); ctx.arc(x, y, 21, 0, Math.PI * 2); ctx.fillStyle = "#050706"; ctx.fill(); ctx.strokeStyle = "#9d5d30"; ctx.lineWidth = 4; ctx.stroke(); }
  const all = [cue, ...balls];
  all.filter(b => !b.pocketed).forEach(drawBall);
  if (!cue.pocketed && canPlay()) {
    ctx.beginPath(); ctx.moveTo(cue.x, cue.y); ctx.lineTo(aim.x, aim.y); ctx.strokeStyle = "#e9f6d588"; ctx.setLineDash([9, 9]); ctx.lineWidth = 2; ctx.stroke(); ctx.setLineDash([]);
  }
}
function roundRect(x, y, w, h, r) { ctx.beginPath(); ctx.roundRect(x, y, w, h, r); }
function drawBall(b) {
  const g = ctx.createRadialGradient(b.x - 4, b.y - 5, 2, b.x, b.y, 15);
  g.addColorStop(0, "#ffffff"); g.addColorStop(.2, b.color); g.addColorStop(1, "#101513");
  ctx.beginPath(); ctx.arc(b.x, b.y, 13, 0, Math.PI * 2); ctx.fillStyle = g; ctx.fill();
  ctx.strokeStyle = "#ffffff55"; ctx.stroke();
  if (b.number) { ctx.beginPath(); ctx.arc(b.x, b.y, 5, 0, Math.PI * 2); ctx.fillStyle = "#f4f5e9"; ctx.fill(); ctx.fillStyle = "#15201b"; ctx.font = "7px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(b.number, b.x, b.y); }
}
function canPlay() { return Boolean(identity && currentMatch && currentMatch.players.some(p => p.user_id === identity.user_id) && currentMatch.status === "ready"); }
function physics() {
  const all = [cue, ...balls];
  all.forEach(b => { if (b.pocketed) return; b.x += b.vx; b.y += b.vy; b.vx *= .985; b.vy *= .985; if (Math.hypot(b.vx,b.vy) < .03) b.vx = b.vy = 0; if (b.x < table.x + 13 || b.x > table.x + table.w - 13) b.vx *= -1; if (b.y < table.y + 13 || b.y > table.y + table.h - 13) b.vy *= -1; b.x = Math.max(table.x + 13, Math.min(table.x + table.w - 13, b.x)); b.y = Math.max(table.y + 13, Math.min(table.y + table.h - 13, b.y)); });
  for (let i=0;i<all.length;i++) for (let j=i+1;j<all.length;j++) { const a=all[i], b=all[j]; if(a.pocketed||b.pocketed) continue; const dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy); if(d && d<26){const nx=dx/d,ny=dy/d; const p=(a.vx-b.vx)*nx+(a.vy-b.vy)*ny; if(p>0){a.vx-=p*nx;a.vy-=p*ny;b.vx+=p*nx;b.vy+=p*ny;} const push=(26-d)/2;a.x-=nx*push;a.y-=ny*push;b.x+=nx*push;b.y+=ny*push;}}
  all.forEach(b => { if (pockets.some(([x,y]) => Math.hypot(b.x-x,b.y-y)<19)) b.pocketed=true; });
  drawTable(); requestAnimationFrame(physics);
}
canvas.addEventListener("pointermove", event => { const r=canvas.getBoundingClientRect(); aim.x=(event.clientX-r.left)*W/r.width; aim.y=(event.clientY-r.top)*H/r.height; });
shootButton.addEventListener("click", () => { if (!canPlay() || cue.vx || cue.vy) return; const dx=cue.x-aim.x,dy=cue.y-aim.y,d=Math.hypot(dx,dy)||1,s=Number(power.value)/13; cue.vx=dx/d*s; cue.vy=dy/d*s; toast("تم تنفيذ الضربة محليًا؛ اعتماد النتيجة يتم من خادم المباراة."); });
power.addEventListener("input", () => powerValue.textContent = `${power.value}%`);

function renderState(next) {
  state = next;
  document.querySelector("#tournament-label").textContent = next.tournament_id || "—";
  document.querySelector("#player-count").textContent = `${next.player_count} / ${next.max_players}`;
  document.querySelector("#tournament-status").textContent = next.status === "registration" ? `تسجيل ${next.registration_remaining}s` : next.status;
  document.querySelector("#round-label").textContent = next.round ? `الجولة ${next.round}` : "قبل البداية";
  const list = document.querySelector("#match-list"); list.innerHTML = "";
  (next.matches || []).forEach(match => { const el=document.createElement("div"); el.className=`match ${match.status === "ready" ? "active" : ""}`; el.innerHTML=`<div class="match-title"><span>Match ${match.slot}</span><span>${match.status}</span></div>${match.players.map(p=>`<div class="match-player ${p.user_id===match.winner_id?'winner':''}"><span>${escapeHtml(p.display_name)}</span><span>${p.user_id===identity?.user_id?'YOU':''}</span></div>`).join("")}`; list.appendChild(el); });
  if (!next.matches?.length) list.innerHTML = '<p class="muted">ستظهر المجموعات بعد إغلاق التسجيل.</p>';
  currentMatch = next.matches?.find(m => m.players.some(p => p.user_id === identity?.user_id)) || null;
  overlay.classList.toggle("hidden", canPlay()); shootButton.disabled = !canPlay();
}
function escapeHtml(v) { return String(v).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }

async function discordAuthorize() {
  if (authToken) return;
  try {
    const { DiscordSDK } = await import("https://cdn.jsdelivr.net/npm/@discord/embedded-app-sdk@2/+esm");
    const sdk = new DiscordSDK(clientId); await sdk.ready();
    const { code } = await sdk.commands.authorize({ client_id: clientId, response_type: "code", state: crypto.randomUUID(), scope: ["identify"] });
    const response = await fetch("/game-auth/discord", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ code, redirect_uri: location.origin + "/game" }) });
    if (!response.ok) throw new Error("Discord authorization failed");
    authToken = (await response.json()).token;
  } catch (error) { toast("افتح اللعبة داخل Discord لإتمام التوثيق."); throw error; }
}
function connect() {
  socket = new WebSocket(wsUrl()); setStatus("جارٍ الاتصال…");
  socket.onopen = async () => { try { await discordAuthorize(); send({type:"auth",token:authToken}); } catch { socket.close(); } };
  socket.onmessage = event => { const message=JSON.parse(event.data); if(message.type==="hello") return; if(message.type==="authenticated"){identity=message.user;identityEl.textContent=identity.display_name;helpEl.textContent=`Discord ID: ${identity.user_id}`;setStatus("موثق ومتصل",true);connectButton.disabled=true;send({type:"join_tournament",tournament_id:tournamentId});} else if(message.type==="tournament_state") renderState(message.state); else if(message.type==="error") toast(message.message || "تعذر تنفيذ العملية"); };
  socket.onclose = () => { setStatus("انقطع الاتصال"); connectButton.disabled=false; };
}
connectButton.addEventListener("click", connect);
initRack(); physics();
if (authToken) connect();
