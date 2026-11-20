"use strict";
const $ = (id) => document.getElementById(id);
const store = {
  get: (k, d = "") => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

// ---- state ----------------------------------------------------------------
let token = store.get("token");
let sessionId = store.get("session") || newSession();
let cursor = 0;           // next event seq we need
let liveFrom = 0;         // events before this were already there when the page opened: show, don't speak
let attached = false;
let running = false;
let approval = null;      // {id, name}
let conversation = false; // hands-free: listen again after the agent finishes talking

function newSession() {
  const id = crypto.randomUUID();
  store.set("session", id);
  return id;
}

// the token can arrive in the link (#token=...); keep it and remove it from the address bar
const m = location.hash.match(/token=([^&]+)/);
if (m) { token = decodeURIComponent(m[1]); store.set("token", token); history.replaceState(null, "", location.pathname); }

// ---- api ------------------------------------------------------------------
async function api(path, opts = {}) {
  const res = await fetch("/v1" + path, {
    ...opts,
    headers: { Authorization: "Bearer " + token, "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* not json */ }
    const e = new Error(msg); e.status = res.status; throw e;
  }
  return res;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) }).then((r) => r.json());

// ---- log ------------------------------------------------------------------
const log = $("log");
function add(cls, text) {
  const d = document.createElement("div");
  d.className = cls; d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  return d;
}
function addDiff(title, preview) {
  const det = document.createElement("details");
  det.className = "diff";
  const sum = document.createElement("summary"); sum.textContent = title;
  const pre = document.createElement("pre");
  for (const line of preview.split("\n")) {
    const span = document.createElement("span");
    span.className = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
    span.textContent = line + "\n"; pre.appendChild(span);
  }
  det.append(sum, pre); log.appendChild(det); log.scrollTop = log.scrollHeight;
}
function setStatus(t) { $("status").textContent = t; }

// ---- speech out -----------------------------------------------------------
const synth = window.speechSynthesis;
let speakQueue = [];
let speaking = false;

function chunks(text) {  // Chrome cuts long utterances off, so speak sentence by sentence
  const out = [];
  for (const s of text.replace(/\s+/g, " ").match(/[^.!?]+[.!?]*/g) || []) {
    let t = s.trim();
    while (t.length > 220) { const i = t.lastIndexOf(" ", 220); out.push(t.slice(0, i > 0 ? i : 220)); t = t.slice(i > 0 ? i : 220).trim(); }
    if (t) out.push(t);
  }
  return out;
}
function speakable(text) {  // never read code aloud
  return text.replace(/```[\s\S]*?```/g, " (code on screen) ").replace(/`([^`]+)`/g, "$1").replace(/[*_#>]/g, "").trim().slice(0, 600);
}
function say(text, { droppable = false } = {}) {
  if (!synth || !$("speakToggle").checked || !text) return;
  if (droppable && (speaking || speakQueue.length)) return;  // don't narrate every file read
  speakQueue.push(...chunks(text));
  pump();
}
function pump() {
  if (speaking) return;
  const next = speakQueue.shift();
  if (!next) { afterSpeech(); return; }
  speaking = true; stopListening();
  const u = new SpeechSynthesisUtterance(next);
  u.rate = 1.05;
  u.onend = u.onerror = () => { speaking = false; pump(); };
  synth.speak(u);
}
function hush() { speakQueue = []; speaking = false; synth && synth.cancel(); }
function afterSpeech() {
  if (conversation && !running && !approval) startListening();
  else if (conversation && approval) startListening();  // waiting for yes/no
}

// ---- speech in ------------------------------------------------------------
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null;
let listening = false;

function startListening() {
  if (!Recognition || listening || speaking) return;
  rec = new Recognition();
  rec.lang = navigator.language || "en-US";
  rec.interimResults = true;
  rec.continuous = false;
  let finalText = "";
  rec.onresult = (e) => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i];
      if (r.isFinal) finalText += r[0].transcript; else interim += r[0].transcript;
    }
    setStatus("Hearing: " + (finalText + interim));
  };
  rec.onend = () => {
    listening = false; $("mic").classList.remove("live"); $("mic").textContent = conversation ? "🎤 Listening… (tap to end)" : "🎤 Talk";
    const t = finalText.trim();
    if (t) handleUtterance(t);
    else if (conversation && !speaking) setTimeout(startListening, 300);  // heard nothing: keep the line open
    updateStatus();
  };
  rec.onerror = (e) => {
    listening = false;
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      conversation = false; setStatus("Microphone blocked. Allow it in the browser (needs HTTPS or localhost).");
    }
  };
  try { rec.start(); listening = true; $("mic").classList.add("live"); setStatus("Listening…"); } catch { /* already started */ }
}
function stopListening() { if (rec && listening) { try { rec.abort(); } catch { /* ignore */ } } listening = false; $("mic").classList.remove("live"); }

// ---- what the user said ---------------------------------------------------
const YES = /^(yes|yeah|yep|yup|approve|approved|ok|okay|go ahead|do it|sure|allow|accept)\b/i;
const NO = /^(no|nope|reject|deny|don't|do not|cancel|decline)\b[\s,.:-]*/i;
const STOP = /^(stop|cancel|never ?mind|abort)\b/i;
const wordCount = (s) => s.trim().split(/\s+/).length;

async function handleUtterance(text) {
  hush();
  if (approval) {
    if (wordCount(text) <= 4 && /\balways\b/i.test(text) && YES.test(text)) return decide(true, true);
    if (wordCount(text) <= 4 && YES.test(text)) return decide(true, false);
    const no = text.match(NO);
    if (no) {
      await decide(false, false);
      const rest = text.slice(no[0].length).trim();
      if (wordCount(rest) >= 2) send(rest);
      return;
    }
    say("Say approve or reject.");
    return;
  }
  if (running && STOP.test(text) && wordCount(text) <= 3) return stop();
  send(text);
}

// ---- sending --------------------------------------------------------------
async function send(text) {
  if (!text.trim()) return;
  const workspace = $("workspace").value;
  if (!workspace) { add("note", "Pick a project folder first."); return; }
  const wasRunning = running;
  try {
    // the server decides: a command for the assistant (open Claude Code, what's happening?) or a prompt for the agent
    const r = await post("/say", {
      session_id: sessionId, workspace, message: text, mode: store.get("mode", "claude"),
      approval_mode: store.get("autoEdits") === "1" ? "autoEdits" : "ask",
    });
    running = r.handled ? r.active : true;
    updateStatus();
    attach(cursor);
  } catch (e) { running = wasRunning; fail(e); }
}

async function decide(approved, always) {
  if (!approval) return;
  const id = approval.id; approval = null; $("approval").hidden = true;
  try { await post("/approve", { id, approved, always }); } catch (e) { fail(e); }
  updateStatus();
}
async function stop() {
  hush();
  try { await post(`/sessions/${sessionId}/cancel`, {}); } catch (e) { fail(e); }
}
function fail(e) {
  if (e.status === 401) { add("note", "Token rejected. Open Settings and paste the token from `python -m app.pair`."); $("settings").hidden = false; }
  else add("note", "Error: " + e.message);
  updateStatus();
}

// ---- event stream ---------------------------------------------------------
async function attach(after) {
  if (attached) return;
  attached = true;
  let delay = 1000;
  try {
    while (true) {
      try {
        const res = await api(`/sessions/${sessionId}/events?after=${cursor ?? after}`);
        delay = 1000;
        const reader = res.body.getReader(), dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const frame = buf.slice(0, i); buf = buf.slice(i + 2);
            if (frame.startsWith("data: ")) handle(JSON.parse(frame.slice(6)));
          }
        }
        if (!running) break;  // server closed after the run ended
      } catch (e) {
        if (e.status === 401) { fail(e); break; }
        setStatus("Connection lost, reconnecting…");
      }
      if (!running) break;
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 2, 15000);
    }
  } finally { attached = false; updateStatus(); }
}

function handle(ev) {
  if (ev.seq !== undefined) cursor = Math.max(cursor, ev.type === "idle" ? ev.seq : ev.seq + 1);
  const live = (ev.seq ?? Infinity) >= liveFrom;
  switch (ev.type) {
    case "user": add("msg user", ev.text); running = true; break;
    case "steer": add("msg steer", ev.text); break;
    case "said": add("msg user", ev.text); break;  // a spoken command that wasn't a prompt
    case "voice": add("msg assistant", ev.text); if (live) say(ev.text); break;
    case "text": add("msg assistant", ev.text); if (live) say(speakable(ev.text)); break;
    case "tool_call": add("act", "▸ " + (ev.speech || ev.name)); if (live && ev.speech) say(ev.speech, { droppable: true }); break;
    case "tool_result": if (ev.is_error) add("act err", "✗ " + ev.name + " failed"); break;
    case "auto_approved": if (ev.preview) addDiff("Auto-approved: " + ev.name, ev.preview); break;
    case "status": if (ev.text) setStatus(ev.text); break;
    case "approval_request":
      approval = { id: ev.id, name: ev.name };
      $("approvalTitle").textContent = ev.name === "run_command" ? "Run this command?"
        : ev.name === "edit_file" ? "Apply this edit?" : "Allow " + ev.name + "?";
      $("approvalPreview").textContent = ev.preview || "";
      $("approval").hidden = false;
      if (live) { say(ev.speech || "Approve?"); }
      break;
    case "done": running = false; break;
    case "stopped": running = false; approval = null; $("approval").hidden = true; add("note", "Stopped."); break;
    case "error": running = false; add("note", "Error: " + ev.message); if (live) say(ev.speech); break;
    case "idle": running = false; break;
  }
  if (!running && ["done", "stopped", "error"].includes(ev.type)) { approval = null; $("approval").hidden = true; }
  updateStatus();
  if (!running && !speaking && !speakQueue.length && conversation) afterSpeech();
}

function updateStatus() {
  $("stopBtn").hidden = !running;
  if (approval) setStatus("Waiting for your approval");
  else if (running) setStatus("Working…");
  else if (!listening) setStatus("Ready");
}

// ---- setup ----------------------------------------------------------------
async function loadWorkspaces() {
  try {
    const { folders } = await (await api("/workspaces")).json();
    const sel = $("workspace"); sel.innerHTML = "";
    for (const f of folders) sel.add(new Option(f.name, f.path));
    sel.value = store.get("workspace") || (folders[0] && folders[0].path) || "";
    if (!folders.length) add("note", "No folders are allowed yet. Run `python -m app.pair --workspace <folder>` on the PC.");
  } catch (e) { fail(e); }
}

async function resume() {
  try {
    const s = await (await api(`/sessions/${sessionId}`)).json();
    liveFrom = s.next_seq;
    running = s.active;
    cursor = s.first_seq;  // replay the whole log so the conversation reappears
    attach(cursor);
    if (!s.active) updateStatus();
  } catch (e) { fail(e); }
}

$("form").addEventListener("submit", (e) => { e.preventDefault(); const t = $("text").value; $("text").value = ""; hush(); send(t); });
$("mic").addEventListener("click", () => {
  if (!Recognition) { add("note", "This browser has no speech recognition. Use the keyboard's dictation button instead."); return; }
  if (conversation) { conversation = false; stopListening(); hush(); $("mic").textContent = "🎤 Talk"; updateStatus(); return; }
  conversation = true; hush(); startListening();
});
$("stopBtn").addEventListener("click", stop);
$("approveBtn").addEventListener("click", () => decide(true, false));
$("alwaysBtn").addEventListener("click", () => decide(true, true));
$("rejectBtn").addEventListener("click", () => decide(false, false));
$("workspace").addEventListener("change", (e) => store.set("workspace", e.target.value));
$("settingsBtn").addEventListener("click", () => { $("settings").hidden = !$("settings").hidden; });
$("saveSettings").addEventListener("click", () => {
  if ($("tokenInput").value.trim()) { token = $("tokenInput").value.trim(); store.set("token", token); }
  store.set("autoEdits", $("autoEdits").checked ? "1" : "0");
  store.set("mode", $("mode").value);
  store.set("speak", $("speakToggle").checked ? "1" : "0");
  $("settings").hidden = true; $("tokenInput").value = ""; loadWorkspaces(); resume();
});
$("newChat").addEventListener("click", () => { sessionId = newSession(); cursor = 0; liveFrom = 0; log.innerHTML = ""; running = false; $("settings").hidden = true; });

$("autoEdits").checked = store.get("autoEdits") === "1";
$("mode").value = store.get("mode", "claude");
$("speakToggle").checked = store.get("speak", "1") === "1";
if (!token) $("settings").hidden = false;
else { loadWorkspaces(); resume(); }
