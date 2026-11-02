(function () {
  const vscode = acquireVsCodeApi();
  const $ = (id) => document.getElementById(id);
  const log = $('log');
  const input = $('input');
  const sendBtn = $('send');
  const modelSel = $('model');
  const empty = $('empty');
  const statusDot = $('status');

  let running = false;
  let thinkingEl = null;
  const openTools = []; // tool chips still waiting for their result
  const approvalCards = {}; // approval id -> function that marks the card answered

  // ---------- helpers ----------
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const el = (tag, cls, html) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  };
  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const scrollDown = () => { log.scrollTop = log.scrollHeight; };
  function append(node) {
    const stick = nearBottom();
    if (thinkingEl) log.insertBefore(node, thinkingEl); else log.appendChild(node);
    empty.style.display = 'none';
    if (stick) scrollDown();
  }

  // ---------- minimal, safe markdown ----------
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  function codeBlock(code, lang) {
    return '<div class="code"><div class="code-head"><span>' + esc(lang || 'code') +
      '</span><button class="copy">Copy</button></div><pre><code>' + esc(code) + '</code></pre></div>';
  }

  function blocks(text) {
    let html = '';
    let list = null; // 'ul' | 'ol'
    let para = [];
    const flushPara = () => { if (para.length) { html += '<p>' + para.map(inline).join('<br>') + '</p>'; para = []; } };
    const closeList = () => { if (list) { html += '</' + list + '>'; list = null; } };
    for (const line of text.split('\n')) {
      let m;
      if ((m = line.match(/^(#{1,3})\s+(.*)/))) { flushPara(); closeList(); html += '<h3>' + inline(m[2]) + '</h3>'; }
      else if ((m = line.match(/^\s*[-*]\s+(.*)/))) { flushPara(); if (list !== 'ul') { closeList(); html += '<ul>'; list = 'ul'; } html += '<li>' + inline(m[1]) + '</li>'; }
      else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { flushPara(); if (list !== 'ol') { closeList(); html += '<ol>'; list = 'ol'; } html += '<li>' + inline(m[1]) + '</li>'; }
      else if (!line.trim()) { flushPara(); closeList(); }
      else { closeList(); para.push(line); }
    }
    flushPara(); closeList();
    return html;
  }

  function markdown(text) {
    return text.split('```').map((part, i) => {
      if (i % 2 === 0) return blocks(part);
      const nl = part.indexOf('\n');
      const lang = nl >= 0 ? part.slice(0, nl).trim() : '';
      const code = (nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, '');
      return codeBlock(code, lang);
    }).join('');
  }

  // Text of the "Working…" indicator at the bottom of the chat.
  function setStatus(text) {
    if (thinkingEl) thinkingEl.querySelector('span').textContent = text;
  }

  // ---------- message renderers ----------
  function addUser(text) {
    append(el('div', 'msg user', esc(text)));
  }

  function addAssistant(text) {
    const wrap = el('div', 'msg assistant');
    wrap.appendChild(el('div', 'avatar', 'AI'));
    wrap.appendChild(el('div', 'bubble', markdown(text)));
    append(wrap);
  }

  const VERBS = { read_file: 'Read', list_dir: 'List', search: 'Search', edit_file: 'Edit', run_command: 'Run' };
  function toolTarget(name, input) {
    return input.path || input.pattern || input.command || '';
  }

  function addTool(name, input) {
    const chip = el('div', 'tool');
    chip.innerHTML = '<span class="state"><span class="spin"></span></span><span class="verb">' +
      esc(VERBS[name] || name) + '</span><span class="target">' + esc(toolTarget(name, input || {})) + '</span>';
    chip.dataset.name = name;
    openTools.push(chip);
    append(chip);
  }

  function finishTool(name, failed) {
    const i = openTools.findIndex((c) => c.dataset.name === name);
    if (i < 0) return;
    const chip = openTools.splice(i, 1)[0];
    chip.classList.add(failed ? 'failed' : 'done');
    chip.querySelector('.state').textContent = failed ? '✕' : '✓';
  }

  function renderDiff(text) {
    const pre = el('div', 'diff');
    text.split('\n').forEach((line) => {
      let cls = 'ln';
      if (line.startsWith('+++') || line.startsWith('---')) cls += ' hunk';
      else if (line.startsWith('@@')) cls += ' hunk';
      else if (line.startsWith('+')) cls += ' add';
      else if (line.startsWith('-')) cls += ' del';
      pre.appendChild(el('span', cls, esc(line) || ' '));
    });
    return pre;
  }

  function addApproval(m) {
    const isEdit = m.name === 'edit_file';
    const card = el('div', 'approval');
    const pathMatch = isEdit && /^\+\+\+ (.+)$/m.exec(m.preview || '');
    const head = el('div', 'approval-head',
      '<span>' + (isEdit ? '✎ Edit file' : '⌘ Run command') + '</span>' +
      (pathMatch ? '<span class="path">' + esc(pathMatch[1]) + '</span>' : '') +
      '<span class="badge">Awaiting approval</span>');
    card.appendChild(head);

    const preview = m.preview || '';
    if (isEdit) card.appendChild(renderDiff(preview));
    else card.appendChild(el('pre', 'cmd' + (preview.startsWith('BLOCKED') ? ' blocked' : ''), esc(preview)));

    const actions = el('div', 'actions');
    const yes = el('button', 'btn primary', 'Approve');
    const no = el('button', 'btn secondary', 'Reject');
    actions.append(yes, no);
    card.appendChild(actions);

    const settle = (ok) => {
      setStatus('Working…');
      actions.remove();
      card.classList.add(ok ? 'approved' : 'rejected');
      head.querySelector('.badge').textContent = ok ? 'Approved' : 'Rejected';
    };
    approvalCards[m.id] = settle; // lets a notification answer update this card
    const answer = (ok) => {
      vscode.postMessage({ type: 'approve', id: m.id, approved: ok });
      settle(ok);
    };
    setStatus('Waiting for your approval ↑');
    yes.onclick = () => answer(true);
    no.onclick = () => answer(false);
    append(card);
  }

  function friendlyError(msg) {
    msg = String(msg);
    if (/429|RESOURCE_EXHAUSTED|rate.?limit/i.test(msg)) {
      const s = /retry in ([\d.]+)s/i.exec(msg);
      return ['Rate limit reached',
        'The model provider\'s quota was exceeded' + (s ? ' - try again in about ' + Math.ceil(parseFloat(s[1])) + 's' : '') +
        '. You can also switch models from the dropdown.'];
    }
    if (/Backend unreachable|Failed to fetch|ECONNREFUSED/i.test(msg)) {
      return ['Backend not reachable', 'Start it with: uvicorn app.main:app --host 127.0.0.1 --port 8765 (from the agent-backend folder).'];
    }
    if (/API_KEY|api key|UNAUTHENTICATED|PERMISSION_DENIED/i.test(msg)) {
      return ['API key problem', 'Check the key for this model in agent-backend/.env, then restart the backend.'];
    }
    if (/NOT_FOUND|404/.test(msg)) {
      return ['Model not found', 'The model name may be retired. Update it in agent-backend/.env and restart the backend.'];
    }
    return ['Something went wrong', 'The request failed. Details below.'];
  }

  function addError(msg) {
    const [title, hint] = friendlyError(msg);
    const box = el('div', 'error');
    box.innerHTML = '<div class="title">' + esc(title) + '</div><div>' + esc(hint) + '</div>' +
      '<details><summary>Details</summary><pre>' + esc(String(msg).slice(0, 1500)) + '</pre></details>';
    append(box);
  }

  // ---------- token usage ----------
  const usageEl = $('usage');
  const kfmt = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + 'K' : String(n));

  function showUsage(t) {
    const inTotal = t.input + t.cache_read + t.cache_write;
    let cost = '';
    if (t.unpriced_calls && !t.cost) cost = ' · cost n/a for this model';
    else cost = ' · ≈$' + t.cost.toFixed(t.cost < 0.1 ? 4 : 2) + (t.unpriced_calls ? ' (+ unpriced calls)' : '');
    usageEl.textContent = t.calls + (t.calls === 1 ? ' call' : ' calls') + ' · ' + kfmt(inTotal) + ' in' +
      (t.cache_read ? ' (' + kfmt(t.cache_read) + ' cached)' : '') + ' · ' + kfmt(t.output) + ' out' + cost;
    usageEl.title = 'Tokens used in this chat (estimate from list prices)';
    usageEl.hidden = false;
  }

  // ---------- state ----------
  // An approval card can't be answered once its stream is gone.
  function expireApprovals(label) {
    document.querySelectorAll('.approval:not(.approved):not(.rejected)').forEach((card) => {
      const actions = card.querySelector('.actions');
      if (actions) actions.remove();
      card.classList.add('rejected');
      card.querySelector('.badge').textContent = label || 'Expired';
    });
  }

  // The send button doubles as the Stop button while a request is running.
  const SEND_ICON = sendBtn.innerHTML;
  const STOP_ICON = '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="2"/></svg>';
  let stopping = false;

  function setRunning(on) {
    running = on;
    if (!on) stopping = false;
    sendBtn.classList.toggle('stop', on);
    sendBtn.innerHTML = on ? STOP_ICON : SEND_ICON;
    sendBtn.title = on ? 'Stop (Esc)' : 'Send (Enter)';
    sendBtn.setAttribute('aria-label', on ? 'Stop' : 'Send');
    if (on && !thinkingEl) {
      thinkingEl = el('div', 'thinking', '<i></i><i></i><i></i><span>Working…</span>');
      log.appendChild(thinkingEl);
      scrollDown();
    }
    if (!on && thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
  }

  function stop() {
    if (!running || stopping) return;
    stopping = true;
    vscode.postMessage({ type: 'stop' });
    if (thinkingEl) thinkingEl.querySelector('span').textContent = 'Stopping…';
  }

  function markStopped() {
    openTools.splice(0).forEach((c) => { c.classList.add('failed'); c.querySelector('.state').textContent = '✕'; });
    expireApprovals('Cancelled');
    if (!log.querySelector('.note.stopped:last-child')) append(el('div', 'note stopped', 'Stopped'));
    setRunning(false);
  }

  function send() {
    const text = input.value.trim();
    if (!text || running) return;
    addUser(text);
    vscode.postMessage({ type: 'send', text, model: modelSel.value || undefined });
    input.value = '';
    autosize();
    setRunning(true);
  }

  function autosize() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  }

  function newChat() {
    if (running) stop(); // cancel the old request before dropping its session
    log.querySelectorAll('.msg, .tool, .approval, .error, .thinking, .note').forEach((n) => n.remove());
    thinkingEl = null;
    openTools.length = 0;
    setRunning(false);
    usageEl.hidden = true;
    empty.style.display = '';
    vscode.postMessage({ type: 'newChat' });
  }

  // ---------- events ----------
  sendBtn.onclick = () => (running ? stop() : send());
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') stop();
  });
  $('new').onclick = newChat;
  document.querySelectorAll('.chip').forEach((c) => {
    c.onclick = () => { input.value = c.dataset.prompt; autosize(); input.focus(); };
  });
  log.addEventListener('click', (e) => {
    if (e.target.classList.contains('copy')) {
      const code = e.target.closest('.code').querySelector('code').textContent;
      vscode.postMessage({ type: 'copy', text: code });
      e.target.textContent = 'Copied';
      setTimeout(() => { e.target.textContent = 'Copy'; }, 1200);
    }
  });
  modelSel.addEventListener('change', () => vscode.setState({ model: modelSel.value }));

  window.addEventListener('message', ({ data: m }) => {
    switch (m.type) {
      case 'models': {
        modelSel.innerHTML = '';
        m.models.forEach((x) => {
          const o = document.createElement('option');
          o.value = x.id;
          o.textContent = x.label + (x.available ? '' : ' (no API key)');
          o.title = x.id;
          o.disabled = !x.available;
          modelSel.appendChild(o);
        });
        const saved = (vscode.getState() || {}).model;
        const pick = m.models.find((x) => x.id === saved && x.available) || m.models.find((x) => x.available);
        if (pick) modelSel.value = pick.id;
        statusDot.className = 'dot ok';
        statusDot.title = 'Backend connected';
        break;
      }
      case 'usage': showUsage(m.session); break;
      case 'text': addAssistant(m.text); break;
      case 'tool_call': addTool(m.name, m.input); break;
      case 'tool_result': finishTool(m.name, !!m.is_error); break;
      case 'approval_request': addApproval(m); break;
      case 'approval_answered':
        if (approvalCards[m.id]) approvalCards[m.id](m.approved);
        break;
      case 'error':
        openTools.splice(0).forEach((c) => { c.classList.add('failed'); c.querySelector('.state').textContent = '✕'; });
        if (/Backend unreachable/.test(m.message)) { statusDot.className = 'dot bad'; statusDot.title = 'Backend not reachable'; }
        expireApprovals();
        addError(m.message);
        setRunning(false);
        break;
      case 'stopped':
        markStopped();
        break;
      case 'done':
      case 'stream_end':
        expireApprovals();
        setRunning(false);
        break;
    }
  });

  vscode.postMessage({ type: 'ready' });
  input.focus();
})();
