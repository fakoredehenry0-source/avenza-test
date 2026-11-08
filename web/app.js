'use strict';

/* ---------------------------------------------------------------------------
 * Avenza PWA client.
 *
 * Voice only drafts. The fingerprint approval is a WebAuthn assertion sent to
 * the server, which rebuilds the challenge from the stored transfer and verifies
 * it before submitting to the bank.
 * ------------------------------------------------------------------------- */

const $ = (id) => document.getElementById(id);

const STORAGE = {
  token: 'avenza.device_token',
  userId: 'avenza.user_id',
  consent: 'avenza.consent_id',
  displayName: 'avenza.display_name',
  language: 'avenza.language',
  browserStt: 'avenza.browser_stt',
};

const CONSENT_VERSION = '2026-11-08-v1';
const SPEECH_LANG = { en: 'en-NG', yo: 'yo-NG', ha: 'ha-NG', ig: 'ig-NG', pcm: 'en-NG' };
const STATE_TERMINAL = new Set(['SETTLED', 'FAILED', 'CANCELLED', 'EXPIRED']);

const state = {
  config: null,
  health: null,
  ws: null,
  session: null,
  deviceToken: null,
  userId: null,
  consentId: null,
  language: 'en',
  displayName: '',
  connected: false,
  running: false,
  listeningDesired: false,
  listening: false,
  speaking: false,
  pendingApproval: null,
  locked: false,
  lastState: null,
  speechQueue: [],
  currentAudio: null,
  audioContext: null,
  mic: null,
  vad: null,
  recognition: null,
  useBrowserStt: true,
  logLines: [],
  promptTexts: {},
};

/* ---------------------------------------------------------------------------
 * Small helpers
 * ------------------------------------------------------------------------- */

function show(el) { if (el) el.hidden = false; }
function hide(el) { if (el) el.hidden = true; }
function setText(el, text) { if (el) el.textContent = text; }

function setStatus(words, detail) {
  setText($('statusWords'), words);
  if (detail !== undefined) setText($('statusDetail'), detail);
}

function setDetail(detail) { setText($('statusDetail'), detail); }

function setTranscript(text) {
  setText($('transcriptText'), text || '—');
}

function addLog(kind, message, data) {
  const stamp = new Date().toISOString().slice(11, 23);
  let line = `${stamp} [${kind}] ${message}`;
  if (data !== undefined) {
    try { line += ` ${JSON.stringify(data)}`; } catch (_) { /* ignore */ }
  }
  state.logLines.push(line);
  if (state.logLines.length > 300) state.logLines.shift();
  const log = $('debugLog');
  if (log) {
    log.textContent = state.logLines.join('\n');
    log.scrollTop = log.scrollHeight;
  }
}

function b64urlToBuf(value) {
  const s = String(value || '').replace(/-/g, '+').replace(/_/g, '/');
  const pad = '='.repeat((4 - (s.length % 4)) % 4);
  const bin = atob(s + pad);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

function bufToB64url(buf) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (state.deviceToken) headers.Authorization = `Bearer ${state.deviceToken}`;
  const response = await fetch(path, { ...options, headers });
  const raw = await response.text();
  let data = null;
  try { data = raw ? JSON.parse(raw) : null; } catch (_) { data = raw; }
  if (!response.ok) {
    const detail = data && data.detail ? data.detail : `HTTP ${response.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

function persist() {
  localStorage.setItem(STORAGE.token, state.deviceToken || '');
  localStorage.setItem(STORAGE.userId, state.userId || '');
  localStorage.setItem(STORAGE.consent, state.consentId || '');
  localStorage.setItem(STORAGE.displayName, state.displayName || '');
  localStorage.setItem(STORAGE.language, state.language || 'en');
  localStorage.setItem(STORAGE.browserStt, state.useBrowserStt ? '1' : '0');
}

function restore() {
  state.deviceToken = localStorage.getItem(STORAGE.token) || null;
  state.userId = localStorage.getItem(STORAGE.userId) || null;
  state.consentId = localStorage.getItem(STORAGE.consent) || null;
  state.displayName = localStorage.getItem(STORAGE.displayName) || '';
  state.language = localStorage.getItem(STORAGE.language) || 'en';
  state.useBrowserStt = localStorage.getItem(STORAGE.browserStt) !== '0';
}

/* ---------------------------------------------------------------------------
 * WebAuthn encoding
 * ------------------------------------------------------------------------- */

function decodeCreationOptions(publicKey) {
  return {
    ...publicKey,
    challenge: b64urlToBuf(publicKey.challenge),
    user: { ...publicKey.user, id: b64urlToBuf(publicKey.user.id) },
    excludeCredentials: (publicKey.excludeCredentials || []).map((c) => ({
      ...c,
      id: b64urlToBuf(c.id),
    })),
  };
}

function decodeRequestOptions(publicKey) {
  return {
    ...publicKey,
    challenge: b64urlToBuf(publicKey.challenge),
    allowCredentials: (publicKey.allowCredentials || []).map((c) => ({
      ...c,
      id: b64urlToBuf(c.id),
    })),
  };
}

function serializeRegistration(credential) {
  return {
    id: credential.id,
    rawId: bufToB64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufToB64url(credential.response.clientDataJSON),
      attestationObject: bufToB64url(credential.response.attestationObject),
      transports: credential.response.getTransports ? credential.response.getTransports() : [],
    },
    clientExtensionResults: credential.getClientExtensionResults
      ? credential.getClientExtensionResults()
      : {},
  };
}

function serializeAssertion(credential) {
  const response = credential.response;
  return {
    id: credential.id,
    rawId: bufToB64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufToB64url(response.clientDataJSON),
      authenticatorData: bufToB64url(response.authenticatorData),
      signature: bufToB64url(response.signature),
      userHandle: response.userHandle ? bufToB64url(response.userHandle) : null,
    },
    clientExtensionResults: credential.getClientExtensionResults
      ? credential.getClientExtensionResults()
      : {},
  };
}

/* ---------------------------------------------------------------------------
 * UI state
 * ------------------------------------------------------------------------- */

function updateBadges() {
  const bank = state.config ? state.config.bank : null;
  const bankBadge = $('bankBadge');
  if (bank) {
    bankBadge.textContent = bank.simulated ? 'Bank: simulated' : 'Bank: sandbox';
    bankBadge.className = `badge ${bank.simulated ? 'badge-sim' : 'badge-online'}`;
    setText($('footerBank'), bank.simulated ? 'simulated (no real money moves)' : 'the organisers’ sandbox');
  }
  const conn = $('connBadge');
  conn.textContent = state.connected ? 'Connected' : 'Offline';
  conn.className = `badge ${state.connected ? 'badge-online' : 'badge-offline'}`;
}

function showEnrol() {
  show($('enrolPanel'));
  hide($('sessionPanel'));
  hide($('approvalPanel'));
  setStatus('Not started', 'Record consent and enrol a fingerprint to begin.');
}

function showSession() {
  hide($('enrolPanel'));
  show($('sessionPanel'));
  setStatus('Ready', 'Tap Start listening, then speak a request.');
}

function setTransfer(transfer) {
  state.lastState = transfer;
  setText($('amount'), transfer.amount_display || '—');
  setText($('recipient'), transfer.recipient || '—');
  setText($('reference'), transfer.reference || '—');
  if (transfer.display) setStatus(transfer.display);
  const flags = transfer.risk_flags || [];
  const risk = $('riskBanner');
  if (flags.length) {
    const messages = {
      duplicate: 'This looks like a recent payment to the same account for the same amount.',
      new_recipient_large: 'New recipient and a large amount. A short safety pause applies.',
      name_mismatch: 'The bank’s verified name differs from the name heard. The bank name is used.',
    };
    risk.textContent = flags.map((f) => messages[f] || f).join(' ');
    show(risk);
  } else {
    hide(risk);
  }
  if (STATE_TERMINAL.has(transfer.state)) {
    state.pendingApproval = null;
    hide($('approvalPanel'));
  }
}

function setEnrolStatus(text, isError = false) {
  const el = $('enrolStatus');
  el.textContent = text;
  el.style.color = isError ? 'var(--danger)' : '';
}

function setApprovalStatus(text, isError = false) {
  const el = $('approvalStatus');
  el.textContent = text;
  el.style.color = isError ? 'var(--danger)' : '';
}

function populateLanguages() {
  const select = $('language');
  if (!state.config) return;
  const available = new Set(state.config.languages.available || ['en']);
  Array.from(select.options).forEach((option) => {
    if (!available.has(option.value)) option.remove();
  });
  select.value = state.language || 'en';
}

/* ---------------------------------------------------------------------------
 * Speech output
 * ------------------------------------------------------------------------- */

function speechLang() {
  return SPEECH_LANG[state.language] || 'en-NG';
}

function playAudioUrl(url) {
  return new Promise((resolve) => {
    const audio = new Audio(url);
    state.currentAudio = audio;
    audio.onended = () => { state.currentAudio = null; resolve(); };
    audio.onerror = () => { state.currentAudio = null; resolve(); };
    audio.play().catch(() => { state.currentAudio = null; resolve(); });
  });
}

function speakText(text) {
  return new Promise((resolve) => {
    if (!('speechSynthesis' in window) || !text) { resolve(); return; }
    try { window.speechSynthesis.cancel(); } catch (_) { /* ignore */ }
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = speechLang();
    utterance.rate = 0.98;
    utterance.pitch = 1.0;
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    utterance.onend = finish;
    utterance.onerror = finish;
    window.speechSynthesis.speak(utterance);
    // Some mobile engines do not fire onend reliably.
    setTimeout(finish, Math.min(30000, 2500 + text.length * 90));
  });
}

async function playPrompt(event) {
  if (event.audio_url) {
    await playAudioUrl(event.audio_url);
  } else {
    await speakText(event.text || '');
  }
}

function enqueueSpeech(event) {
  state.speechQueue.push(event);
  drainSpeech();
}

async function drainSpeech() {
  if (state.speaking) return;
  state.speaking = true;
  // Pause capture for half-duplex playback, but remember that the session
  // still wants to listen so we can resume afterwards.
  stopListening(false, true);
  try {
    while (state.speechQueue.length) {
      const event = state.speechQueue.shift();
      setDetail(event.text || '');
      addLog('prompt', event.id || 'live', { text: event.text });
      await playPrompt(event);
    }
  } finally {
    state.speaking = false;
    if (state.listeningDesired && !state.pendingApproval && state.running) {
      resumeListening();
    }
  }
}

function stopSpeaking() {
  try { window.speechSynthesis.cancel(); } catch (_) { /* ignore */ }
  if (state.currentAudio) {
    try { state.currentAudio.pause(); } catch (_) { /* ignore */ }
    state.currentAudio = null;
  }
  state.speechQueue = [];
}

function tone() {
  if (!state.audioContext) return;
  try {
    const osc = state.audioContext.createOscillator();
    const gain = state.audioContext.createGain();
    osc.type = 'sine';
    osc.frequency.value = 660;
    gain.gain.setValueAtTime(0.0001, state.audioContext.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.05, state.audioContext.currentTime + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, state.audioContext.currentTime + 0.09);
    osc.connect(gain).connect(state.audioContext.destination);
    osc.start();
    osc.stop(state.audioContext.currentTime + 0.1);
  } catch (_) { /* ignore */ }
}

/* ---------------------------------------------------------------------------
 * Microphone, VAD and browser speech recognition
 * ------------------------------------------------------------------------- */

function downsampleTo16k(input, inputRate) {
  if (inputRate === 16000) {
    const out = new Int16Array(input.length);
    for (let i = 0; i < input.length; i += 1) {
      const s = Math.max(-1, Math.min(1, input[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }
  const ratio = inputRate / 16000;
  const length = Math.floor(input.length / ratio);
  const out = new Int16Array(length);
  for (let i = 0; i < length; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j += 1) sum += input[j];
    const s = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function rms(samples) {
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
  return Math.sqrt(sum / Math.max(1, samples.length));
}

function sendJson(payload) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(payload));
    return true;
  }
  addLog('error', 'WebSocket is not open');
  return false;
}

function sendPcm(int16) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(int16.buffer);
  }
}

async function initMic() {
  if (state.mic) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error('This browser cannot access the microphone.');
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });
  if (!state.audioContext) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    state.audioContext = new Ctx();
  }
  await state.audioContext.resume();
  const source = state.audioContext.createMediaStreamSource(stream);
  const processor = state.audioContext.createScriptProcessor(4096, 1, 1);
  const sink = state.audioContext.createGain();
  sink.gain.value = 0.00001;
  source.connect(processor);
  processor.connect(sink);
  sink.connect(state.audioContext.destination);
  processor.onaudioprocess = onAudioFrame;
  state.mic = { stream, source, processor, sink };
  state.vad = {
    speaking: false,
    speechStart: 0,
    silenceStart: 0,
    voiceFrames: 0,
    preRoll: [],
    threshold: 0.014,
    silenceMs: 800,
    minSpeechMs: 250,
    maxMs: 10000,
    preRollMs: 500,
  };
  addLog('mic', 'microphone ready');
}

function onAudioFrame(event) {
  if (!state.running || state.speaking || state.pendingApproval || !state.listening) return;
  if (state.useBrowserStt) return;
  const input = event.inputBuffer.getChannelData(0);
  const sampleRate = event.inputBuffer.sampleRate;
  const level = rms(input);
  const pcm = downsampleTo16k(input, sampleRate);
  const now = performance.now();
  const vad = state.vad;
  if (!vad) return;

  if (!vad.speaking) {
    vad.preRoll.push({ pcm, at: now });
    while (vad.preRoll.length && now - vad.preRoll[0].at > vad.preRollMs) vad.preRoll.shift();
    if (level > vad.threshold) {
      vad.voiceFrames += 1;
      if (vad.voiceFrames >= 2) {
        vad.speaking = true;
        vad.speechStart = now;
        vad.silenceStart = 0;
        sendJson({ type: 'utterance.start', sample_rate: 16000 });
        for (const frame of vad.preRoll) sendPcm(frame.pcm);
        vad.preRoll = [];
        addLog('vad', 'speech start');
      }
    } else {
      vad.voiceFrames = 0;
    }
    return;
  }

  sendPcm(pcm);
  if (level < vad.threshold) {
    if (!vad.silenceStart) vad.silenceStart = now;
    if (now - vad.silenceStart >= vad.silenceMs && now - vad.speechStart >= vad.minSpeechMs) {
      endUtterance('silence');
    }
  } else {
    vad.silenceStart = 0;
  }
  if (now - vad.speechStart > vad.maxMs) endUtterance('max_length');
}

function endUtterance(reason) {
  if (!state.vad || !state.vad.speaking) return;
  state.vad.speaking = false;
  state.vad.preRoll = [];
  state.vad.voiceFrames = 0;
  state.listening = false;
  state.listeningDesired = false;
  hide($('listenIndicator'));
  sendJson({ type: 'utterance.end' });
  addLog('vad', `speech end (${reason})`);
}

function startRecognition() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    state.useBrowserStt = false;
    $('browserSttCheck').checked = false;
    addLog('stt', 'browser speech recognition unavailable; using server STT');
    initMic().catch((err) => setDetail(err.message));
    return;
  }
  try { if (state.recognition) state.recognition.stop(); } catch (_) { /* ignore */ }
  const rec = new SR();
  rec.continuous = false;
  rec.interimResults = true;
  rec.lang = speechLang();
  let finalText = '';
  rec.onresult = (event) => {
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) finalText += transcript;
      else interim += transcript;
    }
    if (interim) {
      setTranscript(interim);
      sendJson({ type: 'utterance.text', text: interim, final: false });
    }
    if (finalText.trim()) {
      const text = finalText.trim();
      setTranscript(text);
      state.listening = false;
      state.listeningDesired = false;
      hide($('listenIndicator'));
      sendJson({ type: 'utterance.text', text, final: true });
      addLog('stt', 'browser final', { text });
    }
  };
  rec.onerror = (event) => {
    addLog('stt', `recognition error: ${event.error}`);
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      state.listeningDesired = false;
      setDetail('Microphone permission was denied. Use the text box or allow the microphone.');
    }
  };
  rec.onend = () => {
    if (state.running && state.listeningDesired && !state.speaking && !state.pendingApproval) {
      setTimeout(() => {
        if (state.running && state.listeningDesired && !state.speaking && !state.pendingApproval) {
          try { rec.start(); } catch (_) { /* already started */ }
        }
      }, 350);
    }
  };
  rec.start();
  state.recognition = rec;
}

async function resumeListening() {
  if (!state.running || state.speaking || state.pendingApproval) return;
  if (state.listening) return;
  state.listening = true;
  state.listeningDesired = true;
  show($('listenIndicator'));
  tone();
  if (state.useBrowserStt) {
    startRecognition();
  } else {
    try {
      await initMic();
      if (state.vad) {
        state.vad.speaking = false;
        state.vad.preRoll = [];
        state.vad.voiceFrames = 0;
      }
    } catch (err) {
      state.listening = false;
      hide($('listenIndicator'));
      setDetail(`Microphone error: ${err.message}`);
      addLog('error', 'microphone failed', { message: err.message });
    }
  }
}

function stopListening(sendEnd = false, keepDesired = false) {
  state.listening = false;
  if (!keepDesired) state.listeningDesired = false;
  hide($('listenIndicator'));
  if (sendEnd && state.vad && state.vad.speaking) {
    state.vad.speaking = false;
    sendJson({ type: 'utterance.end' });
  }
  if (state.recognition) {
    try { state.recognition.stop(); } catch (_) { /* ignore */ }
    state.recognition = null;
  }
}

/* ---------------------------------------------------------------------------
 * Session and WebSocket
 * ------------------------------------------------------------------------- */

async function startSession() {
  if (!state.deviceToken || !state.consentId) {
    showEnrol();
    return;
  }
  try {
    if (!state.audioContext) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      state.audioContext = new Ctx();
    }
    await state.audioContext.resume();
  } catch (_) { /* audio may still work later */ }
  state.running = true;
  hide($('startBtn'));
  show($('stopBtn'));
  connectWs();
}

function connectWs() {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${scheme}://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = () => {
    state.connected = true;
    updateBadges();
    addLog('ws', 'connected');
    sendJson({
      type: 'session.start',
      device_token: state.deviceToken,
      consent_id: state.consentId,
      language: state.language,
    });
  };
  ws.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch (_) { return; }
    handleEvent(data).catch((err) => addLog('error', 'event handler failed', { message: err.message }));
  };
  ws.onclose = () => {
    state.connected = false;
    state.running = false;
    state.listening = false;
    state.listeningDesired = false;
    updateBadges();
    hide($('listenIndicator'));
    hide($('stopBtn'));
    show($('startBtn'));
    setStatus('Disconnected', 'Start again to reconnect. No money moved.');
    addLog('ws', 'closed');
  };
  ws.onerror = () => addLog('ws', 'socket error');
}

async function handleEvent(event) {
  addLog('event', event.type, event.type === 'transcript.final' ? { text: event.text } : undefined);
  switch (event.type) {
    case 'session.ready':
      state.session = event.session_id;
      state.promptTexts = event.prompts || {};
      state.language = event.language || state.language;
      $('devApproveBtn').hidden = !event.dev_approval_allowed;
      $('devEnrolBtn').hidden = !event.dev_approval_allowed;
      showSession();
      updateBadges();
      setStatus('Ready', `Session started. Bank: ${event.bank.simulated ? 'simulated' : 'sandbox'}.`);
      break;

    case 'prompt.play':
      enqueueSpeech(event);
      break;

    case 'listening':
      state.listeningDesired = true;
      if (!state.speaking && !state.pendingApproval) resumeListening();
      break;

    case 'transcript.partial':
      setTranscript(event.text);
      break;

    case 'transcript.final':
      setTranscript(event.text);
      if (event.timings) addLog('latency', 'transcript', event.timings);
      break;

    case 'state.changed':
      setTransfer(event);
      break;

    case 'approval.request':
      state.pendingApproval = event;
      state.listeningDesired = false;
      stopListening(false);
      show($('approvalPanel'));
      const t = event.transfer || {};
      $('approvalSummary').textContent =
        `${t.amount_display || ''} to ${t.recipient || 'the verified recipient'} ` +
        `(${t.bank_name || t.bank_code || ''} ••••${t.account_last4 || ''}). ` +
        `Reference will be issued after settlement. Expires in ${event.expires_in_s || 60} seconds.`;
      setApprovalStatus('');
      setStatus('Fingerprint required', 'Voice cannot approve. Use your fingerprint on this device.');
      $('devApproveBtn').hidden = !(state.config && state.config.dev_approval_allowed);
      break;

    case 'error':
      addLog('server-error', `${event.code}: ${event.message}`);
      if (event.code === 'transfers_locked') {
        state.locked = true;
        show($('unlockBtn'));
      }
      break;

    case 'history.data':
      addLog('history', event.period, { count: (event.transactions || []).length });
      break;

    case 'lock.changed':
      state.locked = !!event.locked;
      $('unlockBtn').hidden = !state.locked;
      setStatus(state.locked ? 'Transfers locked' : 'Transfers unlocked',
        state.locked ? 'Only your fingerprint can unlock transfers.' : 'You can send again.');
      break;

    case 'audio.stream':
      playPcmStream(event).catch((err) => addLog('tts', 'pcm playback failed', { message: err.message }));
      break;

    case 'pong':
      break;

    default:
      break;
  }
}

async function playPcmStream(event) {
  if (!state.audioContext || !event.data) return;
  const bytes = Uint8Array.from(atob(event.data), (c) => c.charCodeAt(0));
  const view = new DataView(bytes.buffer);
  const samples = new Float32Array(bytes.length / 2);
  for (let i = 0; i < samples.length; i += 1) {
    samples[i] = view.getInt16(i * 2, true) / 32768;
  }
  const buffer = state.audioContext.createBuffer(1, samples.length, event.sample_rate || 24000);
  buffer.copyToChannel(samples, 0);
  const source = state.audioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(state.audioContext.destination);
  state.speaking = true;
  await new Promise((resolve) => {
    source.onended = resolve;
    source.start();
  });
  state.speaking = false;
}

/* ---------------------------------------------------------------------------
 * Enrolment and consent
 * ------------------------------------------------------------------------- */

async function recordConsent(language) {
  const consent = await api('/consents', {
    method: 'POST',
    body: JSON.stringify({ wording_version: CONSENT_VERSION, language }),
  });
  state.consentId = consent.consent_id;
  persist();
}

async function enrolWithPasskey() {
  const displayName = $('displayName').value.trim();
  const language = $('language').value;
  if (!displayName) { setEnrolStatus('Enter a display name.', true); return; }
  if (!$('consentCheck').checked) { setEnrolStatus('Record your consent first.', true); return; }
  if (!window.PublicKeyCredential || !navigator.credentials) {
    if (state.config && state.config.dev_approval_allowed) {
      setEnrolStatus('This browser has no passkey support. Falling back to dev enrolment.', true);
      await devEnrol();
      return;
    }
    setEnrolStatus('Passkeys need localhost or HTTPS and a platform authenticator.', true);
    return;
  }

  $('enrolBtn').disabled = true;
  try {
    setEnrolStatus('Opening the fingerprint prompt…');
    const options = await api('/devices/register/options', {
      method: 'POST',
      body: JSON.stringify({ display_name: displayName, language }),
    });
    const publicKey = decodeCreationOptions(options.publicKey);
    let credential;
    try {
      credential = await navigator.credentials.create({ publicKey });
    } catch (err) {
      throw new Error(`Fingerprint enrolment was cancelled or failed: ${err.message}`);
    }
    setEnrolStatus('Verifying the passkey…');
    const verified = await api('/devices/register/verify', {
      method: 'POST',
      body: JSON.stringify({
        challenge_id: options.challenge_id,
        credential: serializeRegistration(credential),
      }),
    });
    state.deviceToken = verified.device_token;
    state.userId = verified.user_id;
    state.displayName = displayName;
    state.language = language;
    await recordConsent(language);
    persist();
    setEnrolStatus('Enrolled. You can start listening.');
    showSession();
  } catch (err) {
    setEnrolStatus(err.message, true);
    addLog('enrol', 'failed', { message: err.message });
  } finally {
    $('enrolBtn').disabled = false;
  }
}

async function devEnrol() {
  const displayName = $('displayName').value.trim() || 'Demo customer';
  const language = $('language').value;
  try {
    const result = await api('/dev/devices/register', {
      method: 'POST',
      body: JSON.stringify({ display_name: displayName, language }),
    });
    state.deviceToken = result.device_token;
    state.userId = result.user_id;
    state.displayName = displayName;
    state.language = language;
    await recordConsent(language);
    persist();
    setEnrolStatus('Dev enrolment complete. Fingerprint approval is simulated.');
    showSession();
  } catch (err) {
    setEnrolStatus(err.message, true);
  }
}

/* ---------------------------------------------------------------------------
 * Approval and lock
 * ------------------------------------------------------------------------- */

async function approveWithFingerprint() {
  const pending = state.pendingApproval;
  if (!pending) return;
  if (!window.PublicKeyCredential || !navigator.credentials) {
    setApprovalStatus('This browser has no WebAuthn support. Use a dev approval if enabled.', true);
    return;
  }
  try {
    setApprovalStatus('Waiting for your fingerprint…');
    const publicKey = decodeRequestOptions(pending.options);
    const credential = await navigator.credentials.get({ publicKey });
    setApprovalStatus('Verifying with the backend…');
    sendJson({ type: 'approval.response', credential: serializeAssertion(credential) });
    hide($('approvalPanel'));
    state.pendingApproval = null;
    setStatus('Verifying fingerprint', 'The backend is checking the approval and submitting to the bank.');
  } catch (err) {
    setApprovalStatus(`Fingerprint was not read: ${err.message}`, true);
  }
}

function devApprove() {
  if (!state.pendingApproval) return;
  sendJson({ type: 'approval.response', dev: true });
  hide($('approvalPanel'));
  state.pendingApproval = null;
  setStatus('Dev approval', 'Submitting with the test bypass. Never use this in production.');
}

function cancelApproval() {
  sendJson({ type: 'utterance.text', text: 'cancel', final: true });
  hide($('approvalPanel'));
  state.pendingApproval = null;
  setStatus('Cancelling', 'No money moves unless the bank has already settled.');
}

async function engageLock() {
  try {
    await api('/locks', { method: 'POST' });
    state.locked = true;
    show($('unlockBtn'));
    setStatus('Transfers locked', 'Only your fingerprint can unlock transfers.');
    addLog('lock', 'engaged');
  } catch (err) {
    setStatus('Lock failed', err.message);
  }
}

async function unlockWithFingerprint() {
  try {
    const options = await api('/locks/options', { method: 'POST' });
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error('WebAuthn is not available in this browser.');
    }
    const publicKey = decodeRequestOptions(options.publicKey);
    const credential = await navigator.credentials.get({ publicKey });
    await api('/locks/unlock', {
      method: 'POST',
      body: JSON.stringify({ challenge_id: options.challenge_id, credential: serializeAssertion(credential) }),
    });
    state.locked = false;
    hide($('unlockBtn'));
    setStatus('Transfers unlocked', 'You can send again.');
  } catch (err) {
    setStatus('Unlock failed', `${err.message} Transfers remain locked.`);
  }
}

/* ---------------------------------------------------------------------------
 * Demo bank controls
 * ------------------------------------------------------------------------- */

async function applyBankMode() {
  try {
    const mode = $('bankMode').value;
    const delay = Number($('bankDelay').value || 0);
    const result = await api('/dev/bank/mode', {
      method: 'POST',
      body: JSON.stringify({ mode, delay_ms: delay }),
    });
    addLog('bank', 'mode set', result.bank);
    setDetail(`Mock bank mode: ${mode}${delay ? `, delay ${delay} ms` : ''}.`);
  } catch (err) {
    addLog('bank', 'mode failed', { message: err.message });
    setDetail(`Bank mode failed: ${err.message}`);
  }
}

function renderDemoAccounts() {
  const box = $('demoAccounts');
  box.innerHTML = '';
  const accounts = (state.config && state.config.demo_accounts) || [];
  if (!accounts.length) return;
  const title = document.createElement('p');
  title.className = 'label';
  title.textContent = 'Mock bank accounts';
  box.appendChild(title);
  for (const account of accounts) {
    const row = document.createElement('div');
    row.className = 'account';
    const left = document.createElement('span');
    const name = document.createElement('strong');
    name.textContent = account.account_name;
    left.appendChild(name);
    left.appendChild(document.createTextNode(` · ${account.bank_name}`));
    const right = document.createElement('span');
    right.textContent = account.account_number;
    row.appendChild(left);
    row.appendChild(right);
    row.addEventListener('click', () => {
      $('textInput').value =
        `Send five thousand naira to ${account.bank_name} account ${account.account_number}`;
      $('textInput').focus();
    });
    box.appendChild(row);
  }
}

/* ---------------------------------------------------------------------------
 * Wiring
 * ------------------------------------------------------------------------- */

function bindEvents() {
  $('consentCheck').addEventListener('change', () => {
    $('enrolBtn').disabled = !$('consentCheck').checked;
  });
  $('enrolBtn').addEventListener('click', enrolWithPasskey);
  $('devEnrolBtn').addEventListener('click', devEnrol);
  $('startBtn').addEventListener('click', startSession);
  $('stopBtn').addEventListener('click', () => {
    stopListening(true);
    stopSpeaking();
    state.running = false;
    sendJson({ type: 'session.end' });
    if (state.ws) state.ws.close();
    hide($('stopBtn'));
    show($('startBtn'));
    setStatus('Stopped', 'Session ended. No money moved.');
  });

  $('textForm').addEventListener('submit', (event) => {
    event.preventDefault();
    const text = $('textInput').value.trim();
    if (!text) return;
    setTranscript(text);
    sendJson({ type: 'utterance.text', text, final: true });
    $('textInput').value = '';
    state.listeningDesired = false;
    stopListening(false);
  });

  document.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const text = chip.dataset.text || chip.textContent;
      $('textInput').value = text;
      $('textForm').requestSubmit();
    });
  });

  $('approveBtn').addEventListener('click', approveWithFingerprint);
  $('devApproveBtn').addEventListener('click', devApprove);
  $('cancelApprovalBtn').addEventListener('click', cancelApproval);
  $('lockBtn').addEventListener('click', engageLock);
  $('unlockBtn').addEventListener('click', unlockWithFingerprint);

  $('browserSttCheck').addEventListener('change', () => {
    state.useBrowserStt = $('browserSttCheck').checked;
    persist();
    if (state.running) {
      stopListening(false);
      state.listeningDesired = true;
      resumeListening();
    }
  });

  $('debugCheck').addEventListener('change', () => {
    $('debugPanel').hidden = !$('debugCheck').checked;
  });

  $('applyBankMode').addEventListener('click', applyBankMode);
  $('clearLog').addEventListener('click', () => {
    state.logLines = [];
    $('debugLog').textContent = '';
  });

  window.addEventListener('beforeunload', () => {
    if (state.ws) state.ws.close();
  });
}

async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  try {
    await navigator.serviceWorker.register('/sw.js');
  } catch (err) {
    addLog('pwa', 'service worker registration failed', { message: err.message });
  }
}

async function init() {
  restore();
  $('browserSttCheck').checked = state.useBrowserStt;
  bindEvents();
  try {
    state.config = await api('/config');
    state.health = await api('/health');
  } catch (err) {
    setStatus('Backend unreachable', `Start the server and reload. ${err.message}`);
    addLog('error', 'config fetch failed', { message: err.message });
    return;
  }
  state.language = state.language || 'en';
  populateLanguages();
  $('displayName').value = state.displayName || '';
  $('language').value = state.language;
  updateBadges();
  renderDemoAccounts();
  $('devEnrolBtn').hidden = !state.config.dev_approval_allowed;
  $('devApproveBtn').hidden = !state.config.dev_approval_allowed;
  addLog('app', 'loaded', {
    bank: state.config.bank,
    languages: state.config.languages,
    devApproval: state.config.dev_approval_allowed,
  });
  if (state.deviceToken && state.consentId) {
    showSession();
    setStatus('Ready', 'Tap Start listening to open a consented session.');
  } else {
    showEnrol();
  }
  registerServiceWorker();
}

window.addEventListener('DOMContentLoaded', init);
