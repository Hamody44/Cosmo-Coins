const root = document.body;
const tournamentId = root.dataset.tournament || "";
const matchId = root.dataset.match || "";
const spectator = root.dataset.spectator === "1";
const authToken = root.dataset.ticket || root.dataset.initialToken || "";

const el = (selector) => document.querySelector(selector);
const canvas = el("#pool-table");
const ctx = canvas.getContext("2d");
const statusEl = el("#connection-status");
const connectButton = el("#connect-button");
const reconnectButton = el("#reconnect-button");
const overlay = el("#table-overlay");
const shootButton = el("#shoot-button");
const powerInput = el("#power");
const powerOutput = el("#power-value");
const stage = el("#table-stage");
const resultBanner = el("#result-banner");
const tournamentPanel = el("#tournament-panel");
const WIDTH = canvas.width;
const HEIGHT = canvas.height;
const TABLE_ASPECT = 2.0;
const BALL_RADIUS = .0235;
const table = { x: 92, y: 46, w: WIDTH - 184, h: HEIGHT - 92, r: 25 };
const ballColors = ["#f1cf49", "#4474c6", "#d9534f", "#8051b2", "#e79635", "#42976d", "#b34452", "#171918", "#e9cd47", "#4a79cb", "#dc5c52", "#8956b8", "#e99a3d", "#4da478", "#bf4c58"];
const phaseNames = { break: "ضربة البداية", open: "طاولة مفتوحة", playing: "اللعب مستمر", finished: "انتهت المباراة" };
const groupNames = { solids: "الكرات الملوّنة", stripes: "الكرات المخططة" };
let socket = null;
let identity = null;
let tournamentState = null;
let currentMatch = null;
let poolState = null;
let aim = { x: .73, y: .5 };
let drag = null;
let placementPreview = null;
let toastTimer = 0;
let lastSeenShot = null;
let autoReconnectTimer = 0;
let reconnectDelay = 1400;
let deliberateClose = false;

function latinDigits(value) { return String(value).replace(/\d/g, digit => "٠١٢٣٤٥٦٧٨٩"[digit]); }
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}
function toast(message) {
  const box = el("#toast");
  box.textContent = message;
  box.classList.add("show");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => box.classList.remove("show"), 3000);
}
function setConnection(label, mode = "") {
  statusEl.querySelector("span").textContent = label;
  statusEl.classList.toggle("connected", mode === "connected");
  statusEl.classList.toggle("error", mode === "error");
}
function socketUrl() { return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/game-ws`; }
function send(message) {
  if (socket?.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(message));
  return true;
}
function playerId(player) { return player?.user_id == null ? null : String(player.user_id); }
function isMyTurn() { return Boolean(identity && poolState?.turn_user_id != null && String(poolState.turn_user_id) === String(identity.user_id)); }
function canPlay() {
  return Boolean(socket?.readyState === WebSocket.OPEN && identity && !spectator && currentMatch?.status === "ready" && poolState && !poolState.moving && poolState.phase !== "finished" && isMyTurn());
}
function ballInHand() { return Boolean(poolState?.ball_in_hand && canPlay()); }
function localPoint(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  return { x: (clientX - rect.left) / rect.width * WIDTH, y: (clientY - rect.top) / rect.height * HEIGHT };
}
function clothPoint(point) {
  return { x: Math.max(.035, Math.min(.965, (point.x - table.x) / table.w)), y: Math.max(.035, Math.min(.965, (point.y - table.y) / table.h)) };
}
function pixelPoint(ball) { return { x: table.x + Number(ball.x || 0) * table.w, y: table.y + Number(ball.y || 0) * table.h }; }
function cueBall() { return poolState?.balls?.find(ball => Number(ball.number) === 0 && !ball.pocketed) || null; }
function drawRoundRect(x, y, width, height, radius) {
  ctx.beginPath();
  ctx.roundRect(x, y, width, height, radius);
}
function drawPocket(x, y, radius, corner) {
  const shade = ctx.createRadialGradient(x - radius * .16, y - radius * .25, radius * .1, x, y, radius * 1.35);
  shade.addColorStop(0, "#030504");
  shade.addColorStop(.72, "#090d0a");
  shade.addColorStop(1, "#4f2c1d");
  ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.fillStyle = shade; ctx.fill();
  ctx.lineWidth = corner ? 7 : 5; ctx.strokeStyle = "#b77943"; ctx.stroke();
  ctx.beginPath(); ctx.arc(x, y, radius * .69, 0, Math.PI * 2); ctx.strokeStyle = "#1c100a"; ctx.lineWidth = 4; ctx.stroke();
}
function drawBall(ball) {
  if (ball.pocketed) return;
  const point = pixelPoint(ball);
  const radius = table.h * BALL_RADIUS;
  const number = Number(ball.number) || 0;
  const base = number === 0 ? "#f3f2e5" : ballColors[(number - 1) % ballColors.length];
  const stripe = number >= 9;
  const gradient = ctx.createRadialGradient(point.x - radius * .37, point.y - radius * .42, 1, point.x, point.y, radius * 1.12);
  gradient.addColorStop(0, "#ffffff"); gradient.addColorStop(.19, stripe ? "#faf8ec" : base); gradient.addColorStop(.76, base); gradient.addColorStop(1, "#18201b");
  ctx.save(); ctx.shadowColor = "#0009"; ctx.shadowBlur = 10; ctx.shadowOffsetY = 5;
  ctx.beginPath(); ctx.arc(point.x, point.y, radius, 0, Math.PI * 2); ctx.fillStyle = gradient; ctx.fill(); ctx.restore();
  if (stripe) {
    ctx.save(); ctx.beginPath(); ctx.arc(point.x, point.y, radius - 1, 0, Math.PI * 2); ctx.clip();
    ctx.fillStyle = "#fffdf0"; ctx.fillRect(point.x - radius, point.y - radius * .35, radius * 2, radius * .7); ctx.restore();
  }
  if (number) {
    ctx.beginPath(); ctx.arc(point.x, point.y, radius * .42, 0, Math.PI * 2); ctx.fillStyle = "#fffef3"; ctx.fill();
    ctx.fillStyle = "#18201a"; ctx.font = `bold ${Math.max(7, radius * .68)}px system-ui, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(String(number), point.x, point.y + .5);
  } else {
    ctx.beginPath(); ctx.arc(point.x - radius * .28, point.y - radius * .32, radius * .14, 0, Math.PI * 2); ctx.fillStyle = "#ffffffb5"; ctx.fill();
  }
  ctx.beginPath(); ctx.arc(point.x, point.y, radius - 1, 0, Math.PI * 2); ctx.strokeStyle = "#ffffff6a"; ctx.lineWidth = 1.2; ctx.stroke();
}
function drawCue(cue) {
  const point = pixelPoint(cue);
  if (drag) {
    const deltaX = drag.start.x - drag.current.x;
    const deltaY = drag.start.y - drag.current.y;
    const length = Math.hypot(deltaX, deltaY);
    const angle = Math.atan2(deltaY, deltaX);
    drawAimLine(point, angle, length);
    return;
  }
  if (!canPlay() || ballInHand()) return;
  const target = { x: table.x + aim.x * table.w, y: table.y + aim.y * table.h };
  const angle = Math.atan2(target.y - point.y, target.x - point.x);
  drawAimLine(point, angle, 0);
}
function drawAimLine(point, angle, pullLength) {
  ctx.save(); ctx.translate(point.x, point.y); ctx.rotate(angle);
  const cueTip = table.h * BALL_RADIUS + 3;
  const shaftLength = Math.min(310, table.w * .31);
  const pull = Math.min(95, pullLength * .55);
  ctx.globalAlpha = .93;
  ctx.shadowColor = "#000b"; ctx.shadowBlur = 8;
  const wood = ctx.createLinearGradient(0, -8, 0, 8); wood.addColorStop(0, "#f4d195"); wood.addColorStop(.5, "#b77c3d"); wood.addColorStop(1, "#69401f");
  const butt = -shaftLength - cueTip - 16 - pull;
  ctx.fillStyle = wood; ctx.beginPath(); ctx.moveTo(butt, -4); ctx.lineTo(-cueTip, -2.2); ctx.lineTo(-cueTip, 2.2); ctx.lineTo(butt, 4); ctx.closePath(); ctx.fill();
  ctx.fillStyle = "#ead8b0"; ctx.fillRect(-cueTip - 4, -2, 4, 4);
  ctx.fillStyle = "#53351f"; ctx.fillRect(butt - 10, -5, 10, 10);
  ctx.setLineDash([8, 9]); ctx.strokeStyle = "#f5f3da76"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(cueTip, 0); ctx.lineTo(Math.min(table.w * .3, 210), 0); ctx.stroke();
  ctx.restore();
  if (drag) {
    const strength = Math.max(10, Math.min(100, Math.round(Math.hypot(drag.start.x - drag.current.x, drag.start.y - drag.current.y) / (table.w * .29) * 100)));
    powerInput.value = String(strength); powerOutput.textContent = `${latinDigits(strength)}٪`;
  }
}
function drawTable() {
  ctx.clearRect(0, 0, WIDTH, HEIGHT);
  const wood = ctx.createLinearGradient(0, 30, WIDTH, HEIGHT);
  wood.addColorStop(0, "#69442a"); wood.addColorStop(.48, "#a56d3c"); wood.addColorStop(1, "#53341f");
  drawRoundRect(table.x - 36, table.y - 35, table.w + 72, table.h + 70, 34); ctx.fillStyle = wood; ctx.fill();
  ctx.strokeStyle = "#e1b07855"; ctx.lineWidth = 3; ctx.stroke();
  drawRoundRect(table.x - 18, table.y - 17, table.w + 36, table.h + 34, 24); ctx.fillStyle = "#26372c"; ctx.fill();
  drawRoundRect(table.x, table.y, table.w, table.h, table.r);
  const felt = ctx.createLinearGradient(0, table.y, 0, table.y + table.h);
  felt.addColorStop(0, "#247653"); felt.addColorStop(.48, "#176846"); felt.addColorStop(1, "#0e5339"); ctx.fillStyle = felt; ctx.fill();
  ctx.save(); drawRoundRect(table.x, table.y, table.w, table.h, table.r); ctx.clip();
  ctx.globalAlpha = .12; ctx.strokeStyle = "#c6edc9"; ctx.lineWidth = 1;
  for (let i = -table.h; i < table.w; i += 18) { ctx.beginPath(); ctx.moveTo(table.x + i, table.y); ctx.lineTo(table.x + i + table.h, table.y + table.h); ctx.stroke(); }
  ctx.restore();
  ctx.strokeStyle = "#a7dca466"; ctx.lineWidth = 2; drawRoundRect(table.x + 5, table.y + 5, table.w - 10, table.h - 10, 21); ctx.stroke();
  const pocketRadius = table.h * .06;
  [[0,0,1],[.5,0,0],[1,0,1],[0,1,1],[.5,1,0],[1,1,1]].forEach(([x, y, corner]) => drawPocket(table.x + x * table.w, table.y + y * table.h, pocketRadius, Boolean(corner)));
  for (let i = 1; i <= 7; i++) {
    const x = table.x + table.w * i / 8;
    [[table.y - 27], [table.y + table.h + 27]].forEach(([y]) => { ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fillStyle = "#e7bd7a"; ctx.fill(); });
  }
  const balls = Array.isArray(poolState?.balls) ? poolState.balls : fallbackBalls();
  balls.forEach(ball => {
    if (ballInHand() && placementPreview && Number(ball.number) === 0) return;
    drawBall(ball);
  });
  const cue = cueBall();
  if (placementPreview && ballInHand()) drawBall({ number: 0, ...placementPreview, pocketed: false });
  if (cue && !placementPreview) drawCue(cue);
}
function fallbackBalls() {
  const balls = [{ number: 0, x: .25, y: .5, pocketed: false }];
  let number = 1;
  for (let row = 0; row < 5; row++) for (let column = 0; column <= row; column++) balls.push({ number: number++, x: .685 + row * BALL_RADIUS * Math.sqrt(3) / TABLE_ASPECT, y: .5 + (column - row / 2) * BALL_RADIUS * 2, pocketed: false });
  return balls;
}
function resizeCanvas() {
  const rect = stage.getBoundingClientRect();
  const aspect = WIDTH / HEIGHT;
  const width = Math.max(1, Math.min(rect.width, rect.height * aspect));
  canvas.style.width = `${width}px`;
  canvas.style.height = `${width / aspect}px`;
  drawTable();
}
function renderTournament(next) {
  tournamentState = next || {};
  el("#tournament-label").textContent = tournamentState.tournament_id || "بطولة مباشرة";
  const playerCount = tournamentState.player_count ?? tournamentState.players?.length;
  el("#player-count").textContent = playerCount == null ? "—" : `${latinDigits(playerCount)} / ${latinDigits(tournamentState.max_players ?? "—")}`;
  const tournamentStatus = tournamentState.status || "بانتظار الاتصال";
  el("#tournament-status").textContent = tournamentStatus === "registration" ? `التسجيل · ${latinDigits(tournamentState.registration_remaining ?? 0)} ث` : tournamentStatus;
  el("#round-label").textContent = tournamentState.round ? `الجولة ${latinDigits(tournamentState.round)}` : "مرحلة البطولة";
  el("#match-label").textContent = tournamentState.tournament_id ? `بطولة ${tournamentState.tournament_id}` : "مباراة مباشرة";
  renderMatchList(tournamentState.matches || []);
  const matches = tournamentState.matches || [];
  currentMatch = spectator
    ? matches.find(match => match.match_id === matchId) || currentMatch
    : matches.find(match => (match.players || []).some(player => playerId(player) === String(identity?.user_id))) || matches.find(match => match.match_id === matchId) || currentMatch;
  updateHud();
  updateOverlay();
}
function renderMatchList(matches) {
  const list = el("#match-list");
  if (!matches.length) { list.innerHTML = '<p class="muted">ستظهر المباريات بعد إغلاق التسجيل.</p>'; return; }
  list.innerHTML = matches.map(match => {
    const isCurrent = match.match_id === matchId || match === currentMatch;
    const players = (match.players || []).map(player => {
      const isWinner = playerId(player) === String(match.winner_id);
      return `<div class="match-player ${isWinner ? "winner" : ""}"><span>${escapeHtml(player.display_name || "لاعب")}</span><span>${isWinner ? "فائز" : ""}</span></div>`;
    }).join("");
    const stateName = match.status === "waiting_entry" ? "بانتظار الدخول" : match.status === "ready" ? "مباشرة" : match.status || "قريباً";
    return `<article class="match-item ${isCurrent ? "active" : ""}"><header><span>مباراة ${latinDigits(match.slot ?? "—")}</span><span>${escapeHtml(stateName)}</span></header>${players}</article>`;
  }).join("");
}
function formatGroup(group) { return groupNames[group] || (group ? "مفتوحة" : "لم تُحدّد المجموعة بعد"); }
function ballCountFor(player) {
  const counts = poolState?.remaining_balls;
  const found = counts && Object.hasOwn(counts, String(player?.user_id)) ? counts[String(player.user_id)] : null;
  if (found != null) return `${latinDigits(found)} متبقية`;
  const group = poolState?.groups?.[String(player?.user_id)];
  if (group && Array.isArray(poolState?.balls)) {
    const count = poolState.balls.filter(ball => !ball.pocketed && (group === "solids" ? Number(ball.number) >= 1 && Number(ball.number) <= 7 : Number(ball.number) >= 9)).length;
    return `${latinDigits(count)} متبقية`;
  }
  return "الكرات المتبقية —";
}
function updateHud() {
  const players = currentMatch?.players || [];
  [0, 1].forEach(index => {
    const player = players[index];
    const group = poolState?.groups?.[String(player?.user_id)] ?? null;
    el(index === 0 ? "#player-one-name" : "#player-two-name").textContent = player?.display_name || (index === 0 ? "اللاعب الأول" : "بانتظار اللاعب الثاني");
    el(index === 0 ? "#player-one-group" : "#player-two-group").textContent = formatGroup(group);
    el(index === 0 ? "#player-one-balls" : "#player-two-balls").textContent = player ? ballCountFor(player) : "بانتظار اللاعب";
    const active = player && poolState?.turn_user_id != null && String(poolState.turn_user_id) === String(player.user_id) && !poolState.moving;
    el(index === 0 ? "#player-one-card" : "#player-two-card").classList.toggle("active", Boolean(active));
    el(index === 0 ? "#player-one-turn" : "#player-two-turn").title = active ? "دوره الآن" : "بانتظار الدور";
  });
  const seconds = poolState?.turn_remaining;
  el("#turn-timer").textContent = seconds == null ? "—" : latinDigits(seconds);
  const turnLabel = poolState?.moving ? "الكرات تتحرك" : isMyTurn() ? (spectator ? "دور لاعب" : "دورك الآن") : "دور الخصم";
  el("#turn-label").textContent = poolState ? turnLabel : "طاولة كوزمو";
  el("#shot-label").textContent = poolState?.shot_number != null ? `التسديدة ${latinDigits(poolState.shot_number)}` : "جاهزة للعب";
  el("#phase-label").textContent = phaseNames[poolState?.phase] || (currentMatch?.status === "ready" ? "المباراة جارية" : "استعد");
  const mayShoot = canPlay();
  shootButton.disabled = !mayShoot;
  el("#control-hint").textContent = spectator ? "مشاهدة مباشرة — التحكم مع اللاعبين" : ballInHand() ? "حرّك الكرة البيضاء إلى مكان مناسب ثم ارفع إصبعك" : mayShoot ? "اسحب من الكرة البيضاء للتصويب، أو استخدم الأسهم" : "ستتاح أدوات التصويب عند حلول دورك";
  el("#aim-hint").hidden = !mayShoot || ballInHand();
  el("#spectator-badge").hidden = !spectator;
  if (poolState?.phase === "finished" || (poolState?.winner_id != null)) {
    const winner = players.find(player => String(player.user_id) === String(poolState.winner_id ?? currentMatch?.winner_id));
    resultBanner.textContent = winner ? `انتهت المباراة — الفائز ${winner.display_name}` : "انتهت المباراة";
    resultBanner.classList.remove("foul"); resultBanner.hidden = false;
  } else if (poolState?.last_shot?.foul) {
    resultBanner.textContent = poolState.last_shot.message || "مخالفة — انتقلت الكرة البيضاء إلى اللاعب الآخر";
    resultBanner.classList.add("foul"); resultBanner.hidden = false;
  } else {
    resultBanner.hidden = true;
  }
  const message = poolState?.last_shot?.message || (ballInHand() ? "كرة بيضاء في اليد — اختر موضعاً للطاولة" : poolState?.moving ? "الكرات تتحرك…" : mayShoot ? "دورك — وجّه العصا واضرب" : spectator ? "المشاهدة فقط — الطاولة مباشرة" : "انتظر دورك");
  el("#table-message").textContent = message;
}
function updateOverlay() {
  const title = el("#overlay-title");
  const detail = el("#overlay-detail");
  connectButton.hidden = false;
  if (spectator && poolState) {
    overlay.classList.add("hidden");
    return;
  }
  if (poolState?.phase === "finished") {
    title.textContent = "انتهت المباراة"; detail.textContent = poolState.last_shot?.message || "شكراً للعب في صالة كوزمو."; connectButton.hidden = true;
    overlay.classList.remove("hidden"); return;
  }
  if (poolState?.moving) {
    overlay.classList.add("hidden"); return;
  }
  if (ballInHand()) {
    title.textContent = "الكرة البيضاء في يدك"; detail.textContent = "المس موضعاً داخل الطاولة لوضعها، ثم صوّب تسديدتك."; connectButton.hidden = true; overlay.classList.remove("hidden"); return;
  }
  if (canPlay()) { overlay.classList.add("hidden"); return; }
  if (currentMatch?.status === "waiting_entry") {
    title.textContent = "بانتظار دخول اللاعب الآخر"; detail.textContent = `المهلة المتبقية ${latinDigits(currentMatch.entry_remaining ?? 0)} ثانية.`; connectButton.hidden = true;
  } else if (currentMatch?.status === "ready" && poolState) {
    title.textContent = "دور الخصم"; detail.textContent = "راقب المباراة مباشرة — ستظهر أدواتك عندما يحين دورك."; connectButton.hidden = true;
  } else if (!socket || socket.readyState !== WebSocket.OPEN) {
    title.textContent = "العب على طاولة كوزمو";
    detail.textContent = spectator ? "اتصل لمشاهدة هذه المباراة مباشرة." : "افتح اللعبة من رابط مباراة Discord للدخول إلى طاولتك.";
    connectButton.textContent = spectator ? "بدء المشاهدة" : "دخول المباراة";
  } else {
    title.textContent = "تهيئة الطاولة"; detail.textContent = "جارٍ مزامنة المباراة مع الخادم…"; connectButton.hidden = true;
  }
  overlay.classList.remove("hidden");
}
function onMessage(event) {
  let message;
  try { message = JSON.parse(event.data); } catch { toast("وصلت رسالة غير مفهومة من الخادم"); return; }
  if (message.type === "hello") {
    if (message.protocol && message.protocol !== protocolVersion) {
      setConnection("إصدار غير متوافق", "error");
      toast("إصدار اللعبة غير متوافق مع الخادم");
      deliberateClose = true;
      socket?.close();
    }
    return;
  }
  if (message.type === "authenticated") {
    identity = message.user || null;
    setConnection("متصل", "connected");
    reconnectButton.hidden = true;
    if (spectator) send({ type: "watch_match", tournament_id: tournamentId, match_id: matchId });
    else send({ type: "enter_match", tournament_id: tournamentId, match_id: matchId });
  } else if (message.type === "tournament_state") {
    renderTournament(message.state || {});
    if (spectator) setConnection("مشاهدة مباشرة", "connected");
  } else if (message.type === "pool_state") {
    const previousShot = poolState?.shot_number;
    poolState = message.state || {};
    if (message.spectator) { /* The server can mark broadcast snapshots for either audience. */ }
    updateHud(); updateOverlay(); drawTable();
    const shot = poolState.shot_number;
    if (shot != null && previousShot != null && shot !== previousShot && poolState.last_shot?.message) toast(poolState.last_shot.message);
    if (shot != null && lastSeenShot != null && shot > lastSeenShot && poolState.last_shot?.pocketed?.length) toast(`كرة في الجيب: ${poolState.last_shot.pocketed.map(latinDigits).join("، ")}`);
    if (shot != null) lastSeenShot = shot;
  } else if (message.type === "error") {
    const text = message.message || "تعذر تنفيذ العملية";
    toast(text);
    setConnection("تعذر الاتصال", "error");
    if (["auth_rejected", "match_entry_rejected", "watch_rejected", "state_unavailable"].includes(message.code)) {
      el("#overlay-title").textContent = "تعذر فتح المباراة"; el("#overlay-detail").textContent = text; connectButton.hidden = true; overlay.classList.remove("hidden");
    }
  }
}
function connect() {
  window.clearTimeout(autoReconnectTimer);
  if (socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) return;
  deliberateClose = false;
  setConnection("جارٍ الاتصال…");
  el("#overlay-title").textContent = "جارٍ الاتصال"; el("#overlay-detail").textContent = "يتم إنشاء اتصال آمن بالطاولة…"; connectButton.disabled = true;
  try { socket = new WebSocket(socketUrl()); }
  catch { setConnection("تعذر الاتصال", "error"); connectButton.disabled = false; return; }
  socket.onopen = () => {
    reconnectDelay = 1400; setConnection("متصل", "connected");
    if (authToken) send({ type: "auth", token: authToken });
    else if (spectator) send({ type: "watch_match", tournament_id: tournamentId, match_id: matchId });
    else {
      toast("افتح اللعبة من رابط مباراة Discord.");
      el("#overlay-title").textContent = "رابط الدخول مطلوب"; el("#overlay-detail").textContent = "افتح اللعبة من زر المباراة في Discord لتأكيد هويتك.";
      socket.close();
    }
  };
  socket.onmessage = onMessage;
  socket.onerror = () => setConnection("مشكلة في الاتصال", "error");
  socket.onclose = () => {
    setConnection("انقطع الاتصال", "error"); connectButton.disabled = false; reconnectButton.hidden = false; updateHud(); updateOverlay();
    if (!deliberateClose && (authToken || spectator)) {
      autoReconnectTimer = window.setTimeout(() => { reconnectDelay = Math.min(12000, reconnectDelay * 1.7); connect(); }, reconnectDelay);
    }
  };
}
function fireShot(angle, shotPower = Number(powerInput.value) / 100) {
  if (!canPlay() || ballInHand()) return;
  send({ type: "shoot", angle, power: Math.max(.10, Math.min(1, shotPower)) });
  drag = null; drawTable();
}
function putCueBall(point) {
  if (!ballInHand()) return;
  const normalized = clothPoint(point);
  if (send({ type: "place_cue_ball", x: normalized.x, y: normalized.y })) {
    placementPreview = null; overlay.classList.add("hidden"); drawTable();
  }
}
function onPointerDown(event) {
  if (!canPlay()) return;
  canvas.focus({ preventScroll: true });
  const point = localPoint(event.clientX, event.clientY);
  if (ballInHand()) {
    placementPreview = clothPoint(point);
    canvas.setPointerCapture(event.pointerId); drawTable(); return;
  }
  drag = { pointerId: event.pointerId, start: point, current: point };
  canvas.setPointerCapture(event.pointerId); drawTable();
}
function onPointerMove(event) {
  const point = localPoint(event.clientX, event.clientY);
  if (ballInHand() && canvas.hasPointerCapture(event.pointerId)) { placementPreview = clothPoint(point); drawTable(); return; }
  if (drag && drag.pointerId === event.pointerId) { drag.current = point; drawTable(); return; }
  const cloth = clothPoint(point); aim = cloth; drawTable();
}
function onPointerUp(event) {
  if (ballInHand() && placementPreview) { putCueBall(localPoint(event.clientX, event.clientY)); return; }
  if (!drag || drag.pointerId !== event.pointerId) return;
  const release = localPoint(event.clientX, event.clientY);
  const dx = drag.start.x - release.x;
  const dy = drag.start.y - release.y;
  const length = Math.hypot(dx, dy);
  if (length < 8) {
    const cue = cueBall();
    if (cue) {
      const target = clothPoint(release);
      aim = target;
      fireShot(Math.atan2(target.y - cue.y, (target.x - cue.x) * TABLE_ASPECT), Number(powerInput.value) / 100);
    }
  } else {
    const strength = Math.max(.10, Math.min(1, length / (table.w * .29)));
    fireShot(Math.atan2(dy, dx), strength);
  }
}
function onKeyDown(event) {
  if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", " "].includes(event.key) && event.target !== powerInput) event.preventDefault();
  const step = event.shiftKey ? .035 : .012;
  if (event.key === "ArrowUp") aim.y = Math.max(.03, aim.y - step);
  else if (event.key === "ArrowDown") aim.y = Math.min(.97, aim.y + step);
  else if (event.key === "ArrowLeft") aim.x = Math.max(.03, aim.x - step);
  else if (event.key === "ArrowRight") aim.x = Math.min(.97, aim.x + step);
  else if (event.key === " " && !event.repeat) {
    const cue = cueBall();
    if (cue && canPlay()) fireShot(Math.atan2(aim.y - cue.y, (aim.x - cue.x) * TABLE_ASPECT));
  } else return;
  drawTable();
}
function toggleTournament(open = tournamentPanel.hidden) {
  tournamentPanel.hidden = !open;
  el("#tournament-button").setAttribute("aria-expanded", String(open));
}
async function toggleFullscreen() {
  try {
    if (!document.fullscreenElement) await document.documentElement.requestFullscreen();
    else await document.exitFullscreen();
    await lockLandscape();
  } catch { toast("ملء الشاشة غير متاح في هذا المتصفح"); }
}

async function lockLandscape() {
  try {
    if (screen.orientation?.lock) await screen.orientation.lock("landscape");
  } catch {
    // The CSS fallback keeps the game usable when locking requires fullscreen.
  }
}

connectButton.addEventListener("click", async () => { await lockLandscape(); connect(); });
reconnectButton.addEventListener("click", async () => { await lockLandscape(); connect(); });
shootButton.addEventListener("click", () => {
  const cue = cueBall();
  if (cue && canPlay()) fireShot(Math.atan2(aim.y - cue.y, (aim.x - cue.x) * TABLE_ASPECT));
});
powerInput.addEventListener("input", () => { powerOutput.textContent = `${latinDigits(powerInput.value)}٪`; });
canvas.addEventListener("pointerdown", onPointerDown);
canvas.addEventListener("pointermove", onPointerMove);
canvas.addEventListener("pointerup", onPointerUp);
canvas.addEventListener("pointercancel", () => { drag = null; placementPreview = null; drawTable(); });
canvas.addEventListener("keydown", onKeyDown);
el("#tournament-button").addEventListener("click", () => toggleTournament());
el("#close-tournament").addEventListener("click", () => toggleTournament(false));
el("#fullscreen-button").addEventListener("click", toggleFullscreen);
document.addEventListener("fullscreenchange", () => { el("#fullscreen-button").setAttribute("aria-label", document.fullscreenElement ? "إنهاء ملء الشاشة" : "ملء الشاشة"); resizeCanvas(); });
window.addEventListener("resize", resizeCanvas);
if ("ResizeObserver" in window) new ResizeObserver(resizeCanvas).observe(stage);

powerOutput.textContent = `${latinDigits(powerInput.value)}٪`;
resizeCanvas();
lockLandscape();
if (authToken || spectator) connect();
const protocolVersion = "cosmo-8ball-v1";
