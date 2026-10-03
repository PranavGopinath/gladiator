export const PLAYERS = ['a', 'b'] as const;
export type PlayerId = typeof PLAYERS[number];
export type Harness = 'claude' | 'codex' | 'scripted';
export type Phase = 'ready' | 'preparation' | 'competition' | 'finished';
export interface PlayerConfig { id: PlayerId; harness: Harness; model: string; token: string; appToken: string; flag: string }
export interface MatchConfig {
  id: string; players: PlayerConfig[]; prepSeconds: number; matchSeconds: number;
  graceSeconds: number; requestLimit: number; outputLimit: number; adminToken: string;
}
export interface PlayerState {
  id: PlayerId; harness: Harness; model: string; alive: boolean; reason: string | null;
  healthy: boolean | null; protected: boolean | null; unhealthySince: number | null;
  captures: number; requests: number; inputTokens: number; outputTokens: number;
}
export interface ArenaEvent { seq: number; at: number; type: string; player?: PlayerId; data: unknown }
export interface MatchState {
  id: string; phase: Phase; phaseEndsAt: number | null; startedAt: number | null;
  endedAt: number | null; outcome: 'winner' | 'draw' | 'invalid' | 'canceled' | null;
  winner: PlayerId | null; detail: string | null; players: PlayerState[];
}
