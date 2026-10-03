'use strict';
const $ = id => document.getElementById(id);
const controlToken = document.querySelector('meta[name="arena-control"]').content;
const palette = ['#bff574', '#eeab88', '#9eacff', '#76d8e0', '#e8a1ef'];
const activePhases = ['preparing', 'running', 'finishing'];
let config = null, providers = {}, latest = null, events = [], selected = null, ready = false, connected = false, busy = false;
let cursor = 0, matchId = null, renderedFeed = '', renderedTerminal = '', setupDraft = [], highlightedSeq = null, renderedReport = '', renderedPrediction = '';
const clone = value => JSON.parse(JSON.stringify(value));
const node = (tag, value, className) => { const n = document.createElement(tag); if (value != null) n.textContent = value; if (className) n.className = className; return n; };
const colorFor = index => palette[index % palette.length];
const duration = value => { const n = Math.max(0, Math.floor(value || 0)); return `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`; };
const stamp = value => new Date(value * 1000).toLocaleTimeString([], {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'});
const compact = value => value == null ? '—' : new Intl.NumberFormat(undefined, {notation: 'compact', maximumFractionDigits: 1}).format(value);
const bytes = value => value == null ? '—' : value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GB` : `${(value / 1024 ** 2).toFixed(1)} MB`;
const active = () => latest && activePhases.includes(latest.phase);
const providerLabel = harness => providers[harness]?.label || ({codex: 'Codex', claude: 'Claude Code', gemini: 'Gemini', grok: 'Grok', compatible: 'OpenAI compatible'})[harness] || harness;
const modelLabel = player => player.model || player.configured_model || 'Harness default';
function mascot(index) {
  const c = colorFor(index), ornament = [
    '<path d="M45 26V10h10v16M37 21h26"/>',
    '<path d="m32 31-12-16 6 25m42-9 12-16-6 25"/>',
    '<path d="m50 9-10 17h20Zm-17 19 17-8 17 8"/>',
    '<path d="M29 30 17 21v15l13 8m41-14 12-9v15L70 44"/>',
    '<path d="m29 29-3-17 15 11 9-16 9 16 15-11-3 17"/>'
  ][index % 5];
  return `<svg class="mascot" viewBox="0 0 100 110" aria-hidden="true"><ellipse cx="50" cy="103" rx="29" ry="4" fill="${c}" opacity=".11"/><g fill="#202a35" stroke="${c}" stroke-width="1.5" stroke-linejoin="round">${ornament}<path d="m24 77-12 8 4 15h18l2-20m40-3 12 8-4 15H66l-2-20"/><path d="m30 75 20-9 20 9 5 25H25Z" fill="#171f2b"/><path d="m50 76 12 6-3 11-9 7-9-7-3-11Z" fill="${c}" fill-opacity=".12"/><path d="m50 29 25 12-4 26-21 15-21-15-4-26Z" fill="#25313b"/><path d="m27 43 23 6 23-6-3 22-20 14-20-14Z" fill="#121a25"/><path d="m50 31 1 18m-1 7v18" stroke-opacity=".5"/><path d="m32 52 13 4-2 6-10-3Zm36 0-13 4 2 6 10-3Z" fill="${c}" stroke="none"/><path d="m42 68 8 4 8-4"/><path d="m22 83-5 8m61-8 5 8" stroke-opacity=".6"/></g><path d="m46 83 4-3 4 3v7l-4 3-4-3Z" fill="${c}"/></svg>`;
}
function playersForView() {
  const live = Object.values(latest?.players || {});
  return live.length ? live : (config?.players || []).map((p, index) => ({...p, id: `agent-${index + 1}`, activity: 'ready', state: 'ready'}));
}
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
function positions(count) {
  if (count === 2) return [[25, 50], [75, 50]];
  if (count === 3) return [[50, 24], [24, 72], [76, 72]];
  if (count === 4) return [[25, 27], [75, 27], [25, 75], [75, 75]];
  return [[50, 24], [22, 46], [78, 46], [32, 78], [68, 78]];
}
function renderArena() {
  const players = playersForView();
  if (!players.some(p => p.id === selected)) selected = players[0]?.id;
  $('arena-stage').dataset.count = players.length;
  const rosterKey = players.map(p => p.id).join('|');
  if ($('contenders').dataset.roster !== rosterKey) {
    $('contenders').replaceChildren();
    $('contenders').dataset.roster = rosterKey;
    players.forEach((p, i) => {
      const card = node('button', null, 'contender'); card.type = 'button'; card.dataset.player = p.id; card.style.setProperty('--player-color', colorFor(i));
      card.innerHTML = mascot(i);
      card.append(node('span', null, 'contender-name'), node('span', null, 'contender-model'));
      const badge = node('span', null, 'contender-state'); badge.append(node('span', null, 'state-dot'), node('span', null, 'state-text'));
      card.append(badge, node('span', null, 'contender-callout'));
      card.onclick = () => selectPlayer(p.id); $('contenders').append(card);
    });
  }
  const coords = positions(players.length);
  players.forEach((p, i) => {
    const card = $('contenders').children[i], status = statusOf(p);
    card.style.left = `${coords[i][0]}%`; card.style.top = `${coords[i][1]}%`;
    card.className = `contender ${status.kind}${selected === p.id ? ' selected' : ''}`;
    card.setAttribute('aria-pressed', String(selected === p.id)); card.setAttribute('aria-label', `${p.name}, ${modelLabel(p)}, ${status.text}. Follow contestant.`);
    card.querySelector('.contender-name').textContent = p.name;
    card.querySelector('.contender-model').textContent = modelLabel(p);
    card.querySelector('.state-text').textContent = status.text;
    const retry = p.alive && p.next_turn_at && active() ? `NEXT TURN ${duration(p.next_turn_at - Date.now() / 1000)}` : '';
    card.querySelector('.contender-callout').textContent = status.kind === 'blocked' ? 'PARKED · SESSION ALIVE' : retry || p.current_tool?.name?.split('\n')[0] || (p.turn ? `TURN ${p.turn} · ${p.tool_count || 0} TOOLS` : providerLabel(p.harness));
  });
}
function selectPlayer(id) { selected = id; renderedTerminal = ''; renderArena(); renderObservation(); }
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
function renderContestantReport(player) {
  const status = statusOf(player), report = $('contestant-report'), elimination = player.elimination;
  const show = status.kind === 'eliminated' || status.kind === 'turn_error' || status.kind === 'blocked';
  const key = JSON.stringify([player.id, status.kind, elimination, player.last_error]);
  report.hidden = !show;
  if (key !== renderedReport) {
    renderedReport = key; report.replaceChildren(); report.className = `contestant-report ${status.kind}`;
    if (status.kind === 'eliminated') {
      report.append(node('span', 'ELIMINATION REPORT', 'small-label'), node('strong', elimination?.summary || player.reason || 'The referee observed the contestant session terminate.'));
      const details = node('div', null, 'elimination-details');
      if (elimination?.cause) details.append(node('span', `Cause: ${String(elimination.cause).replaceAll('_', ' ')}`));
      if (elimination?.confidence) details.append(node('span', `Confidence: ${elimination.confidence}`));
      report.append(details);
      const attacker = playersForView().find(p => p.id === elimination?.attacker);
      if (attacker) {
        const link = node('button', `Attributed contender: ${attacker.name} / ${providerLabel(attacker.harness)}`, 'attributed-contender');
        link.type = 'button'; link.onclick = () => selectPlayer(attacker.id); report.append(link);
      } else report.append(node('p', 'Attacker: not established', 'attribution-note'));
      if (elimination?.attacker && elimination.confidence !== 'confirmed') report.append(node('p', 'The process death was observed. Attacker attribution is a correlation with recorded activity.', 'attribution-note'));
      if (Array.isArray(elimination?.evidence_seqs) && elimination.evidence_seqs.length) {
        const links = node('div', null, 'evidence-links'); links.append(node('span', 'Recorded evidence:', 'attribution-note'));
        elimination.evidence_seqs.forEach(seq => links.append(evidenceButton(seq))); report.append(links);
      }
    } else {
      report.append(node('span', status.kind === 'blocked' ? 'POLICY BLOCK · CONTESTANT ALIVE' : 'TURN ERROR · CONTESTANT ALIVE', 'small-label'));
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
  if ($('observer-avatar').dataset.player !== selected) { $('observer-avatar').innerHTML = mascot(index); $('observer-avatar').dataset.player = selected; }
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
  $('telemetry').replaceChildren(...metrics.map(([label, value]) => { const stat = node('div', null, 'stat'); stat.append(node('span', label, 'small-label'), node('strong', value)); return stat; }));
}
function renderPlayFeed() {
  const key = `${matchId}:${events[0]?.seq}:${events.at(-1)?.seq}:${highlightedSeq}`;
  if (key === renderedFeed) return;
  renderedFeed = key;
  const feed = $('play-feed'), scroll = feed.scrollTop;
  const rows = events.filter(row => row.kind !== 'usage').slice(-180);
  if (!rows.length) { const empty = node('div', null, 'empty-feed'); empty.append(node('strong', 'The stage is set.'), node('p', 'Actual tool activity and referee decisions will appear here.')); feed.replaceChildren(empty); return; }
  feed.replaceChildren();
  const players = playersForView();
  rows.forEach(row => {
    const index = players.findIndex(p => p.id === row.player), player = players[index];
    const item = node('article', null, `play-row ${row.kind}${row.seq === highlightedSeq ? ' evidence-highlight' : ''}`); item.dataset.seq = row.seq; item.style.setProperty('--player-color', index < 0 ? '#c4cedd' : colorFor(index));
    const avatar = node('span', index < 0 ? 'R' : `${index + 1}`.padStart(2, '0'), 'play-avatar');
    const body = node('div', null, 'play-body'), meta = node('div', null, 'play-meta');
    meta.append(node('strong', player?.name || 'Referee'), node('time', stamp(row.time)));
    let brief = row.text || '';
    if (row.kind === 'tool') brief = `${row.data?.status === 'running' ? 'Running' : row.data?.status === 'failed' ? 'Tool failed:' : 'Completed:'} ${brief.split('\n')[0]}`;
    body.append(meta, node('p', brief.length > 180 ? `${brief.slice(0, 177)}…` : brief), node('span', `${row.kind.toUpperCase()} · #${row.seq}`, 'event-kind'));
    if (row.kind === 'attribution' || row.kind === 'elimination') {
      const attribution = row.data?.elimination || row.data || {};
      const attacker = players.find(p => p.id === attribution.attacker);
      if (attacker) body.append(node('p', `Attributed contender: ${attacker.name} / ${providerLabel(attacker.harness)}`, 'attribution-detail'));
      if (attribution.confidence) body.append(node('span', `CONFIDENCE: ${String(attribution.confidence).toUpperCase()}`, 'event-kind'));
      if (Array.isArray(attribution.evidence_seqs) && attribution.evidence_seqs.length) {
        const links = node('div', null, 'evidence-links');
        attribution.evidence_seqs.forEach(seq => links.append(evidenceButton(seq))); body.append(links);
      }
    }
    item.append(avatar, body);
    if (player) { item.tabIndex = 0; item.setAttribute('role', 'button'); item.setAttribute('aria-label', `Follow ${player.name}: ${brief}`); item.onclick = () => selectPlayer(player.id); item.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectPlayer(player.id); } }; }
    feed.append(item);
  });
  feed.scrollTop = $('follow').checked ? feed.scrollHeight : scroll;
}
function updateControls() {
  const running = active();
  $('start').disabled = !ready || !connected || running || busy;
  $('start').hidden = !!running;
  $('stop').hidden = !running;
  $('stop').disabled = busy;
  $('configure').disabled = !!running;
  $('configure-nav').disabled = !!running;
  $('start-label').textContent = latest?.match_id ? 'Run another match' : 'Enter the arena';
  $('export').disabled = !latest?.match_id;
}
function tick() {
  const limit = latest?.config?.duration_seconds || config?.duration_seconds || 300;
  const elapsed = latest?.started_at ? (latest.ended_at || Date.now() / 1000) - latest.started_at : 0;
  $('clock').textContent = duration(limit - elapsed);
}
function renderPrediction() {
  const prediction = latest?.prediction || {};
  const key = JSON.stringify([prediction, latest?.match_id, playersForView().map(p => [p.id, p.name, p.state])]);
  if (key === renderedPrediction) return;
  renderedPrediction = key;
  const labels = {waiting: 'Waiting', live: 'Live estimate', unavailable: 'Unavailable', disabled: 'Disabled', final: 'Final referee result', stopped: 'Stopped'};
  const status = prediction.status || 'waiting';
  $('prediction-status').textContent = labels[status] || status;
  $('prediction-status').className = `forecast-status ${status}`;
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
    const row = node('div', null, `odds-row${id === 'draw' ? ' draw' : ''}`); row.dataset.player = id; row.style.setProperty('--forecast-color', index < 0 ? '#aab4c7' : colorFor(index));
    const label = id === 'draw' ? 'Draw' : player?.name || id;
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
  if (!rows.children.length) rows.append(node('p', status === 'waiting' ? 'Forecasts will appear when match evidence is available.' : 'No probabilities are available for this match.', 'forecast-empty'));
  const parts = [];
  if (prediction.model) parts.push(prediction.model);
  if (prediction.as_of) parts.push(`Evidence as of ${stamp(prediction.as_of)}`);
  if (Number.isFinite(prediction.confidence)) parts.push(`Model confidence ${percent(prediction.confidence)}`);
  if (Number.isFinite(prediction.latency_ms)) parts.push(`${prediction.latency_ms} ms`);
  if (Number.isFinite(prediction.evaluations) && prediction.evaluations) parts.push(`${prediction.evaluations} evaluations`);
  $('prediction-meta').textContent = parts.join(' · ');
}
function render() {
  if (!config) return;
  const players = playersForView(), state = latest?.phase || 'idle';
  const phase = {idle: 'LOBBY', preparing: 'PREPARING', running: 'LIVE MATCH', finishing: 'FINISHING', finished: 'FINISHED', error: 'INTERRUPTED', interrupted: 'INTERRUPTED'}[state] || state.toUpperCase();
  $('phase').textContent = phase;
  $('phase-dot').classList.toggle('live', state === 'running'); $('arena-live').classList.toggle('live', state === 'running');
  $('arena-subtitle').textContent = state === 'idle' ? 'AWAITING THE STARTING SIGNAL' : state === 'running' ? 'LIVE · EVERY MOVE OBSERVED' : phase;
  const standing = Object.keys(latest?.players || {}).length ? players.filter(p => p.alive).length : players.length;
  $('standing').replaceChildren(document.createTextNode(`${standing} `), node('span', `/ ${players.length}`));
  $('result').textContent = latest?.result || ''; $('result').hidden = !latest?.result;
  $('event-count').textContent = `${events.length} EVENTS`;
  $('recording').textContent = latest?.match_id ? 'MATCH RECORDED' : 'LOCAL RECORDING'; $('recording').title = latest?.match_id || '';
  const objective = (latest?.config?.prompt || config.prompt || '').split('\n').find(line => line.trim() && !line.trim().startsWith('GAME:'));
  $('objective-label').textContent = /LAST AGENT STANDING/i.test(latest?.config?.prompt || config.prompt) ? 'Last agent standing' : objective || 'Custom objective';
  tick(); renderArena(); renderObservation(); renderPlayFeed(); renderPrediction(); updateControls();
}
async function jsonRequest(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
async function refresh() {
  try {
    const state = await jsonRequest(`/api/state?after=${cursor}&match_id=${encodeURIComponent(matchId || '')}`);
    if (state.events_reset || state.match_id !== matchId) { events = []; highlightedSeq = null; renderedReport = '';  renderedFeed = ''; renderedTerminal = ''; $('play-feed').replaceChildren(); }
    matchId = state.match_id;
    const seen = new Set(events.map(row => row.seq));
    events.push(...(state.events || []).filter(row => !seen.has(row.seq)));
    events = events.slice(-1500); cursor = state.event_seq || 0; latest = state;
    connected = true; $('connection').textContent = 'Referee connected'; $('connection-dot').parentElement.className = 'connection connected';
    render();
  } catch (error) {
    connected = false; $('connection').textContent = 'Disconnected · retrying'; $('connection-dot').parentElement.className = 'connection disconnected'; updateControls();
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
    const card = node('div', null, 'roster-card'); card.style.setProperty('--player-color', colorFor(index));
    const avatar = node('div', null, 'roster-avatar'); avatar.innerHTML = mascot(index); card.append(avatar);
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
$('configure').onclick = openSetup; $('configure-nav').onclick = openSetup;
$('close-setup').onclick = () => $('setup-dialog').close();
$('start').onclick = () => control('start'); $('stop').onclick = () => control('stop');
$('rules').onclick = () => { $('objective-text').textContent = latest?.config?.prompt || config?.prompt || ''; $('objective-dialog').showModal(); };
$('close-objective').onclick = () => $('objective-dialog').close();
$('export').onclick = () => $('export-dialog').showModal(); $('close-export').onclick = () => $('export-dialog').close();
$('follow').onchange = () => { if ($('follow').checked) { $('terminal-feed').scrollTop = $('terminal-feed').scrollHeight; $('play-feed').scrollTop = $('play-feed').scrollHeight; } };
async function init() {
  try {
    const [settings, catalog] = await Promise.all([jsonRequest('/api/config'), jsonRequest('/api/models')]);
    providers = catalog.providers; config = settings;
    try { const stored = JSON.parse(localStorage.getItem('gladiator-match-config')); if (stored?.players?.length >= 2 && stored.players.length <= 5 && stored.players.every(p => providers[p.harness])) config = stored; } catch (_) {}
    config.players = config.players.map((p, i) => ({...p, id: `agent-${i + 1}`}));
    ready = true; await refresh();
  } catch (error) { errorMessage(`Could not load arena settings: ${error.message}`); }
  setTimeout(poll, 1000);
}
async function poll() { if (!ready) { await init(); return; } await refresh(); setTimeout(poll, 1000); }
setInterval(tick, 250);
init();
