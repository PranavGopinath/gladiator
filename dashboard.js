'use strict';
const $ = id => document.getElementById(id);
let controlToken = document.querySelector('meta[name="arena-control"]').content;
const palette = ['#ffd23f', '#e8a1ef', '#76d8e0', '#bff574', '#eeab88'];
const harnessColors = {codex: '#10a37f', claude: '#d97757', gemini: '#4285f4', grok: '#f2f2f2', compatible: '#e8a1ef'};
const activePhases = ['preparing', 'running', 'finishing'];
let config = null, providers = {}, latest = null, events = [], selected = null, ready = false, connected = false, busy = false;
let cursor = 0, matchId = null, renderedFeed = '', renderedTerminal = '', setupDraft = [], highlightedSeq = null, renderedReport = '', renderedPrediction = '';
const clone = value => JSON.parse(JSON.stringify(value));
const node = (tag, value, className) => { const n = document.createElement(tag); if (value != null) n.textContent = value; if (className) n.className = className; return n; };
const duration = value => { const n = Math.max(0, Math.floor(value || 0)); return `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`; };
const stamp = value => new Date(value * 1000).toLocaleTimeString([], {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'});
const compact = value => value == null ? '—' : new Intl.NumberFormat(undefined, {notation: 'compact', maximumFractionDigits: 1}).format(value);
const bytes = value => value == null ? '—' : value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GB` : `${(value / 1024 ** 2).toFixed(1)} MB`;
const fmt = value => new Intl.NumberFormat().format(Math.round(value || 0));
const signed = value => `${value >= 0 ? '+' : '−'}${fmt(Math.abs(value))}`;
const active = () => latest && activePhases.includes(latest.phase);
const providerLabel = harness => providers[harness]?.label || ({codex: 'Codex', claude: 'Claude Code', gemini: 'Gemini', grok: 'Grok', compatible: 'OpenAI compatible'})[harness] || harness;
const modelLabel = player => player.model || player.configured_model || 'Harness default';
function playersForView() {
  const live = Object.values(latest?.players || {});
  return live.length ? live : (config?.players || []).map((p, index) => ({...p, id: `agent-${index + 1}`, activity: 'ready', state: 'ready', alive: true}));
}
const colorOf = (player, index) => harnessColors[player?.harness] || palette[index % palette.length];
const playerColor = id => { const players = playersForView(), index = players.findIndex(p => p.id === id); return index < 0 ? '#c4cedd' : colorOf(players[index], index); };
const outcomeLabel = (id, market) => id === 'draw' ? 'Draw' : id === 'nobody' ? (market === 'first_blood' ? 'No kill' : 'Nobody falls') : (playersForView().find(p => p.id === id)?.name || id);
function statusOf(player) {
  if (player.state === 'eliminated' || player.activity === 'eliminated') return {kind: 'eliminated', text: 'ELIMINATED'};
  if (['blocked', 'policy_blocked'].includes(player.activity) || ['blocked', 'policy_blocked'].includes(player.state)) return {kind: 'blocked', text: 'POLICY BLOCK · ALIVE'};
  if (player.activity === 'turn_error' || player.state === 'turn_error') return {kind: 'turn_error', text: 'TURN ERROR · ALIVE'};
  if (player.activity === 'tool') return {kind: 'tool', text: 'USING TOOL'};
  if (player.activity === 'active') return {kind: 'active', text: 'MODEL ACTIVE'};
  if (player.activity === 'failed') return {kind: 'failed', text: 'MODEL FAILED'};
  if (player.activity === 'starting' || player.state === 'starting') return {kind: 'starting', text: 'STARTING'};
  if (latest?.ended_at && player.alive) return {kind: 'survivor', text: 'SURVIVED'};
  if (player.alive) return {kind: 'idle', text: 'ALIVE · BETWEEN TURNS'};
  return {kind: 'ready', text: 'READY'};
}

/* --- Arena: pixel stage + roster strip + odds HUD ------------------------- */
function renderArena() {
  const players = playersForView();
  if (!players.some(p => p.id === selected)) selected = players[0]?.id;
  const prediction = latest?.prediction || {}, live = prediction.status === 'live';
  const stage = window.ArenaStage;
  if (stage) {
    stage.setRoster(players.map((p, i) => {
      const status = statusOf(p);
      return {id: p.id, name: p.name, model: modelLabel(p), color: colorOf(p, i), alive: p.alive !== false && status.kind !== 'eliminated',
        selected: p.id === selected, status: status.kind === 'eliminated' ? 'K.O.' : status.kind === 'tool' ? 'TOOL' : status.kind === 'active' ? 'THINKING' : status.kind === 'blocked' ? 'PARKED' : status.kind === 'turn_error' ? 'TURN ERROR' : status.kind === 'survivor' ? 'SURVIVED' : '',
        statusColor: status.kind === 'eliminated' ? '#ff5b6e' : status.kind === 'survivor' ? '#39e08a' : status.kind === 'blocked' || status.kind === 'turn_error' ? '#f5cc86' : '#9aa7c7'};
    }));
    players.forEach(p => stage.setThreat(p.id, live ? prediction.factors?.[p.id]?.danger : null));
    let favorite = null, best = 0;
    if (live) for (const [id, value] of Object.entries(prediction.probabilities || {})) if (id !== 'draw' && value > best && players.find(p => p.id === id)?.alive) { best = value; favorite = id; }
    stage.crown(favorite);
  }
  const strip = $('roster-strip'), rosterKey = players.map(p => p.id).join('|');
  if (strip.dataset.roster !== rosterKey) {
    strip.replaceChildren(); strip.dataset.roster = rosterKey;
    players.forEach((p, i) => {
      const card = node('button', null, 'contender'); card.type = 'button'; card.dataset.player = p.id; card.style.setProperty('--player-color', colorOf(p, i));
      const name = node('span', null, 'contender-name'); name.append(node('i', null, 'chip'), node('span'));
      card.append(name, node('span', null, 'contender-model'), node('span', null, 'contender-state'), node('span', null, 'contender-callout'));
      card.onclick = () => selectPlayer(p.id); strip.append(card);
    });
  }
  players.forEach((p, i) => {
    const card = strip.children[i], status = statusOf(p);
    card.className = `contender ${status.kind}${selected === p.id ? ' selected' : ''}`;
    card.setAttribute('aria-pressed', String(selected === p.id)); card.setAttribute('aria-label', `${p.name}, ${modelLabel(p)}, ${status.text}. Follow contestant.`);
    card.querySelector('.contender-name span').textContent = p.name;
    card.querySelector('.contender-model').textContent = `${providerLabel(p.harness)} · ${modelLabel(p)}`;
    card.querySelector('.contender-state').textContent = status.text;
    const retry = p.alive && p.next_turn_at && active() ? `NEXT TURN ${duration(p.next_turn_at - Date.now() / 1000)}` : '';
    const odds = live && Number.isFinite(prediction.probabilities?.[p.id]) ? `JEV ${Math.round(prediction.probabilities[p.id] * 100)}% · ` : '';
    card.querySelector('.contender-callout').textContent = odds + (status.kind === 'blocked' ? 'PARKED · SESSION ALIVE' : retry || p.current_tool?.name?.split('\n')[0] || (p.turn ? `TURN ${p.turn} · ${p.tool_count || 0} TOOLS` : providerLabel(p.harness)));
  });
  renderSpark(players);
  const standing = players.filter(p => p.alive).length;
  const phase = latest?.phase || 'idle';
  $('hud-tag').textContent = phase === 'running' ? `▶ LIVE · ${standing} STANDING${myFavorite() ? ` · YOU'RE ON ${myFavorite().toUpperCase()}` : ''}` : phase === 'idle' ? '▶ LOBBY · NO MATCH' : phase === 'finished' ? `■ FINAL · ${latest?.result || ''}` : `▶ ${phase.toUpperCase()}`;
}
function myFavorite() {
  const live = (position?.positions?.winner || []).filter(b => b.status === 'live');
  if (!live.length) return null;
  const byOutcome = {}; live.forEach(b => byOutcome[b.outcome] = (byOutcome[b.outcome] || 0) + b.stake);
  return outcomeLabel(Object.entries(byOutcome).sort((a, b) => b[1] - a[1])[0][0]);
}
function renderSpark(players) {
  const history = (latest?.prediction_history || []).filter(p => p.probabilities).slice(-80);
  const svg = $('spark'), legend = $('spark-legend');
  svg.replaceChildren(); legend.replaceChildren();
  $('hud-odds').hidden = !history.length;
  if (!history.length) return;
  const W = 220, H = 54, x = i => history.length > 1 ? i / (history.length - 1) * W : W, y = v => H - v * H;
  players.forEach((p, i) => {
    const color = colorOf(p, i), pts = history.map((s, j) => Number.isFinite(s.probabilities[p.id]) ? `${x(j).toFixed(1)} ${y(s.probabilities[p.id]).toFixed(1)}` : null).filter(Boolean);
    if (!pts.length) return;
    svg.append(svgNode('path', {d: 'M' + pts.join(' L '), fill: 'none', stroke: color, 'stroke-width': 2, opacity: p.alive ? 1 : .35}));
    const last = history.at(-1).probabilities[p.id];
    if (Number.isFinite(last)) svg.append(svgNode('circle', {cx: x(history.length - 1), cy: y(last), r: 3, fill: color}));
    const item = node('span', `${p.name.toUpperCase().slice(0, 10)} ${Number.isFinite(last) ? Math.round(last * 100) : '—'}%`); const chip = node('i', null, 'chip'); chip.style.background = color; item.prepend(chip); legend.append(item);
  });
}
function selectPlayer(id) { selected = id; renderedTerminal = ''; renderArena(); renderObservation(); }

/* --- Event-driven stage animation ----------------------------------------- */
let animatedSeq = null, animationQueue = [], animationTimer = null;
function classifyForStage(row) {
  const players = playersForView();
  if (row.kind === 'elimination') return () => window.ArenaStage.knockout(row.data?.contestant || row.player, row.data?.attacker, row.data?.confidence === 'confirmed');
  if (!players.some(p => p.id === row.player)) return null;
  if (row.kind === 'tool') return row.data?.status === 'failed' ? null : () => window.ArenaStage.tool(row.player);
  if (row.kind === 'message') {
    const text = String(row.text || '');
    if (/^\s*(ATTACK|OFFENSE|STRIKE)\b/i.test(text)) {
      const target = players.find(p => p.id !== row.player && p.alive && (text.includes(p.name) || text.includes(p.id)))
        || players.find(p => p.id !== row.player && p.alive);
      return () => window.ArenaStage.strike(row.player, target?.id, 'STRIKE');
    }
    if (/^\s*(DEFEND|DEFENSE|GUARD|HARDEN|FORTIFY)\b/i.test(text)) return () => window.ArenaStage.guard(row.player);
    return () => window.ArenaStage.speak(row.player);
  }
  return null;
}
function enqueueAnimations(rows) {
  if (!window.ArenaStage) return;
  if (animatedSeq === null) { animatedSeq = rows.at(-1)?.seq ?? 0; return; } // never replay a whole recording on load
  rows.filter(row => row.seq > animatedSeq).forEach(row => { const fn = classifyForStage(row); if (fn) animationQueue.push(fn); animatedSeq = row.seq; });
  animationQueue = animationQueue.slice(-12);
  if (!animationTimer) drainAnimations();
}
function drainAnimations() {
  const fn = animationQueue.shift();
  if (!fn) { animationTimer = null; return; }
  try { fn(); } catch (_) {}
  animationTimer = setTimeout(drainAnimations, 340);
}

/* --- Kernel evidence & contestant report (retained) ----------------------- */
function evidenceButton(seq) {
  const button = node('button', `Event #${seq}`, 'evidence-link'); button.type = 'button';
  button.setAttribute('aria-label', `View evidence event ${seq}`);
  button.onclick = event => { event.stopPropagation(); showEvidence(seq); };
  return button;
}
function showEvidence(seq) {
  const row = events.find(event => event.seq === seq);
  if (!row) { errorMessage(`Event #${seq} is outside the live display window. Export the complete event log to inspect it.`); return; }
  highlightedSeq = seq; $('follow').checked = false;
  if (playersForView().some(player => player.id === row.player)) selected = row.player;
  renderedTerminal = ''; renderedFeed = ''; selectTab('terminal'); renderArena(); renderObservation(); renderPlayFeed();
  const target = row.player === selected ? $('terminal-feed').querySelector(`[data-seq="${seq}"]`) : $('play-feed').querySelector(`[data-seq="${seq}"]`);
  if (target) { target.scrollIntoView({block: 'center', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'}); target.tabIndex = -1; target.focus({preventScroll: true}); }
}
function kernelEvidence(elimination) {
  return Array.isArray(elimination?.kernel_evidence) ? elimination.kernel_evidence.filter(event => event && typeof event === 'object' && typeof (event.type || event.kind) === 'string').map(event => ({...event, type: event.type || event.kind})) : [];
}
function eliminationConfidence(elimination) {
  const trace = kernelEvidence(elimination);
  if (elimination?.confidence === 'confirmed' && trace.some(event => event.type === 'SIGNAL') && trace.some(event => event.type === 'EXIT')) return 'confirmed';
  return elimination?.confidence === 'probable' ? 'probable' : 'unknown';
}
function renderKernelObserver() {
  const observer = latest?.observer || {}, status = observer.status || 'unavailable';
  const labels = {disabled: 'Off', starting: 'Starting', ready: 'Live', unavailable: 'Unavailable', degraded: 'Degraded', stopped: observer.previous_status === 'ready' ? 'Off · recorded' : observer.previous_status === 'degraded' ? 'Off · degraded' : 'Off'};
  const display = connected ? status : 'unavailable';
  $('kernel-status').textContent = connected ? labels[status] || 'Unavailable' : 'Unavailable';
  $('kernel-status').className = `kernel-status ${display}`;
  const defaults = {disabled: 'Kernel observation is disabled for this match.', starting: 'The kernel observer is starting. Evidence is not yet available.', ready: 'Recording kernel connections, process ancestry, signals, and exits.', unavailable: 'No kernel observation is available for this match. Attacker attribution may rely on recorded activity.', degraded: 'Kernel observation has gaps. Incomplete evidence cannot confirm attacker attribution.', stopped: 'Kernel recording has stopped. Evidence captured during the match remains in the recording.'};
  $('kernel-message').textContent = connected ? observer.message || defaults[status] || defaults.unavailable : 'The referee connection was lost. Kernel observer status cannot be verified.';
  const parts = [];
  const losses = observer.loss_count ?? observer.dropped_events;
  if (Number.isFinite(losses)) parts.push(`Dropped events: ${losses}`);
  if (observer.boot_id) parts.push(`Boot: ${observer.boot_id}`);
  $('kernel-meta').textContent = parts.join(' · '); $('kernel-meta').hidden = !parts.length;
}
function kernelEvidenceDetails(elimination) {
  const trace = kernelEvidence(elimination);
  if (!trace.length) return null;
  const detail = node('details', null, 'kernel-evidence');
  detail.append(node('summary', `Kernel evidence chain · ${trace.length} events`));
  const list = node('ol', null, 'kernel-chain');
  const labels = {CONNECT: 'Source connection', SSH_RECV: 'Remote SSH session', FORK: 'Process ancestry', SIGNAL: 'Signal sent', EXIT: 'Process exit'};
  const names = {id: 'Evidence ID', ts: 'Monotonic time (ns)', pid: 'Host PID', start: 'Process start (ns)', parent: 'Parent host PID', parent_start: 'Parent start (ns)', child: 'Child host PID', child_start: 'Child start (ns)', sender: 'Sender host PID', sender_start: 'Sender start (ns)', sender_parent: 'Sender parent PID', target: 'Target host PID', target_start: 'Target start (ns)', cgroup: 'Cgroup', ns: 'PID namespace', namespace: 'PID namespace', exit_code: 'Raw exit status', group_exit_code: 'Group exit status', signal: 'Signal number', comm: 'Command', boot_id: 'Boot ID', epoch: 'Observer epoch'};
  trace.slice().sort((a, b) => Number(a.ts || 0) - Number(b.ts || 0)).forEach(event => {
    const item = node('li'), title = node('strong', labels[event.type] || event.type);
    item.append(title);
    const contestant = playersForView().find(player => player.id === event.contestant);
    if (contestant) item.append(node('p', `Contender: ${contestant.name}`, 'kernel-chain-context'));
    if (event.local && event.remote) item.append(node('p', `${event.local}:${event.local_port ?? '?'} ${event.type === 'SSH_RECV' ? '←' : '→'} ${event.remote}:${event.remote_port ?? '?'}`, 'kernel-chain-context mono'));
    if (event.type === 'SIGNAL' && event.signal != null) item.append(node('p', `Signal ${event.signal}${({9: ' · SIGKILL', 15: ' · SIGTERM'})[event.signal] || ''}`, 'kernel-chain-context'));
    const fields = node('dl', null, 'kernel-fields');
    Object.entries(names).forEach(([key, label]) => {
      if (event[key] == null || typeof event[key] === 'object') return;
      fields.append(node('dt', label), node('dd', String(event[key])));
    });
    item.append(fields); list.append(item);
  });
  detail.append(list);
  const download = node('a', 'Download kernel recording ↗', 'kernel-download'); download.href = '/api/export/kernel'; download.download = ''; detail.append(download);
  return detail;
}
function renderContestantReport(player) {
  const status = statusOf(player), report = $('contestant-report'), elimination = player.elimination;
  const show = status.kind === 'eliminated' || status.kind === 'turn_error' || status.kind === 'blocked';
  const key = JSON.stringify([player.id, status.kind, elimination, player.last_error]);
  report.hidden = !show;
  if (key !== renderedReport) {
    renderedReport = key; report.replaceChildren(); report.className = `contestant-report ${status.kind}`;
    if (status.kind === 'eliminated') {
      const confidence = eliminationConfidence(elimination);
      const summary = elimination?.confidence === 'confirmed' && confidence !== 'confirmed' ? 'The contestant session terminated. Kernel evidence is unavailable in this report.' : elimination?.summary || player.reason || 'The referee observed the contestant session terminate.';
      report.append(node('span', 'ELIMINATION REPORT', 'lbl'), node('strong', summary));
      const details = node('div', null, 'elimination-details');
      if (elimination?.cause) details.append(node('span', `Cause: ${String(elimination.cause).replaceAll('_', ' ')}`));
      details.append(node('span', {confirmed: 'Confirmed · kernel evidence', probable: 'Probable · activity correlation', unknown: 'Unknown · attribution unverified'}[confidence], `confidence-badge ${confidence}`));
      report.append(details);
      const attacker = playersForView().find(p => p.id === elimination?.attacker);
      if (attacker) {
        const link = node('button', `Attributed contender: ${attacker.name} / ${providerLabel(attacker.harness)}`, 'attributed-contender');
        link.type = 'button'; link.onclick = () => selectPlayer(attacker.id); report.append(link);
      } else report.append(node('p', 'Attacker: not established', 'attribution-note'));
      if (elimination?.attacker && confidence !== 'confirmed') report.append(node('p', 'The process death was observed. Attacker attribution is a correlation with recorded activity.', 'attribution-note'));
      const kernel = kernelEvidenceDetails(elimination); if (kernel) report.append(kernel);
      if (Array.isArray(elimination?.evidence_seqs) && elimination.evidence_seqs.length) {
        const links = node('div', null, 'evidence-links'); links.append(node('span', 'Recorded evidence:', 'attribution-note'));
        elimination.evidence_seqs.forEach(seq => links.append(evidenceButton(seq))); report.append(links);
      }
    } else {
      report.append(node('span', status.kind === 'blocked' ? 'POLICY BLOCK · CONTESTANT ALIVE' : 'TURN ERROR · CONTESTANT ALIVE', 'lbl'));
      report.append(node('strong', status.kind === 'blocked' ? 'The agent is parked after a policy block. Its session is still alive.' : 'The model turn failed. The contestant session is still alive.'));
      if (player.last_error) report.append(node('p', player.last_error, 'turn-error-detail'));
      if (status.kind !== 'blocked') report.append(node('p', null, 'retry-countdown'));
    }
  }
  const retry = report.querySelector('.retry-countdown');
  if (retry) retry.textContent = player.next_turn_at && active() ? `Next turn in ${duration(player.next_turn_at - Date.now() / 1000)}.` : 'Waiting for the next scheduled turn.';
}
function renderObservation() {
  const players = playersForView(), index = players.findIndex(p => p.id === selected), player = players[index];
  if (!player) return;
  $('observer-avatar').style.setProperty('--player-color', colorOf(player, index));
  $('observer-name').textContent = player.name;
  $('observer-model').textContent = `${providerLabel(player.harness)} / ${modelLabel(player)}`;
  $('terminal-title').textContent = `${player.id} / public activity`;
  $('terminal-activity').textContent = statusOf(player).text;
  renderContestantReport(player);
  const allRows = events.filter(row => row.player === selected);
  const rows = allRows.slice(-300);
  const evidenceRow = allRows.find(row => row.seq === highlightedSeq);
  if (evidenceRow && !rows.includes(evidenceRow)) rows.unshift(evidenceRow);
  const key = `${selected}:${matchId}:${rows[0]?.seq}:${rows.at(-1)?.seq}:${highlightedSeq}`;
  if (key !== renderedTerminal) {
    renderedTerminal = key;
    const feed = $('terminal-feed'), scroll = feed.scrollTop;
    feed.replaceChildren();
    if (!rows.length) {
      const empty = node('div', null, 'terminal-empty'); empty.append(node('span', '>_', 'mono'), node('p', 'Commands, tool results, and model messages will appear here.')); feed.append(empty);
    }
    rows.forEach(row => {
      const item = node('article', null, `terminal-row ${row.kind}${row.seq === highlightedSeq ? ' evidence-highlight' : ''}`), meta = node('div', null, 'terminal-meta');
      item.dataset.seq = row.seq;
      meta.append(node('span', row.kind.toUpperCase(), 'terminal-kind'), node('time', stamp(row.time)), node('span', `#${row.seq}`));
      if (row.data?.status) meta.append(node('span', row.data.status.toUpperCase()));
      if (row.data?.exit_code != null) meta.append(node('span', `EXIT ${row.data.exit_code}`));
      item.append(meta, node('pre', row.text));
      if (row.data?.output != null && row.data.output !== '') item.append(node('pre', typeof row.data.output === 'string' ? row.data.output : JSON.stringify(row.data.output, null, 2), 'terminal-output'));
      feed.append(item);
    });
    feed.scrollTop = $('follow').checked ? feed.scrollHeight : scroll;
  }
  const usage = player.usage || {}, resource = player.resources || {};
  const metrics = [
    ['SESSION', player.state || 'Ready'], ['CURRENT TURN', player.turn ?? '—'], ['TOOLS EXECUTED', player.tool_count ?? '—'], ['CONTAINER HEALTH', player.health ?? '—'],
    ['INPUT TOKENS', compact(usage.input_tokens)], ['OUTPUT TOKENS', compact(usage.output_tokens)], ['CACHED INPUT', compact(usage.cached_input_tokens)], ['REPORTED COST', player.cost_usd == null ? '—' : `$${player.cost_usd.toFixed(4)}`],
    ['CPU', resource.cpu_percent == null ? '—' : `${resource.cpu_percent.toFixed(1)}%`], ['MEMORY', bytes(resource.memory_bytes)], ['MEMORY LIMIT', bytes(resource.memory_limit_bytes)], ['PROCESSES', resource.pids ?? '—']
  ];
  $('telemetry').replaceChildren(...metrics.map(([label, value]) => { const stat = node('div', null, 'stat'); stat.append(node('span', label, 'lbl'), node('strong', value)); return stat; }));
}

/* --- P/L feed: match events merged with the bettor's own money events ----- */
function moneyRows() {
  return (position?.history || []).filter(e => e.ts && (e.reason !== 'bet' || e.match_id === matchId)).map(e => ({money: true, time: e.ts, seq: `m${e.key || e.ts}`, entry: e}));
}
function describeMoney(e) {
  const market = (marketTitle[e.market] || e.market || '').toLowerCase();
  if (e.reason === 'purchase') return `Credits added to your wallet.`;
  if (e.reason === 'bet') return `You backed ${outcomeLabel(e.outcome, e.market)} on ${market} at ×${(1 / (e.price || 1)).toFixed(2)}.`;
  if (e.reason === 'payout') return `Your ${market} ticket on ${outcomeLabel(e.outcome, e.market)} paid out.`;
  if (e.reason === 'refund') return `Stake returned: ${e.note || 'market voided'}.`;
  return e.reason;
}
function renderPlayFeed() {
  const rows = [...events.filter(row => row.kind !== 'usage').slice(-180), ...moneyRows()].sort((a, b) => a.time - b.time).slice(-220);
  const key = `${matchId}:${rows.length}:${rows[0]?.seq}:${rows.at(-1)?.seq}:${highlightedSeq}`;
  if (key === renderedFeed) return;
  renderedFeed = key;
  const feed = $('play-feed'), scroll = feed.scrollTop;
  if (!rows.length) { const empty = node('div', null, 'empty-feed'); empty.append(node('strong', 'The stage is set.'), node('p', 'Tool activity, referee decisions, and your money events appear here.')); feed.replaceChildren(empty); return; }
  feed.replaceChildren();
  const players = playersForView();
  const icons = {elimination: '☠', tool: '›', message: '…', session: '◷', result: '🏁', market: '◈', observer: '◎', error: '!', attribution: '⚑', system: '·', start: '▶'};
  rows.forEach(row => {
    if (row.money) {
      const e = row.entry, item = node('article', null, 'evt money'), body = node('div', null, 'body');
      body.append(node('span', 'YOU', 'who'), node('time', stamp(row.time), 'time'), node('p', describeMoney(e), 'txt'), node('span', e.reason.toUpperCase(), 'kind'));
      item.append(node('span', '◈', 'ic'), body, node('span', `${signed(e.delta)} ◈`, `amt ${e.delta > 0 ? 'win' : e.delta < 0 ? 'lose' : 'neu'}`));
      feed.append(item); return;
    }
    const index = players.findIndex(p => p.id === row.player), player = players[index];
    const item = node('article', null, `evt ${row.kind}${row.seq === highlightedSeq ? ' evidence-highlight' : ''}`); item.dataset.seq = row.seq; item.style.setProperty('--player-color', index < 0 ? '#c4cedd' : colorOf(player, index));
    const body = node('div', null, 'body');
    let brief = row.text || '';
    if (row.kind === 'tool') brief = `${row.data?.status === 'running' ? 'Running' : row.data?.status === 'failed' ? 'Tool failed:' : 'Completed:'} ${brief.split('\n')[0]}`;
    body.append(node('span', player?.name || 'Referee', 'who'), node('time', stamp(row.time), 'time'), node('p', brief.length > 180 ? `${brief.slice(0, 177)}…` : brief, 'txt'), node('span', `${row.kind.toUpperCase()} · #${row.seq}`, 'kind'));
    if (row.kind === 'attribution' || row.kind === 'elimination') {
      const attribution = row.data?.elimination || row.data || {};
      const attacker = players.find(p => p.id === attribution.attacker);
      if (attacker) body.append(node('p', `Attributed contender: ${attacker.name} / ${providerLabel(attacker.harness)}`, 'attribution-detail'));
      body.append(node('span', `CONFIDENCE: ${eliminationConfidence(attribution).toUpperCase()}`, `kind confidence-${eliminationConfidence(attribution)}`));
      if (Array.isArray(attribution.evidence_seqs) && attribution.evidence_seqs.length) {
        const links = node('div', null, 'evidence-links');
        attribution.evidence_seqs.forEach(seq => links.append(evidenceButton(seq))); body.append(links);
      }
    }
    const amount = moneyConsequence(row);
    item.append(node('span', icons[row.kind] || '·', 'ic'), body, node('span', amount.text, `amt ${amount.cls}`));
    if (player) { item.tabIndex = 0; item.setAttribute('role', 'button'); item.setAttribute('aria-label', `Follow ${player.name}: ${brief}`); item.onclick = () => selectPlayer(player.id); item.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectPlayer(player.id); } }; }
    feed.append(item);
  });
  feed.scrollTop = $('follow').checked ? feed.scrollHeight : scroll;
}
function moneyConsequence(row) {
  const tickets = Object.values(position?.positions || {}).flat();
  if (row.kind === 'elimination') {
    const victim = row.data?.contestant || row.player, attacker = row.data?.attacker;
    if (tickets.some(t => t.market === 'first_fallen' && t.outcome === victim)) return {text: 'SETTLES', cls: 'win'};
    if (attacker && tickets.some(t => t.market === 'first_blood' && t.outcome === attacker)) return {text: 'KILL CREDITED', cls: 'win'};
    if (tickets.some(t => t.market === 'winner' && t.outcome === victim)) return {text: 'TICKET DEAD', cls: 'lose'};
    return {text: '', cls: 'neu'};
  }
  if (row.kind === 'market' && row.data?.status === 'settled') {
    const mine = tickets.filter(t => t.market === row.data.market);
    if (!mine.length) return {text: '', cls: 'neu'};
    const won = mine.reduce((sum, t) => sum + (t.returned || 0), 0), staked = mine.reduce((sum, t) => sum + t.stake, 0);
    return {text: `${signed(won - staked)} ◈`, cls: won >= staked ? 'win' : 'lose'};
  }
  return {text: '', cls: 'neu'};
}

/* --- Betting: board, slip, position, wallet -------------------------------- */
let board = null, position = null, balance = 0, info = {}, slip = {market: null, outcome: null}, moves = {}, lastPrices = {}, seenLedger = null, toastTimer = null, renderedBoard = '', renderedPosition = '';
const MARKET_NAMES = ['winner', 'first_blood', 'first_fallen'];
const marketTitle = {winner: 'Match winner', first_blood: 'First blood', first_fallen: 'First fallen'};
const marketShort = {winner: 'WINNER', first_blood: 'FIRST BLOOD', first_fallen: 'FIRST FALLEN'};
const marketSub = {winner: 'Who is the last agent standing. Sole surviving agent wins. A draw refunds every stake in this market. Bets close 10 seconds before the deadline.', first_blood: 'Who lands the first kill, as attributed by the referee. Priced by Jev progress evidence; "no kill" is pool-priced.', first_fallen: 'Who falls first. Priced by Jev near-term danger; "nobody falls" is pool-priced.'};
function bettingMessage(text, kind = 'error') { const el = $('market-message'); el.textContent = text || ''; el.className = `market-message ${text ? kind : ''}`; }
async function refreshBetting() {
  try {
    const [b, p] = await Promise.all([jsonRequest('/api/market'), jsonRequest('/api/position')]);
    board = b; position = p; balance = p.balance;
    if (!info.loaded) { const c = await jsonRequest('/api/credits'); info = {...c, loaded: true}; }
    trackMovement(); trackSettlements();
    renderWallet(); renderMarkets(); renderPosition(); renderSlip(); renderedFeed = ''; renderPlayFeed();
  } catch (_) { /* keep the last board; the match poller reports connectivity */ }
}
function trackMovement() {
  const now = Date.now();
  for (const name of MARKET_NAMES) for (const [id, o] of Object.entries(board?.[name]?.outcomes || {})) {
    const key = `${name}:${id}`, prev = lastPrices[key];
    if (prev != null && Math.abs(o.price - prev) >= 0.005) moves[key] = {dir: o.price < prev ? 'up' : 'dn', at: now};
    lastPrices[key] = o.price;
  }
}
function trackSettlements() {
  const entries = position?.history || [];
  if (seenLedger === null) { seenLedger = new Set(entries.map(e => e.key || e.ts)); return; }
  entries.forEach(e => {
    const id = e.key || e.ts; if (seenLedger.has(id)) return; seenLedger.add(id);
    if (e.reason === 'payout') toast(`◈ +${fmt(e.delta)} · ${outcomeLabel(e.outcome, e.market).toUpperCase()} PAYS`, `${marketTitle[e.market] || e.market} market settled by the referee.`, 'win');
    else if (e.reason === 'refund') toast(`◈ +${fmt(e.delta)} · STAKE RETURNED`, e.note || 'Market voided.', 'refund');
    else if (e.reason === 'purchase') toast(`◈ +${fmt(e.delta)} · CREDITS ADDED`, 'Stripe confirmed your payment.', 'win');
  });
}
function toast(title, sub, kind) {
  $('toast-title').textContent = title; $('toast-sub').textContent = sub || ''; $('toast').className = `toast ${kind || ''}`; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 6500);
}
function renderWallet() {
  $('wallet-balance').textContent = fmt(balance);
  const add = $('add-funds');
  add.textContent = '+ ADD FUNDS';
  if (info.stripe) { add.disabled = false; add.title = 'Buy credits with Stripe Checkout'; }
  else if (info.dev_credits) { add.disabled = false; add.title = 'Add funds'; }
  else { add.disabled = true; add.title = 'Set STRIPE_SECRET_KEY on the host to enable purchases'; }
}
function renderMarkets() {
  if (!board) return;
  const players = playersForView();
  const key = JSON.stringify([board, slip, players.map(p => [p.id, p.name, p.alive]), Object.values(moves).map(m => m.at > Date.now() - 6000)]);
  if (key === renderedBoard) return; renderedBoard = key;
  const root = $('markets'); root.replaceChildren();
  const labels = {idle: 'Opens at kickoff', open: 'Open', closed: 'Locked', settled: 'Settled', void: 'Void · refunded'};
  const anyOpen = MARKET_NAMES.some(n => board[n]?.status === 'open');
  $('market-status').textContent = anyOpen ? 'OPEN · IN-PLAY' : board.phase === 'running' ? 'LOCKED' : 'PARI-MUTUEL';
  for (const name of MARKET_NAMES) {
    const m = board[name]; if (!m) continue;
    const row = node('div', null, 'mrow'), q = node('div', null, 'q');
    const winning = m.result?.winning_outcome;
    q.append(node('b', marketTitle[name]), node('span', m.status === 'settled' ? `Settled · ${outcomeLabel(winning, name)}` : labels[m.status] || m.status, m.status));
    row.append(q);
    const opts = node('div', null, 'opts');
    const entries = Object.entries(m.outcomes || {});
    if (!entries.length) opts.append(node('p', 'The book opens when a match begins.', 'empty'));
    entries.forEach(([id, o]) => {
      const open = m.status === 'open' && o.open, settled = m.status === 'settled' || m.status === 'void';
      const out = !o.open && !settled && !(id === 'draw' || id === 'nobody');
      const btn = node('button', null, `chipbtn${slip.market === name && slip.outcome === id ? ' sel' : ''}${out || (!open && !settled) ? ' dead' : ''}${winning === id ? ' winner' : settled ? ' lost' : ''}`);
      btn.type = 'button'; btn.disabled = !open;
      const nm = node('span', null, 'nm');
      if (id !== 'draw' && id !== 'nobody') { const chip = node('i', null, 'chip'); chip.style.background = playerColor(id); nm.append(chip); }
      nm.append(document.createTextNode(outcomeLabel(id, name)));
      const od = node('span', winning === id ? 'WON' : m.status === 'void' ? 'REFUNDED' : settled ? '—' : out ? 'OUT' : `${(o.odds || 0).toFixed(2)}×`, 'od');
      const mv = moves[`${name}:${id}`];
      if (open && mv && mv.at > Date.now() - 6000) od.append(node('span', mv.dir === 'up' ? '▲' : '▼', `mv ${mv.dir}`));
      btn.append(nm, od, node('span', o.pool ? `${fmt(o.pool)} ◈ pooled` : 'no money yet', 'pool'));
      btn.setAttribute('aria-label', `${outcomeLabel(id, name)} at ${(o.odds || 0).toFixed(2)} times${open ? '' : ', closed'}`);
      btn.onclick = () => { slip = {market: name, outcome: id}; renderMarkets(); renderSlip(); $('stake').focus(); };
      opts.append(btn);
    });
    row.append(opts, node('p', marketSub[name], 'meta'));
    root.append(row);
  }
  const parts = [];
  const pooled = MARKET_NAMES.reduce((s, n) => s + (board[n]?.total_pool || 0), 0), bets = MARKET_NAMES.reduce((s, n) => s + (board[n]?.bet_count || 0), 0);
  if (pooled) parts.push(`${fmt(pooled)} credits pooled across the books`);
  parts.push(`${bets} bet${bets === 1 ? '' : 's'}`);
  if (board.winner?.rake_bps) parts.push(`${(board.winner.rake_bps / 100).toFixed(2)}% rake`);
  parts.push('Settles only on referee facts');
  $('market-meta').textContent = parts.join(' · ');
}
function projectedReturn(m, outcome, stake) {
  const o = m?.outcomes?.[outcome]; if (!o || !(stake > 0)) return null;
  const w = stake / o.price, total = (m.total_pool || 0) + stake, distributable = total - Math.floor(total * (m.rake_bps || 0) / 10000);
  const opposing = Object.entries(m.outcomes).some(([id, x]) => id !== outcome && x.pool > 0);
  return {amount: Math.floor(distributable * w / ((o.weight || 0) + w)), opposing};
}
function renderSlip() {
  const m = board?.[slip.market], o = m?.outcomes?.[slip.outcome];
  const stake = Math.floor(Number($('stake').value));
  const open = m?.status === 'open' && o?.open;
  if (!o) { $('slip-selection').textContent = 'No selection'; $('slip-odds').textContent = ''; $('slip-return').textContent = '—'; $('slip-status').textContent = 'SELECTION'; $('place-bet').textContent = 'PLACE BET'; $('place-bet').disabled = true; $('slip-note').textContent = 'Pari-mutuel: you win a share of the pool, weighted by the odds when you bet. The referee settles.'; return; }
  $('slip-selection').textContent = `${outcomeLabel(slip.outcome, slip.market)} · ${marketTitle[slip.market]}`;
  $('slip-odds').textContent = `${(o.odds || 0).toFixed(2)}×`;
  $('slip-status').textContent = open ? 'OPEN' : 'CLOSED';
  const projection = projectedReturn(m, slip.outcome, stake);
  $('slip-return').textContent = projection ? `≈ ◈ ${fmt(projection.amount)}` : '—';
  $('slip-note').textContent = !projection ? 'Enter a stake to see the projected return.' : !projection.opposing ? 'No opposing money yet: with nothing to win from, the stake is refunded at settlement unless someone backs another outcome.' : `If ${outcomeLabel(slip.outcome, slip.market)} settles and no more money arrives. Later bets dilute this; earlier conviction pays more.`;
  const affordable = Number.isFinite(stake) && stake > 0 && stake <= balance;
  $('place-bet').textContent = affordable ? `PLACE BET ◈ ${fmt(stake)}` : stake > balance ? 'NOT ENOUGH CREDITS' : 'PLACE BET';
  $('place-bet').disabled = !open || !affordable;
}
function renderPosition() {
  const tickets = Object.values(position?.positions || {}).flat().sort((a, b) => a.ts - b.ts);
  const key = JSON.stringify([tickets, matchId]);
  if (key === renderedPosition) return; renderedPosition = key;
  const root = $('position-rows'), summary = $('position-summary'); root.replaceChildren();
  if (!tickets.length) { root.append(node('p', board?.winner?.status === 'open' ? 'Pick an outcome in the markets to open a ticket.' : 'No tickets on this match.', 'empty')); summary.hidden = true; $('position-status').textContent = 'NO TICKETS'; return; }
  const live = tickets.filter(t => t.status === 'live');
  $('position-status').textContent = live.length ? `${live.length} LIVE` : 'SETTLED';
  tickets.forEach(t => {
    const card = node('div', null, `ticket ${t.status}`), big = node('div', null, 'big');
    if (t.outcome !== 'draw' && t.outcome !== 'nobody') { const chip = node('i', null, 'chip'); chip.style.background = playerColor(t.outcome); big.append(chip); }
    big.append(document.createTextNode(outcomeLabel(t.outcome, t.market).toUpperCase()), node('span', marketShort[t.market] || t.market.toUpperCase(), 'mk'));
    card.append(big);
    const row = (label, value, cls) => { const r = node('div', null, 'row'); r.append(node('span', label), node('b', value, cls)); card.append(r); };
    row('Stake', `◈ ${fmt(t.stake)}`); row('Odds (entry)', `${t.odds.toFixed(2)}×`);
    if (t.status === 'live') { const pl = t.projected - t.stake; row('Projected return', `◈ ${fmt(t.projected)}`); row('If it settles', `${signed(pl)} ◈ ${pl >= 0 ? '▲' : '▼'}`, `pl ${pl > 0 ? 'up' : pl < 0 ? 'down' : 'neu'}`); }
    else if (t.status === 'won') row('Paid out', `◈ ${fmt(t.returned)} (${signed(t.returned - t.stake)})`, `pl ${t.returned >= t.stake ? 'up' : 'down'}`);
    else if (t.status === 'lost') row('Result', `−${fmt(t.stake)} ◈`, 'pl down');
    else row('Refunded', `◈ ${fmt(t.returned)}`, 'pl neu');
    root.append(card);
  });
  const staked = tickets.reduce((s, t) => s + t.stake, 0), projected = live.reduce((s, t) => s + t.projected, 0), settled = tickets.filter(t => t.status !== 'live').reduce((s, t) => s + (t.returned || 0) - t.stake, 0);
  summary.hidden = false; summary.replaceChildren();
  [['Total staked', `◈ ${fmt(staked)}`], ['Live projected', live.length ? `◈ ${fmt(projected)}` : '—'], ['Settled P/L', tickets.length > live.length ? `${signed(settled)} ◈` : '—'], ['Tickets', `${tickets.length}`]].forEach(([l, v]) => { const d = node('div'); d.append(node('span', l), node('b', v)); summary.append(d); });
}
async function placeBet() {
  const stake = Math.floor(Number($('stake').value));
  if (!slip.market || !slip.outcome) { bettingMessage('Pick an outcome first.'); return; }
  if (!Number.isFinite(stake) || stake <= 0) { bettingMessage('Enter a stake of at least 1 credit.'); return; }
  $('place-bet').disabled = true;
  try {
    const result = await jsonRequest('/api/bet', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({market: slip.market, outcome: slip.outcome, stake})});
    balance = result.balance;
    bettingMessage(`Ticket open: ${outcomeLabel(slip.outcome, slip.market)} for ${fmt(stake)} at ×${(1 / result.bet.price).toFixed(2)}.`, 'ok');
    toast(`◈ ${fmt(stake)} ON ${outcomeLabel(slip.outcome, slip.market).toUpperCase()}`, `${marketTitle[slip.market]} · entry ${(1 / result.bet.price).toFixed(2)}× · frozen at placement`, 'bet');
    await refreshBetting();
  } catch (error) { bettingMessage(error.message); renderSlip(); }
}
async function addFunds() {
  $('funds-error').hidden = true;
  if (info.stripe) {
    const packs = $('funds-packs'); packs.replaceChildren();
    Object.entries(info.packs || {small: 500, medium: 1500, large: 5000}).forEach(([pack, cents]) => {
      const btn = node('button', null, 'pack'); btn.type = 'button';
      btn.append(node('b', `◈ ${fmt(cents)}`), node('span', `${pack} · $${(cents / 100).toFixed(2)} via Stripe Checkout`));
      btn.onclick = async () => {
        try { const session = await jsonRequest('/api/checkout', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pack})}); window.location.href = session.url; }
        catch (error) { $('funds-error').textContent = error.message; $('funds-error').hidden = false; }
      };
      packs.append(btn);
    });
    $('funds-copy').textContent = info.sandbox ? 'Stripe Checkout in test mode: use card 4242 4242 4242 4242. Credits arrive when Stripe confirms the payment.' : 'Stripe Checkout. Credits arrive when Stripe confirms the payment.';
    $('funds-dialog').showModal();
  } else if (info.dev_credits) {
    try { await jsonRequest('/api/stripe/webhook', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({credits: 500})}); await refreshBetting(); }
    catch (error) { bettingMessage(error.message); }
  }
}
function fundsReturn() {
  const params = new URLSearchParams(location.search), state = params.get('credits');
  if (!state) return;
  history.replaceState(null, '', location.pathname);
  const note = $('funds-note'); note.hidden = false;
  note.textContent = state === 'pending' ? 'Payment sent to Stripe. Credits land in your wallet the moment the webhook confirms it; keep this tab open.' : 'Checkout canceled. No charge was made.';
  if (state === 'pending') {
    const landed = () => { const purchase = (position?.history || []).filter(e => e.reason === 'purchase').at(-1); note.hidden = true; toast(`◈ +${fmt(purchase?.delta || balance)} · CREDITS ADDED`, 'Stripe confirmed your payment.', 'win'); };
    if (balance > 0) landed();
    else { const check = setInterval(() => { if (balance > 0) { landed(); clearInterval(check); } }, 1000); setTimeout(() => clearInterval(check), 120000); }
  }
  else setTimeout(() => { note.hidden = true; }, 8000);
}

/* --- Forecast chart & Jev panel (retained) --------------------------------- */
function svgNode(tag, attrs = {}, value) { const n = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [key, v] of Object.entries(attrs)) n.setAttribute(key, v); if (value !== undefined) n.textContent = value; return n; }
let chartMatch = null, chartSelected = null;
function renderForecastChart(s) {
  const chart = $('forecast-chart'), legend = $('chart-legend'), slider = $('chart-scrubber'), detail = $('chart-detail');
  const history = (s.prediction_history || []).filter(p => Number.isFinite(p.updated_at) && p.probabilities).slice().sort((a, b) => a.updated_at - b.updated_at);
  if (chartMatch !== s.match_id) { chartMatch = s.match_id; chartSelected = null; }
  chart.replaceChildren(); legend.replaceChildren();
  const players = playersForView();
  const ids = [...new Set(history.flatMap(p => Object.keys(p.probabilities)))];
  const colorFor = id => { const i = players.findIndex(p => p.id === id); return i < 0 ? '#9daaa7' : colorOf(players[i], i); };
  for (const id of ids) { const label = node('span', s.players[id]?.name || id); const dot = document.createElement('i'); dot.style.background = colorFor(id); label.prepend(dot); legend.append(label); }
  const start = s.started_at || history[0]?.updated_at || 0;
  const end = s.ended_at || (s.phase === 'running' ? Date.now() / 1000 : history.at(-1)?.updated_at) || start + 1;
  const span = Math.max(10, end - start), x = t => 55 + Math.max(0, t - start) / span * 865, y = p => 210 - p * 185;
  for (const pct of [0, 25, 50, 75, 100]) chart.append(svgNode('line', {x1: 55, x2: 920, y1: y(pct / 100), y2: y(pct / 100), stroke: '#262649', 'stroke-dasharray': '3 5'}), svgNode('text', {x: 43, y: y(pct / 100) + 4, fill: '#9aa7c7', 'font-size': 11, 'text-anchor': 'end'}, pct + '%'));
  for (let i = 0; i <= 4; i++) { const elapsed = span * i / 4; chart.append(svgNode('text', {x: x(start + elapsed), y: 238, fill: '#9aa7c7', 'font-size': 11, 'text-anchor': 'middle'}, duration(elapsed))); }
  for (const id of ids) {
    const samples = history.filter(p => Number.isFinite(p.probabilities[id])), color = colorFor(id);
    let path = ''; for (const [j, p] of samples.entries()) path += j ? ' H ' + x(p.updated_at) + ' V ' + y(p.probabilities[id]) : 'M ' + x(p.updated_at) + ' ' + y(p.probabilities[id]);
    chart.append(svgNode('path', {d: path, fill: 'none', stroke: color, 'stroke-width': 2.5, 'data-agent': id}));
    for (const p of samples) { const dot = svgNode('circle', {cx: x(p.updated_at), cy: y(p.probabilities[id]), r: 3, fill: color}); dot.append(svgNode('title', {}, (s.players[id]?.name || id) + ' ' + (p.probabilities[id] * 100).toFixed(1) + '% · ' + stamp(p.updated_at))); chart.append(dot); }
  }
  if (s.ended_at) chart.append(svgNode('line', {x1: x(s.ended_at), x2: x(s.ended_at), y1: 25, y2: 210, stroke: '#9aa7c7', 'stroke-dasharray': '4 4'}));
  slider.hidden = history.length === 0; slider.max = String(Math.max(0, history.length - 1));
  if (!history.length) { detail.textContent = s.ended_at ? 'No Jev estimates were recorded for this match.' : 'Waiting for agent activity and the first Jev estimate.'; chart.onpointermove = null; return; }
  const cursorLine = svgNode('line', {y1: 25, y2: 210, stroke: '#edf2ef', 'stroke-opacity': .4}); chart.append(cursorLine);
  function inspect(index) { const p = history[index]; slider.value = String(index); cursorLine.setAttribute('x1', x(p.updated_at)); cursorLine.setAttribute('x2', x(p.updated_at)); detail.textContent = stamp(p.updated_at) + ' · ' + ids.filter(id => Number.isFinite(p.probabilities[id])).map(id => (s.players[id]?.name || id) + ' ' + (p.probabilities[id] * 100).toFixed(1) + '%').join(' / ') + ' · Evidence through event ' + (p.event_seq ?? '—') + (p.as_of ? ' at ' + stamp(p.as_of) : ''); }
  inspect(chartSelected === null ? history.length - 1 : Math.min(chartSelected, history.length - 1));
  slider.oninput = () => { chartSelected = Number(slider.value); inspect(chartSelected); };
  chart.onpointermove = e => { const rect = chart.getBoundingClientRect(); if (!rect.width) return; const t = start + ((e.clientX - rect.left) * 960 / rect.width - 55) / 865 * span; let nearest = 0; history.forEach((p, i) => { if (Math.abs(p.updated_at - t) < Math.abs(history[nearest].updated_at - t)) nearest = i; }); chartSelected = nearest; inspect(nearest); };
  chart.onpointerleave = () => { chartSelected = null; inspect(history.length - 1); };
}
function renderPrediction() {
  renderForecastChart(latest || {players: {}});
  const prediction = latest?.prediction || {};
  const key = JSON.stringify([prediction, latest?.match_id, playersForView().map(p => [p.id, p.name, p.state])]);
  if (key === renderedPrediction) return;
  renderedPrediction = key;
  const labels = {waiting: 'Waiting', live: 'Live estimate', unavailable: 'Unavailable', disabled: 'Disabled', final: 'Final referee result', stopped: 'Stopped'};
  const status = prediction.status || 'waiting';
  $('prediction-status').textContent = labels[status] || status;
  $('prediction-status').className = `status-chip ${status}`;
  $('prediction-panel').classList.toggle('stale', ['unavailable', 'disabled', 'stopped'].includes(status));
  $('prediction-title').textContent = status === 'final' ? 'RECORDED OUTCOME' : 'OUTCOME FORECAST';
  $('prediction-download').hidden = !latest?.match_id;
  $('prediction-note').textContent = status === 'final' ? 'Actual result from the external referee. Earlier Jev forecasts remain in the recording.' : prediction.message || 'Win chances assuming a sole winner, based on recent agent logs. Odds total 100% across agents.';
  const rows = $('prediction-rows'), players = playersForView();
  rows.replaceChildren();
  const percent = value => Number.isFinite(value) ? `${Math.round(Math.min(1, Math.max(0, value)) * 100)}%` : '—';
  for (const [id, probability] of Object.entries(prediction.probabilities || {})) {
    if (!Number.isFinite(probability)) continue;
    const value = Math.min(1, Math.max(0, probability)), index = players.findIndex(p => p.id === id), player = players[index];
    const row = node('div', null, `odds-row${id === 'draw' ? ' draw' : ''}`); row.dataset.player = id; row.style.setProperty('--forecast-color', index < 0 ? '#aab4c7' : colorOf(player, index));
    const label = outcomeLabel(id);
    const name = node(player ? 'button' : 'span', label, 'odds-name');
    if (player) { name.type = 'button'; name.setAttribute('aria-label', `Follow ${label}, ${percent(value)} forecast`); name.onclick = () => selectPlayer(id); }
    const track = node('div', null, 'odds-track'); track.setAttribute('role', 'meter'); track.setAttribute('aria-label', `${label} ${status === 'final' ? 'recorded outcome' : 'win probability'}`); track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', '100'); track.setAttribute('aria-valuenow', String(value * 100));
    const fill = node('div', null, 'odds-fill'); fill.style.width = `${value * 100}%`; track.append(fill);
    row.append(name, track, node('span', `${(value * 100).toFixed(1)}%`, 'odds-percent mono'));
    const factors = prediction.factors?.[id], detail = node('div', null, 'odds-factors');
    if (player?.state === 'eliminated') detail.append(node('span', 'Eliminated', 'forecast-eliminated'));
    else if (factors) {
      if (typeof factors.strategy === 'string' && factors.strategy) detail.append(node('span', factors.strategy, 'forecast-strategy'));
      detail.append(node('span', `Near-term danger ${percent(factors.danger)}`), node('span', `Progress evidence ${percent(factors.progress)}`));
    }
    row.append(detail); rows.append(row);
  }
  if (!rows.children.length) rows.append(node('p', status === 'waiting' ? 'Forecasts will appear when match evidence is available.' : 'No probabilities are available for this match.', 'empty'));
  const parts = [];
  if (prediction.model) parts.push(prediction.model);
  if (prediction.as_of) parts.push(`Evidence as of ${stamp(prediction.as_of)}`);
  if (Number.isFinite(prediction.confidence)) parts.push(`Model confidence ${percent(prediction.confidence)}`);
  if (Number.isFinite(prediction.latency_ms)) parts.push(`${prediction.latency_ms} ms`);
  if (Number.isFinite(prediction.evaluations) && prediction.evaluations) parts.push(`${prediction.evaluations} evaluations`);
  $('prediction-meta').textContent = parts.join(' · ');
}

/* --- Page chrome, controls, polling ---------------------------------------- */
function updateControls() {
  const running = active();
  $('start').disabled = !ready || !connected || running || busy;
  $('start').hidden = !!running;
  $('stop').hidden = !running;
  $('stop').disabled = busy;
  $('configure').disabled = !!running;
  $('start-label').textContent = latest?.match_id ? 'Run another match' : 'Enter the arena';
  $('export').disabled = !latest?.match_id;
}
function tick() {
  const limit = latest?.config?.duration_seconds || config?.duration_seconds || 300;
  const elapsed = latest?.started_at ? (latest.ended_at || Date.now() / 1000) - latest.started_at : 0;
  const left = limit - elapsed;
  $('clock').textContent = duration(left);
  $('clock-sub').textContent = latest?.phase === 'running' ? (left < 60 ? 'CLUTCH · FINAL MINUTE' : 'TIME REMAINING') : latest?.ended_at ? 'FINAL' : 'TIME REMAINING';
}
function render() {
  if (!config) return;
  const players = playersForView(), state = latest?.phase || 'idle';
  const phase = {idle: 'LOBBY', preparing: 'PREPARING', running: 'LIVE', finishing: 'FINISHING', finished: 'FINISHED', error: 'INTERRUPTED', interrupted: 'INTERRUPTED'}[state] || state.toUpperCase();
  $('phase').textContent = phase;
  $('live-tag').textContent = state === 'running' ? '● LIVE' : phase; $('live-tag').className = `live-tag${state === 'running' ? ' live' : latest?.ended_at ? ' done' : ''}`;
  const standing = Object.keys(latest?.players || {}).length ? players.filter(p => p.alive).length : players.length;
  $('standing').replaceChildren(document.createTextNode(`${standing} `), node('span', `/ ${players.length}`));
  $('result').textContent = latest?.result || ''; $('result').hidden = !latest?.result;
  $('event-count').textContent = `${events.length} EVENTS`;
  $('recording').textContent = latest?.match_id ? `MATCH ${latest.match_id}` : 'LOCAL RECORDING';
  const objective = (latest?.config?.prompt || config.prompt || '').split('\n').find(line => line.trim() && !line.trim().startsWith('GAME:'));
  $('objective-label').textContent = /LAST AGENT STANDING/i.test(latest?.config?.prompt || config.prompt) ? 'Last agent standing' : objective || 'Custom objective';
  tick(); renderKernelObserver(); renderArena(); renderObservation(); renderPlayFeed(); renderPrediction(); updateControls();
}
async function jsonRequest(url, options) {
  let response = await fetch(url, options);
  let data = await response.json();
  if (response.status === 403 && data.error === 'Invalid control token' && options?.headers?.['X-Arena-Control']) {
    const page = await fetch('/', {cache: 'no-store'});
    if (!page.ok) throw new Error('Cannot refresh dashboard session. Reload the page.');
    const fresh = new DOMParser().parseFromString(await page.text(), 'text/html').querySelector('meta[name="arena-control"]')?.content;
    if (!fresh) throw new Error('Dashboard session changed. Reload the page.');
    controlToken = fresh;
    response = await fetch(url, {...options, headers: {...options.headers, 'X-Arena-Control': fresh}});
    data = await response.json();
  }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
async function refresh() {
  try {
    const state = await jsonRequest(`/api/state?after=${cursor}&match_id=${encodeURIComponent(matchId || '')}`);
    if (state.events_reset || state.match_id !== matchId) { events = []; highlightedSeq = null; renderedReport = ''; renderedFeed = ''; renderedTerminal = ''; animatedSeq = null; animationQueue = []; $('play-feed').replaceChildren(); }
    matchId = state.match_id;
    const seen = new Set(events.map(row => row.seq));
    const fresh = (state.events || []).filter(row => !seen.has(row.seq));
    events.push(...fresh);
    events = events.slice(-1500); cursor = state.event_seq || 0; latest = state;
    connected = true; $('connection').textContent = 'Referee connected'; $('connection-dot').parentElement.className = 'connection connected';
    render();
    enqueueAnimations(fresh);
  } catch (error) {
    connected = false; $('connection').textContent = 'Disconnected · retrying'; $('connection-dot').parentElement.className = 'connection disconnected'; renderKernelObserver(); updateControls();
  }
}
function errorMessage(message, target = 'error') { $(target).textContent = message || ''; $(target).hidden = !message; }
async function control(action) {
  busy = true; updateControls(); errorMessage('');
  try {
    await jsonRequest(`/api/${action}`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Arena-Control': controlToken}, body: JSON.stringify(action === 'start' ? config : {})});
    await refresh();
  } catch (error) { errorMessage(error.message); }
  busy = false; updateControls();
}
function labeledInput(title, id, className, input) {
  const label = node('label', title, className); label.htmlFor = id; input.id = id; label.append(input); return label;
}
function renderRoster() {
  $('roster').replaceChildren(); $('roster-count').textContent = `${setupDraft.length} / 5`; $('add-player').disabled = setupDraft.length >= 5;
  setupDraft.forEach((p, index) => {
    const card = node('div', null, 'roster-card'); card.style.setProperty('--player-color', colorOf(p, index));
    card.append(node('div', null, 'roster-avatar'));
    const name = node('input'); name.value = p.name; name.required = true; name.maxLength = 48; name.oninput = () => p.name = name.value;
    card.append(labeledInput('Contender name', `name-${index}`, 'roster-name', name));
    const provider = node('select');
    Object.entries(providers).forEach(([id, info]) => { const option = node('option', info.label); option.value = id; provider.append(option); }); provider.value = p.harness;
    provider.onchange = () => { p.harness = provider.value; p.model = providers[p.harness]?.default || ''; delete p.base_url; renderRoster(); };
    card.append(labeledInput('Model provider / harness', `provider-${index}`, 'roster-provider', provider));
    const info = providers[p.harness] || {}, models = info.models || [], select = node('select'), controls = node('div', null, 'roster-model-controls');
    const known = models.some(model => model.id === p.model) && !(p.harness === 'compatible' && !p.model);
    models.filter(model => p.harness !== 'compatible' || model.id).forEach(model => { const option = node('option', `${model.label}${model.id ? ` · ${model.id}` : ''}`); option.value = model.id; select.append(option); });
    const other = node('option', 'Custom model ID…'); other.value = '__custom__'; select.append(other); select.value = known ? p.model : '__custom__'; select.id = `model-${index}`;
    const custom = node('input'); custom.value = known ? '' : p.model; custom.hidden = known; custom.placeholder = 'Exact model ID'; custom.maxLength = 160; custom.setAttribute('aria-label', `Contender ${index + 1} custom model ID`); custom.oninput = () => p.model = custom.value; custom.required = !known;
    select.onchange = () => { custom.hidden = select.value !== '__custom__'; custom.required = !custom.hidden; p.model = custom.hidden ? select.value : custom.value; if (!custom.hidden) custom.focus(); };
    controls.append(select, custom);
    const modelLabelNode = node('label', 'Model', 'roster-model'); modelLabelNode.htmlFor = select.id; modelLabelNode.append(controls); card.append(modelLabelNode);
    const harnessType = info.harness_type === 'shared' || ['gemini', 'grok', 'compatible'].includes(p.harness) ? 'Shared tool harness' : `Native ${p.harness === 'codex' ? 'Codex' : 'Claude Code'}`;
    const auth = info.configured === false ? ` · ${info.credential_hint || 'Credentials need setup on the host.'}` : info.configured === true ? ' · Credentials configured' : '';
    card.append(node('p', `${harnessType}${auth}`, 'roster-provider-info'));
    if (p.harness === 'compatible') {
      const endpoint = node('input'); endpoint.type = 'url'; endpoint.placeholder = 'http://host.docker.internal:11434/v1'; endpoint.value = p.base_url || info.base_url || ''; endpoint.required = true; endpoint.oninput = () => p.base_url = endpoint.value;
      if (!p.base_url && endpoint.value) p.base_url = endpoint.value;
      card.append(labeledInput('API base URL (reachable from the arena computer)', `endpoint-${index}`, 'roster-endpoint', endpoint));
    }
    const remove = node('button', 'Remove', 'roster-remove'); remove.type = 'button'; remove.disabled = setupDraft.length <= 2; remove.setAttribute('aria-label', `Remove contender ${index + 1}`); remove.onclick = () => { setupDraft.splice(index, 1); renderRoster(); }; card.append(remove);
    $('roster').append(card);
  });
}
function openSetup() {
  if (active() || !config) return;
  setupDraft = clone(config.players);
  $('duration').value = config.duration_seconds; $('interval').value = config.turn_interval_seconds; $('prompt').value = config.prompt;
  errorMessage('', 'setup-error'); renderRoster(); $('setup-dialog').showModal();
}
$('add-player').onclick = () => {
  if (setupDraft.length >= 5) return;
  const ids = Object.keys(providers), harness = ids[setupDraft.length % ids.length] || 'codex';
  let index = setupDraft.length + 1;
  while (setupDraft.some(p => p.name === `Gladiator ${index}`)) index++;
  setupDraft.push({name: `Gladiator ${index}`, harness, model: providers[harness]?.default || ''}); renderRoster(); $('roster').lastElementChild.scrollIntoView({block: 'nearest'});
};
$('setup-form').onsubmit = event => {
  event.preventDefault();
  if (active()) return;
  const names = setupDraft.map(p => p.name.trim().toLowerCase());
  if (new Set(names).size !== names.length) { errorMessage('Give each contender a different name.', 'setup-error'); return; }
  if (!$('prompt').value.trim()) { errorMessage('Add an objective for the contenders.', 'setup-error'); return; }
  config = {players: setupDraft.map((p, i) => ({...p, id: `agent-${i + 1}`, name: p.name.trim(), model: p.model.trim()})), duration_seconds: Number($('duration').value), turn_interval_seconds: Number($('interval').value), prompt: $('prompt').value};
  try { localStorage.setItem('gladiator-match-config', JSON.stringify(config)); } catch (_) {}
  $('setup-dialog').close(); render();
};
function selectTab(tab) {
  ['terminal', 'stats'].forEach(id => { const on = id === tab; $(`${id}-tab`).setAttribute('aria-selected', String(on)); $(`${id}-tab`).tabIndex = on ? 0 : -1; $(`${id}-view`).hidden = !on; });
}
['terminal', 'stats'].forEach(id => {
  $(`${id}-tab`).onclick = () => selectTab(id);
  $(`${id}-tab`).onkeydown = event => { if (['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const next = event.key === 'Home' ? 'terminal' : event.key === 'End' ? 'stats' : id === 'terminal' ? 'stats' : 'terminal'; selectTab(next); $(`${next}-tab`).focus(); } };
});
$('expand-terminal').onclick = () => { const expanded = $('expand-terminal').getAttribute('aria-expanded') !== 'true'; $('expand-terminal').setAttribute('aria-expanded', String(expanded)); $('expand-terminal').textContent = expanded ? 'Collapse terminal ↙' : 'Expand terminal ↗'; $('terminal-feed').classList.toggle('expanded', expanded); if ($('follow').checked) $('terminal-feed').scrollTop = $('terminal-feed').scrollHeight; };
$('configure').onclick = openSetup;
$('close-setup').onclick = () => $('setup-dialog').close();
$('start').onclick = () => control('start'); $('stop').onclick = () => control('stop');
$('rules').onclick = () => { $('objective-text').textContent = latest?.config?.prompt || config?.prompt || ''; $('objective-dialog').showModal(); };
$('close-objective').onclick = () => $('objective-dialog').close();
$('export').onclick = () => $('export-dialog').showModal(); $('close-export').onclick = () => $('export-dialog').close();
$('close-funds').onclick = () => $('funds-dialog').close();
$('follow').onchange = () => { if ($('follow').checked) { $('terminal-feed').scrollTop = $('terminal-feed').scrollHeight; $('play-feed').scrollTop = $('play-feed').scrollHeight; } };
$('add-funds').onclick = addFunds;
$('place-bet').onclick = placeBet;
$('stake').oninput = renderSlip;
document.querySelectorAll('.quick button').forEach(btn => btn.onclick = () => { const current = Math.max(0, Math.floor(Number($('stake').value) || 0)); $('stake').value = btn.dataset.add === 'max' ? Math.max(1, balance) : current + Number(btn.dataset.add); renderSlip(); });
async function init() {
  try {
    const [settings, catalog] = await Promise.all([jsonRequest('/api/config'), jsonRequest('/api/models')]);
    providers = catalog.providers; config = settings;
    try { const stored = JSON.parse(localStorage.getItem('gladiator-match-config')); if (stored?.players?.length >= 2 && stored.players.length <= 5 && stored.players.every(p => providers[p.harness])) config = stored; } catch (_) {}
    config.players = config.players.map((p, i) => ({...p, id: `agent-${i + 1}`}));
    if (window.ArenaStage) { window.ArenaStage.mount('arena-game'); window.ArenaStage.onSelect(selectPlayer); }
    ready = true; await refresh(); await refreshBetting(); fundsReturn();
  } catch (error) { errorMessage(`Could not load arena settings: ${error.message}`); }
  setTimeout(poll, 1000);
}
async function poll() { if (!ready) { await init(); return; } await Promise.all([refresh(), refreshBetting()]); setTimeout(poll, 1000); }
setInterval(tick, 250);
init();
