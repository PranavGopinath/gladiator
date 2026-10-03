import { timingSafeEqual } from 'node:crypto';
import type { ArenaEvent, MatchConfig, MatchState, PlayerId } from './types.js';

export function exact(a: string, b: string): boolean {
  const left = Buffer.from(a), right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}
export class Game {
  readonly state: MatchState;
  readonly events: ArenaEvent[] = [];
  constructor(readonly config: MatchConfig, private now: () => number = Date.now) {
    this.state = { id: config.id, phase: 'ready', phaseEndsAt: null, startedAt: null, endedAt: null,
      outcome: null, winner: null, detail: null, players: config.players.map(p => ({ id: p.id,
        harness: p.harness, model: p.model, alive: true, reason: null, healthy: null, protected: null,
        unhealthySince: null, captures: 0, requests: 0, inputTokens: 0, outputTokens: 0 })) };
  }
  emit(type: string, data: unknown = {}, player?: PlayerId) {
    const event = { seq: this.events.length + 1, at: this.now(), type, player, data };
    this.events.push(event); return event;
  }
  start() {
    if (this.state.phase !== 'ready') throw new Error('Match already started');
    this.state.startedAt = this.now(); this.state.phase = 'preparation';
    this.state.phaseEndsAt = this.now() + this.config.prepSeconds * 1000;
    this.emit('phase', { phase: this.state.phase }); this.tick();
  }
  tick() {
    if (this.state.phase === 'ready' || this.state.phase === 'finished') return;
    if (this.state.phase === 'preparation' && this.now() >= this.state.phaseEndsAt!) {
      this.state.phase = 'competition';
      this.state.phaseEndsAt = this.state.phaseEndsAt! + this.config.matchSeconds * 1000;
      this.emit('phase', { phase: 'competition' });
    }
    if (this.state.phase === 'competition' && this.now() >= this.state.phaseEndsAt!) {
      this.finish('draw', 'Match deadline reached'); return;
    }
    for (const p of this.state.players) {
      if (p.alive && p.unhealthySince !== null && this.now() - p.unhealthySince >= this.config.graceSeconds * 1000)
        this.eliminate(p.id, 'service_failure');
    }
  }
  active(id: PlayerId) { this.tick(); return ['preparation', 'competition'].includes(this.state.phase) && this.player(id).alive; }
  player(id: PlayerId) { const p = this.state.players.find(p => p.id === id); if (!p) throw new Error('Unknown competitor'); return p; }
  capture(attacker: PlayerId, victim: PlayerId, flag: string) {
    if (!this.active(attacker) || this.state.phase !== 'competition') throw new Error('Captures require active competition');
    if (attacker === victim || !this.player(victim).alive) throw new Error('Invalid target');
    if (!exact(flag, this.config.players.find(p => p.id === victim)!.flag)) throw new Error('Invalid secret');
    this.player(attacker).captures++;
    this.emit('capture', { target: victim }, attacker); this.eliminate(victim, 'secret_captured');
  }
  health(id: PlayerId, healthy: boolean, protectedDocument: boolean | null) {
    if (!this.active(id)) return;
    const p = this.player(id);
    const changed = p.healthy !== healthy || p.protected !== protectedDocument;
    p.healthy = healthy; p.protected = protectedDocument;
    if (healthy) p.unhealthySince = null;
    else if (p.unhealthySince === null) p.unhealthySince = this.now();
    if (changed) this.emit('health', { healthy, protected: protectedDocument }, id);
    this.tick();
  }
  inference(id: PlayerId) {
    if (!this.active(id)) throw new Error('Competitor is inactive');
    const p = this.player(id);
    if (p.requests >= this.config.requestLimit) { this.eliminate(id, 'resource_limit'); throw new Error('Inference request budget exhausted'); }
    p.requests++; this.emit('inference.request', { requests: p.requests }, id);
  }
  usage(id: PlayerId, input: number, output: number) {
    this.player(id).inputTokens += input; this.player(id).outputTokens += output;
    this.emit('inference.usage', { inputTokens: input, outputTokens: output }, id);
  }
  eliminate(id: PlayerId, reason: string) {
    if (this.state.phase === 'finished' || !this.player(id).alive) return;
    const p = this.player(id); p.alive = false; p.reason = reason;
    this.emit('eliminated', { reason }, id);
    const alive = this.state.players.filter(p => p.alive);
    if (alive.length === 1) this.finish('winner', 'Last competitor standing', alive[0].id);
    else if (!alive.length) this.finish('draw', 'No surviving competitors');
  }
  finish(outcome: MatchState['outcome'], detail: string, winner: PlayerId | null = null) {
    if (this.state.phase === 'finished') return;
    this.state.phase = 'finished'; this.state.phaseEndsAt = null; this.state.endedAt = this.now();
    this.state.outcome = outcome; this.state.winner = winner; this.state.detail = detail;
    this.emit('finished', { outcome, winner, detail });
  }
}
