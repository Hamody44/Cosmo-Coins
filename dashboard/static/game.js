const root = document.body;
const tournamentId = root.dataset.tournament;
const matchId = root.dataset.match;
const spectator = root.dataset.spectator === "1";
let authToken = root.dataset.ticket || root.dataset.initialToken || "";
let socket = null, identity = null, state = null, currentMatch = null, poolState = null;
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
const pockets = [[0,0],[.5,0],[1,0],[0,1],[.5,1],[1,1]];
let aim = { x: .5, y: .5 };
const colors = ["#f2d35b","#4e78c7","#d75a54","#8d59b8","#e9a13a","#4da77e","#ad4d58","#161b1a","#f0d66b","#608cd3","#cf6159","#9761bd","#e8a845","#54aa83","#bd5862"];

function toast(message) { const el=document.querySelector("#toast"); el.textContent=message; el.classList.add("show"); setTimeout(()=>el.classList.remove("show"),2600); }
function setStatus(text, connected=false) { statusEl.textContent=text; statusEl.classList.toggle("connected",connected); }
function wsUrl() { return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/game-ws`; }
function send(message) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message)); }
function canPlay() { return Boolean(identity && currentMatch?.status === "ready" && poolState && !poolState.moving && poolState.turn_user_id === identity.user_id); }
function toPixel(ball) { return { x: table.x + ball.x * table.w, y: table.y + ball.y * table.h }; }
function drawBall(ball) {
  const p=toPixel(ball), radius=Math.max(11, Math.min(20, table.w*.026));
  const g=ctx.createRadialGradient(p.x-radius*.35,p.y-radius*.4,2,p.x,p.y,radius);
  g.addColorStop(0,"#ffffff"); g.addColorStop(.18,ball.number===0?"#f5f5e8":colors[(ball.number-1)%colors.length]); g.addColorStop(1,"#101513");
  ctx.beginPath(); ctx.arc(p.x,p.y,radius,0,Math.PI*2); ctx.fillStyle=g; ctx.fill(); ctx.strokeStyle="#ffffff55"; ctx.stroke();
  if(ball.number){ctx.beginPath();ctx.arc(p.x,p.y,radius*.38,0,Math.PI*2);ctx.fillStyle="#f4f5e9";ctx.fill();ctx.fillStyle="#15201b";ctx.font=`${Math.max(8,radius*.55)}px sans-serif`;ctx.textAlign="center";ctx.textBaseline="middle";ctx.fillText(ball.number,p.x,p.y);}
}
function fallbackBalls(){const balls=[{number:0,x:.25,y:.5,pocketed:false}];let n=1;for(let row=0;row<5;row++)for(let col=0;col<=row;col++)balls.push({number:n++,x:.68+row*.047,y:.5+(col-row/2)*.053,pocketed:false});return balls;}
function drawTable(){
  ctx.clearRect(0,0,W,H); ctx.fillStyle="#070b09"; ctx.fillRect(0,0,W,H);
  ctx.fillStyle="#5a321e"; roundRect(table.x-17,table.y-17,table.w+34,table.h+34,33);ctx.fill();
  ctx.fillStyle="#125c45";roundRect(table.x,table.y,table.w,table.h,table.r);ctx.fill();ctx.strokeStyle="#319d70";ctx.lineWidth=3;ctx.stroke();
  pockets.forEach(([x,y])=>{const p={x:table.x+x*table.w,y:table.y+y*table.h};ctx.beginPath();ctx.arc(p.x,p.y,23,0,Math.PI*2);ctx.fillStyle="#050706";ctx.fill();ctx.strokeStyle="#b66c36";ctx.lineWidth=4;ctx.stroke();});
  (poolState?.balls || fallbackBalls()).filter(b=>!b.pocketed).forEach(drawBall);
  if(canPlay()){const cue=poolState.balls.find(b=>b.number===0);if(cue)drawCueStick(cue);}
}
function drawCueStick(cue){
  const p=toPixel(cue), target={x:table.x+aim.x*table.w,y:table.y+aim.y*table.h};
  const angle=Math.atan2(target.y-p.y,target.x-p.x), length=Math.min(320,table.w*.34);
  ctx.save();ctx.translate(p.x,p.y);ctx.rotate(angle);
  ctx.shadowColor="#000b";ctx.shadowBlur=8;
  ctx.fillStyle="#e3b26a";ctx.fillRect(-length-18,-3,length,6);
  ctx.fillStyle="#fff0ba";ctx.fillRect(-length+20,-2,length*.64,4);
  ctx.fillStyle="#512b18";ctx.fillRect(-length-30,-7,28,14);
  ctx.fillStyle="#d9d9c4";ctx.fillRect(-length+18,-2,7,4);ctx.restore();
}
function roundRect(x,y,w,h,r){ctx.beginPath();ctx.roundRect(x,y,w,h,r);}
function renderState(next){
  state=next;document.querySelector("#tournament-label").textContent=next.tournament_id||"—";document.querySelector("#player-count").textContent=`${next.player_count} / ${next.max_players}`;
  document.querySelector("#tournament-status").textContent=next.status==="registration"?`تسجيل ${next.registration_remaining}s`:next.status;document.querySelector("#round-label").textContent=next.round?`الجولة ${next.round}`:"قبل البداية";
  const list=document.querySelector("#match-list");list.innerHTML="";(next.matches||[]).forEach(match=>{const el=document.createElement("div");el.className=`match ${match.status==="ready"?"active":""}`;el.innerHTML=`<div class="match-title"><span>Match ${match.slot}</span><span>${match.status}</span></div>${match.players.map(p=>`<div class="match-player ${p.user_id===match.winner_id?'winner':''}"><span>${escapeHtml(p.display_name)}</span><span>${p.user_id===identity?.user_id?'YOU':''}</span></div>`).join("")}`;list.appendChild(el);});
  if(!next.matches?.length)list.innerHTML='<p class="muted">ستظهر المباريات بعد إغلاق التسجيل.</p>';
  currentMatch=spectator?(next.matches||[]).find(m=>m.match_id===matchId)||null:(next.matches||[]).find(m=>m.players.some(p=>p.user_id===identity?.user_id))||null;
  updateHud();
  if(spectator){overlay.textContent="وضع المشاهدة المباشرة";overlay.classList.add("hidden");}
  else if(canPlay()){overlay.textContent=poolState?.moving?"الكرات تتحرك…":"دورك — صوب واضرب";overlay.classList.toggle("hidden",!poolState?.moving);}
  else if(currentMatch?.status==="waiting_entry"){overlay.textContent=`بانتظار اللاعب الآخر… ${currentMatch.entry_remaining||0} ث`;overlay.classList.remove("hidden");}
  else if(currentMatch?.status==="ready" && poolState){overlay.textContent="دور الخصم — انتظر دوره";overlay.classList.remove("hidden");}
  else{overlay.textContent="تهيئة المباراة…";overlay.classList.remove("hidden");}
  shootButton.disabled=!canPlay();
}
function updateHud(){
  const players=currentMatch?.players||[];
  document.querySelector("#player-one-name").textContent=players[0]?.display_name||"—";
  document.querySelector("#player-two-name").textContent=players[1]?.display_name||"—";
  const turn=poolState?.turn_user_id;
  document.querySelector("#player-one-turn").textContent=turn===players[0]?.user_id?"دوره":"";
  document.querySelector("#player-two-turn").textContent=turn===players[1]?.user_id?"دوره":"";
  document.querySelector("#turn-label").textContent=poolState?.moving?"الكرات تتحرك":turn===identity?.user_id?"دورك":"دور الخصم";
  document.querySelector("#turn-timer").textContent=String(poolState?.turn_remaining??30);
}
function escapeHtml(v){return String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function connect(){
  socket=new WebSocket(wsUrl());setStatus("جارٍ الاتصال…");
  socket.onopen=()=>{if(authToken)send({type:"auth",token:authToken});else if(spectator)send({type:"watch_match",tournament_id:tournamentId,match_id:matchId});else{toast("افتح اللعبة من زر مباراة Discord.");socket.close();}};
  socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.type==="hello")return;if(message.type==="authenticated"){identity=message.user;identityEl.textContent=identity.display_name;helpEl.textContent=`Discord ID: ${identity.user_id}`;setStatus("متصل",true);connectButton.disabled=true;send({type:"enter_match",tournament_id:tournamentId,match_id:matchId});}else if(message.type==="tournament_state"){renderState(message.state);if(spectator){identityEl.textContent="مشاهد";helpEl.textContent="المشاهدة فقط — لا يمكنك التحكم بالكرات.";setStatus("مشاهدة مباشرة",true);connectButton.disabled=true;}}else if(message.type==="pool_state"){poolState=message.state;drawTable();updateHud();if(state)renderState(state);}else if(message.type==="error")toast(message.message||"تعذر تنفيذ العملية");};
  socket.onclose=()=>{setStatus("انقطع الاتصال");if(!spectator)connectButton.disabled=false;};
}
canvas.addEventListener("pointermove",event=>{const r=canvas.getBoundingClientRect();aim.x=Math.max(0,Math.min(1,(event.clientX-r.left)/r.width));aim.y=Math.max(0,Math.min(1,(event.clientY-r.top)/r.height));drawTable();});
shootButton.addEventListener("click",()=>{if(!canPlay())return;const cue=poolState.balls.find(b=>b.number===0);const angle=Math.atan2(aim.y-cue.y,aim.x-cue.x);send({type:"shoot",angle,power:Number(power.value)/100});});
power.addEventListener("input",()=>powerValue.textContent=`${power.value}%`);connectButton.addEventListener("click",connect);
function frame(){drawTable();requestAnimationFrame(frame);}frame();
if(authToken||spectator)connect();
