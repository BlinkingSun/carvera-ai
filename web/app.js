// Carvera Control UI — talks only to the local server's JSON API.
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
let S = null;            // last /api/status
let step = 1;
let actSeq = 0, msgSeq = 0;
let lastJobId = null;

const fmt = (v, d = 3) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(d);

async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: { "Content-Type": "application/json", "X-Source": "ui" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let out = {};
  try { out = await r.json(); } catch (_) {}
  if (!r.ok) {
    const e = new Error(out.error || `${r.status}`);
    e.pair = r.status === 401 && !!out.pair;   // phone/tablet that is not paired (yet)
    throw e;
  }
  return out;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), err ? 6000 : 2500);
}

async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- status
async function poll() {
  try {
    S = await api("GET", "/api/status");
    render();
  } catch (e) {
    if (e.pair) return showPair();          // polling restarts after pairing
    document.body.classList.add("offline");
    $("#state").textContent = "Server down"; $("#state").className = "pill Alarm";
    $("#mState").textContent = "Server down"; $("#mState").className = "pill Alarm";
  }
  setTimeout(poll, document.hidden ? 2000 : 250);
}

function render() {
  const st = S.status || {};
  const connected = S.connected;
  document.body.classList.toggle("offline", !connected);
  const busy = !!(S.job && S.job.state === "running");
  document.body.classList.toggle("busy", busy);
  const state = connected ? (st.state || "…") : "Offline";
  const pill = $("#state");
  pill.textContent = busy ? `${state} · ${S.job.kind}` : state;
  pill.className = "pill " + (connected ? (st.state || "") : "off");
  $("#machine").textContent = connected ? `${S.info.name || "Carvera"} ${S.host}${S.info.version ? " · fw " + S.info.version : ""}` : S.host;
  $("#machine").title = $("#machine").textContent;
  $("#btnConnect").textContent = connected ? "Disconnect" : "Connect";
  $("#banner").classList.toggle("hidden", !(connected && (st.state || "").startsWith("Alarm")));
  if (connected && (st.state || "").startsWith("Alarm")) $("#banner").textContent = "Machine in ALARM. Check it, then press Unlock (and Home if position may be lost).";

  for (const a of ["x", "y", "z"]) {
    $("#w" + a).textContent = st.wpos ? fmt(st.wpos[a]) : "—";
    $("#m" + a).textContent = st.mpos ? fmt(st.mpos[a]) : "—";
    $("#mW" + a).textContent = st.wpos ? fmt(st.wpos[a]) : "—";
  }
  $("#mState").textContent = pill.textContent; $("#mState").className = pill.className;
  const rb = $("#btnRemote"), rem = S.remote || {};
  rb.classList.toggle("hidden", S.client === "remote");      // phone access is managed on the computer
  rb.classList.toggle("on", !!rem.on);
  rb.textContent = rem.on ? "Phone · on" : "Phone";
  $("#tool").textContent = connected ? S.tool_name : "—";
  $("#tlo").textContent = fmt(st.tlo);
  $("#probev").textContent = st.probe_v ? `${fmt(st.probe_v, 2)} V` : "—";
  $("#rpm").textContent = st.spindle_rpm !== undefined ? `${Math.round(st.spindle_rpm)} rpm` : "—";
  renderVacuum(connected);
  renderSpindle(connected);
  const g = (S.offsets || {}).G54;
  $("#g54").textContent = g ? `${fmt(g.x)}, ${fmt(g.y)}, ${fmt(g.z, 4)}` : "—";
  const p = (S.offsets || {}).PRB;
  $("#prb").textContent = p ? `Z ${fmt(p.z, 4)}${p.hit ? "" : " (miss)"}` : "—";
  $("#wcs").textContent = S.modal ? (S.modal.match(/G5[4-9]/) || [""])[0] : "";

  const j = S.job;
  const box = $("#job");
  if (j) {
    box.classList.remove("hidden");
    box.className = "job " + j.state;
    const res = j.state === "failed" ? `✗ ${j.error}` : j.state === "done" ? "✓ done" : "running…";
    box.innerHTML = `<b>${j.kind}</b> <span class="muted">(${j.source})</span> — ${esc(res)}` +
      (j.messages && j.messages.length ? `<pre>${esc(j.messages.slice(-8).join("\n"))}</pre>` : "");
    if (j.id !== lastJobId && j.state !== "running") { lastJobId = j.id; refreshPoints(); }
  }
  if (S.activity_seq > actSeq) loadActivity();
  if (S.msg_seq > msgSeq) loadMessages();
}

// Vacuum + probe charger come from the firmware's 'diagnose' report (null = unknown / stale).
let vacBusy = false;
function renderVacuum(connected) {
  const v = S.vacuum || {};
  const pill = $("#vacState");
  let text = "—", cls = "pill vac-unknown";
  if (connected && v.on === true) { text = `On ${v.power}%`; cls = "pill vac-on"; }
  else if (connected && v.on === false) { text = v.auto ? "Auto" : "Off"; cls = v.auto ? "pill vac-auto" : "pill off"; }
  else if (connected) text = "?";
  pill.textContent = text; pill.className = cls;
  pill.title = v.auto ? "Vacuum mode (M331): the vacuum follows the spindle" : "";
  $("#vacFact").textContent = !connected ? "—" : v.on === true ? `on ${v.power}%` : v.on === false ? (v.auto ? "off (auto w/ spindle)" : "off") : "unknown";
  $("#charger").textContent = !connected || v.probe_charger === null || v.probe_charger === undefined ? "—" : v.probe_charger ? "on" : "OFF";
  $("#btnVacOn").textContent = v.on === true ? "Set" : "On";
  for (const id of ["#btnVacOn", "#btnVacOff", "#vacPower"]) $(id).disabled = !connected || vacBusy;
}

async function setVacuum(on) {
  const power = Math.round(Number($("#vacPower").value));
  if (on && !(power >= 1 && power <= 100)) return toast("Vacuum power must be 1–100 %", true);
  vacBusy = true;
  try {
    const r = await api("POST", "/api/vacuum", on ? { on, power } : { on });
    const v = r.vacuum;
    if (!r.confirmed) toast(`Sent ${r.command}, but the machine did not confirm the vacuum state`, true);
    else toast(on ? `Vacuum on ${v.power}%` : "Vacuum off");
    try { if (on) localStorage.setItem("cc.vacPower", String(power)); } catch (_) {}
  } catch (e) { toast(e.message, true); }
  finally { vacBusy = false; }
}

// Spindle: switch/target from 'diagnose', actual rpm from the status line; can_start comes from the server.
let spinBusy = false;
function renderSpindle(connected) {
  const sp = S.spindle || {};
  const pill = $("#spinState");
  let text = "—", cls = "pill off";
  if (connected && sp.on === true) { text = `On ${sp.target}`; cls = "pill spin-on"; }
  else if (connected && sp.on === false) text = "Off";
  else if (connected) text = "?";
  pill.textContent = text; pill.className = cls;
  pill.title = connected && sp.rpm !== null && sp.rpm !== undefined ? `actual ${Math.round(sp.rpm)} rpm` : "";
  const start = $("#btnSpinOn");
  start.textContent = sp.on === true ? "Set" : "Start";
  start.disabled = !connected || spinBusy || !sp.can_start;
  start.title = sp.can_start ? "Start the spindle (asks to confirm)" : `Can't start: ${sp.why_not || "unknown"}`;
  $("#btnSpinOff").disabled = !connected || spinBusy;
  $("#spinRpm").disabled = !connected || spinBusy;
}

async function setSpindle(on) {
  const rpm = Math.round(Number($("#spinRpm").value));
  const [lo, hi] = (S && S.spindle && S.spindle.range) || [1000, 20000];
  if (on) {
    if (!(rpm >= lo && rpm <= hi)) return toast(`Spindle speed must be ${lo}–${hi} rpm`, true);
    const tool = (S && S.tool_name) || "the tool";
    const verb = S && S.spindle && S.spindle.on ? `Change the spindle to ${rpm} rpm` : `Start the spindle at ${rpm} rpm`;
    if (!confirm(`${verb} with ${tool}?\n\nKeep hands and loose items clear. It takes about 15 s to reach speed.`)) return;
  }
  spinBusy = true;
  try {
    const r = await api("POST", "/api/spindle", on ? { on, rpm } : { on });
    if (!r.confirmed) toast(`Sent ${r.command}, but the machine did not confirm the spindle state`, true);
    else toast(on ? `Spindle on ${r.spindle.target} rpm` : "Spindle stopped");
    try { if (on) localStorage.setItem("cc.spinRpm", String(rpm)); } catch (_) {}
  } catch (e) { toast(e.message, true); }
  finally { spinBusy = false; }
}

// ---------------------------------------------------------------- phone / tablet pairing (remote listener)
function guessDeviceName() {
  const ua = navigator.userAgent;
  if (/iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return "iPad";
  if (/iPhone/.test(ua)) return "iPhone";
  if (/Android/.test(ua)) return /Mobile/.test(ua) ? "Android phone" : "Android tablet";
  return "Phone or tablet";
}

function showPair() {
  $("#pair").classList.remove("hidden");
  if (!$("#pairName").value) $("#pairName").value = guessDeviceName();
  setTimeout(() => $("#pairCode").focus(), 50);
}

async function submitPair(e) {
  e.preventDefault();
  $("#pairErr").textContent = "";
  $("#pairBtn").disabled = true;
  try {
    const r = await api("POST", "/api/pair", { code: $("#pairCode").value, name: $("#pairName").value });
    $("#pair").classList.add("hidden");
    $("#pairCode").value = "";
    toast(`Paired as ${r.device}`);
    refreshPoints(); poll();
  } catch (err) {
    $("#pairErr").textContent = err.message;
    $("#pairCode").select();
  } finally { $("#pairBtn").disabled = false; }
}

// ---------------------------------------------------------------- phone access dialog (on the computer)
let remoteTimer = null;
function renderRemote(v) {
  $("#rState").textContent = v.on ? "on" : "off";
  $("#rToggle").textContent = v.on ? "Turn off" : "Turn on";
  $("#rErr").textContent = v.error || "";
  $("#rOn").classList.toggle("hidden", !v.on);
  $("#rUrls").innerHTML = (v.urls || []).map((u) => `<div>${esc(u)}</div>`).join("") || `<div class="muted">port ${v.port}</div>`;
  $("#rCode").textContent = v.code ? `${v.code.slice(0, 3)} ${v.code.slice(3)}` : "—";
  $("#rDevs").innerHTML = (v.devices || []).map((d) =>
    `<div><span><b>${esc(d.name)}</b> <span class="muted">paired ${esc(d.paired)} · last seen ${esc(d.last_seen)}</span></span>` +
    `<button data-id="${esc(d.id)}" title="Forget this device">Forget</button></div>`).join("") ||
    `<div class="muted">None yet.</div>`;
  $("#rForgetAll").disabled = !(v.devices || []).length;
}
async function refreshRemote() {
  try { renderRemote(await api("GET", "/api/remote")); } catch (e) { $("#rErr").textContent = e.message; }
}
function openRemote() {
  $("#remoteDlg").showModal();
  refreshRemote();
  clearInterval(remoteTimer); remoteTimer = setInterval(refreshRemote, 1000);
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------------------------------------------------------------- activity & messages
async function loadActivity() {
  const r = await api("GET", `/api/activity?limit=80`);
  const box = $("#activity");
  box.innerHTML = r.items.slice().reverse().map((a) => {
    const failed = String(a.action).endsWith(":failed");
    let d = a.detail;
    const xyz = (p) => p ? `X ${fmt(p.x)} Y ${fmt(p.y)} Z ${fmt(p.z)}` : "";
    if (d && d.error) d = d.error;
    else if (d && d.command) d = `${d.command}${d.confirmed ? " ✓ confirmed" : " (not confirmed)"}`;
    else if (d && d.params) d = Object.entries(d.params).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
    else if (d && d.result) d = d.result.mpos ? `→ machine ${xyz(d.result.mpos)}` : (d.result.note || JSON.stringify(d.result).slice(0, 140));
    else if (d && d.result === null) d = "";
    else if (d && typeof d === "object") d = JSON.stringify(d).slice(0, 200);
    return `<div class="act${failed ? " failed" : ""}"><span class="muted">${a.time}</span><span class="src ${a.source}">${a.source}</span>` +
      `<span class="d"><b>${esc(a.action)}</b> ${esc(d || "")}</span></div>`;
  }).join("");
  actSeq = S ? S.activity_seq : actSeq;
}

async function loadMessages() {
  const r = await api("GET", `/api/messages?since=${Math.max(0, msgSeq - 60)}`);
  const box = $("#messages");
  box.innerHTML = r.messages.slice(-60).reverse().map((m) => `<div>${esc(m.text)}</div>`).join("");
  msgSeq = S ? S.msg_seq : msgSeq;
}

// ---------------------------------------------------------------- points
let POINTS = [];
async function refreshPoints() {
  const r = await api("GET", "/api/points");
  POINTS = r.points;
  $("#ptNames").innerHTML = POINTS.map((p) => `<option value="${esc(p.name)}">`).join("");
  const body = $("#ptBody");
  if (!POINTS.length) { body.innerHTML = `<tr><td colspan="5" class="muted">No points yet.</td></tr>`; return; }
  body.innerHTML = POINTS.map((p) => {
    const cell = (a) => {
      const c = p.axes[a];
      if (!c) return `<td class="v muted">—</td>`;
      const tip = `work ${fmt(c.work)} · T${c.tool} TLO ${fmt(c.tlo)} · ${c.at}`;
      return `<td class="v" title="${esc(tip)}">${fmt(c.machine)}</td>`;
    };
    return `<tr><td><b>${esc(p.name)}</b>${p.note ? `<div class="note">${esc(p.note)}</div>` : ""}</td>` +
      cell("x") + cell("y") + cell("z") +
      `<td class="acts"><button class="go" data-name="${esc(p.name)}">Go</button> ` +
      `<button class="note-btn" data-name="${esc(p.name)}" title="Edit note">✎</button> ` +
      `<button class="del" data-name="${esc(p.name)}" title="Delete">✕</button></td></tr>`;
  }).join("");
}

// ---------------------------------------------------------------- jog
const PRESETS = () => $$("#steps button[data-step]").map((b) => Number(b.dataset.step));
function setStep(v) {
  step = Number(v);
  $$("#steps button[data-step]").forEach((b) => b.classList.toggle("on", Number(b.dataset.step) === step));
  const custom = !PRESETS().includes(step);
  $("#stepCustom").classList.toggle("on", custom);
  if (custom) $("#stepCustom").value = step;
  $("#stepShow").textContent = `${step} mm`;
  try { localStorage.setItem("cc.step", String(step)); } catch (_) {}
}

function useCustomStep() {
  const v = Number($("#stepCustom").value);
  if (!(v >= 0.001 && v <= 100)) return toast("Custom step must be 0.001–100 mm", true);
  setStep(Math.round(v * 1000) / 1000);
  toast(`Jog step ${step} mm`);
}

let jogging = false;
async function jog(axis, dir) {
  if (jogging) return;
  jogging = true;
  const feed = axis === "z" ? Number($("#feedZ").value) : Number($("#feedXY").value);
  try { await api("POST", "/api/jog?wait=1", { axis, distance: dir * step, feed }); }
  catch (e) { toast(e.message, true); }
  finally { jogging = false; }
}

// ---------------------------------------------------------------- wiring
function wire() {
  $$("#steps button[data-step]").forEach((b) => b.addEventListener("click", () => setStep(b.dataset.step)));
  $("#stepCustomUse").addEventListener("click", useCustomStep);
  $("#stepCustom").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); useCustomStep(); } });
  $$("button.j").forEach((b) => b.addEventListener("click", () => jog(b.dataset.axis, Number(b.dataset.dir))));

  document.addEventListener("keydown", (e) => {
    if (!$("#kbd").checked || e.target.matches("input,select,textarea")) return;
    const map = { ArrowLeft: ["x", -1], ArrowRight: ["x", 1], ArrowUp: ["y", 1], ArrowDown: ["y", -1], PageUp: ["z", 1], PageDown: ["z", -1] };
    const m = map[e.key];
    if (m) { e.preventDefault(); if (!e.repeat) jog(m[0], m[1]); }
  });

  $("#btnConnect").addEventListener("click", () => act(async () => {
    if (S && S.connected) return api("POST", "/api/disconnect", {});
    return api("POST", "/api/connect", {});
  }));
  $("#btnStop").addEventListener("click", () => act(() => api("POST", "/api/stop", {}), "Stopped — Unlock before moving"));
  $("#btnHold").addEventListener("click", () => act(() => api("POST", "/api/hold", {}), "Feed hold"));
  $("#btnResume").addEventListener("click", () => act(() => api("POST", "/api/resume", {}), "Resumed"));
  $("#btnUnlock").addEventListener("click", () => act(() => api("POST", "/api/unlock?wait=1", {}), "Unlocked"));
  $("#btnVacOn").addEventListener("click", () => setVacuum(true));
  $("#btnVacOff").addEventListener("click", () => setVacuum(false));
  $("#vacPower").addEventListener("keydown", (e) => { if (e.key === "Enter") setVacuum(true); });
  $("#btnSpinOn").addEventListener("click", () => setSpindle(true));
  $("#btnSpinOff").addEventListener("click", () => setSpindle(false));
  $("#spinRpm").addEventListener("keydown", (e) => { if (e.key === "Enter" && !$("#btnSpinOn").disabled) setSpindle(true); });

  $("#btnHome").addEventListener("click", () => {
    if (confirm("Home the machine? Z rises first, then X/Y go to the home switches.")) act(() => api("POST", "/api/home", {}));
  });
  $("#btnTool").addEventListener("click", () => {
    const t = Number($("#toolSel").value);
    const name = $("#toolSel").selectedOptions[0].textContent;
    if (confirm(`Change to ${name}? The ATC lifts Z, swaps tools at the rack and measures the new tool on the setter.`))
      act(() => api("POST", "/api/tool", { tool: t }));
  });
  $("#btnProbe").addEventListener("click", () => {
    const max = Number($("#pMax").value), lift = Number($("#pLift").value), set = $("#pSet").checked;
    const msg = `Probe Z here?\n\nProbe moves down up to ${max} mm (fast 300 mm/min), re-touches slowly (60 mm/min), lifts ${lift} mm.` +
      (set ? `\n\nThen sets WORK Z0 on the surface. This resets the tool length offset: the probe becomes the length reference.` : "");
    if (confirm(msg)) act(() => api("POST", "/api/probe/z", { max_distance: max, lift, set_zero: set }));
  });
  $("#btnMove").addEventListener("click", () => {
    const body = { system: $("#toSys").value, feed: Number($("#feedXY").value), z_feed: Number($("#feedZ").value) };
    for (const a of ["x", "y", "z"]) { const v = $("#to" + a.toUpperCase()).value; if (v !== "") body[a] = Number(v); }
    if (!("x" in body || "y" in body || "z" in body)) return toast("Enter at least one coordinate", true);
    const desc = ["x", "y", "z"].filter((a) => a in body).map((a) => `${a.toUpperCase()} ${body[a]}`).join(", ");
    if (confirm(`Move to ${body.system} ${desc}?\n(Z moves first when going up, last when going down.)`)) act(() => api("POST", "/api/move", body));
  });
  $$("button.zero").forEach((b) => b.addEventListener("click", () => {
    const a = b.dataset.axis;
    const extra = a === "z" ? "\n\nNote: setting work Z resets the tool length offset — the current tool becomes the length reference." : "";
    if (confirm(`Set work ${a.toUpperCase()} = 0 at the current position?${extra}`)) act(() => api("POST", "/api/zero", { axes: [a] }));
  }));

  $$("button.cap").forEach((b) => b.addEventListener("click", () => {
    const name = $("#ptName").value.trim();
    if (!name) return toast("Type a point name first", true);
    const axes = b.dataset.axes.split(",");
    act(async () => { await api("POST", "/api/points", { name, axes }); await refreshPoints(); },
      `Saved ${axes.map((a) => a.toUpperCase()).join("")} → ${name}`);
  }));
  $("#ptBody").addEventListener("click", (e) => {
    const cell = e.target.closest("td.v[title]");
    if (cell) return toast(cell.title);          // touch screens have no hover
    const b = e.target.closest("button"); if (!b) return;
    const name = b.dataset.name;
    if (b.classList.contains("go")) {
      const mode = $("#gotoMode").value, include_z = $("#gotoZ").checked;
      const how = mode === "lift" ? "lift to safe Z, then move XY" : "move XY at the current height";
      if (confirm(`Go to "${name}"? (${how}${include_z ? ", then Z" : ""})`))
        act(() => api("POST", `/api/points/${encodeURIComponent(name)}/goto`, { mode, include_z, feed: Number($("#feedXY").value) }));
    } else if (b.classList.contains("del")) {
      if (confirm(`Delete point "${name}"?`)) act(async () => { await api("DELETE", `/api/points/${encodeURIComponent(name)}`); await refreshPoints(); });
    } else if (b.classList.contains("note-btn")) {
      const p = POINTS.find((x) => x.name === name);
      const note = prompt(`Note for "${name}":`, p ? p.note : "");
      if (note !== null) act(async () => { await api("PATCH", `/api/points/${encodeURIComponent(name)}`, { note }); await refreshPoints(); });
    }
  });
  try {
    const sv = localStorage.getItem("cc.step"); setStep(sv ? Number(sv) : 1);
    const gm = localStorage.getItem("cc.gotoMode"); if (gm) $("#gotoMode").value = gm;
    const vp = localStorage.getItem("cc.vacPower"); if (vp) $("#vacPower").value = vp;
    const sr = localStorage.getItem("cc.spinRpm"); if (sr) $("#spinRpm").value = sr;
  } catch (_) { setStep(1); }
  $("#gotoMode").addEventListener("change", (e) => { try { localStorage.setItem("cc.gotoMode", e.target.value); } catch (_) {} });

  $("#pairForm").addEventListener("submit", submitPair);
  $("#btnRemote").addEventListener("click", openRemote);
  $("#rClose").addEventListener("click", () => $("#remoteDlg").close());
  $("#remoteDlg").addEventListener("close", () => clearInterval(remoteTimer));
  $("#rToggle").addEventListener("click", async () => {
    const on = $("#rState").textContent !== "on";
    try { renderRemote(await api("POST", "/api/remote", { on })); }
    catch (e) { $("#rErr").textContent = e.message; refreshRemote(); }
  });
  $("#rForgetAll").addEventListener("click", () => {
    if (confirm("Forget every paired phone and tablet? Each one needs a new code to connect again."))
      act(async () => renderRemote(await api("POST", "/api/remote/forget", {})), "All devices forgotten");
  });
  $("#rDevs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-id]"); if (!b) return;
    act(async () => renderRemote(await api("POST", "/api/remote/forget", { id: b.dataset.id })), "Device forgotten");
  });
}

wire();
refreshPoints().catch(() => {});
poll();
