# Strategy learning

This is a contextual bandit around frozen agents, not LLM weight training.
Enable **Learn from this match** in match setup. The default is off. Every
contestant receives one strategy selected before the match; no paid matches are
started automatically. Normal matches keep their existing instructions.

## First-time setup

1. Run both SQL migrations in filename order from `supabase/migrations/` in your existing
   project's SQL editor, or apply it through your authorized migration tool.
   The final tables are `learning_strategies`, `learning_matches`,
   `learning_decisions`, and `learning_checkpoints`; RPCs are `learning_begin` and
   `learning_finish`. If you already applied `202610040001`, apply only
   `202610040002_learning_names.sql` next: it preserves existing data and access
   controls. Migration tooling should track application of each migration.
2. Provide `SUPABASE_URL` and `SUPABASE_SECRET_KEY` in the backend environment or
   host `.env`. The backend loads those values lazily. They must not be copied
   into the frontend, Compose environment, or contestant containers.
3. Restart `python3 dashboard.py`, refresh the page, open match setup, and enable
   learning. Selected strategies appear during preparation. Missing storage or
   migration prevents a learning match from launching, with a visible error.

No publishable key, JWKS endpoint, browser database access, Python SDK, or GPU is
needed. The Python standard library calls Supabase's REST/RPC endpoints using the
backend key. Existing native and compatible-API harnesses are supported.
Application API keys do not grant schema-migration access.

## How selection works

There are four immutable, versioned choices in `arena_learning.py`: baseline
(empty addition), defense-first, investigation-first, and verification-first.
The catalog hash changes whenever their text changes. Instructions never change
the game objective, permission boundaries, lifecycle rules, or provider policies.

One shared controller serves all contestants. For each strategy it computes a
mean reward across the most recent 200 scored learning matches in the same
scope. A contestant/matchup estimate combines its observed rewards with five
pseudo-observations at that shared mean. Unseen strategies start at 0.5.

Each contestant chooses independently from a frozen pre-match snapshot:
20% uniform exploration plus 80% exploitation, uniformly split among strategies
with the best estimated reward. With four choices, every strategy has at least a
5% selection probability. With no data, selection is uniform. This probability
is the chance the controller picks the strategy, **not** a predicted win chance.
The exact distribution, selected strategy, estimated rewards, and snapshot hash
are recorded for every contestant. Matchup context includes harness/model,
slot, opponent models, duration, turn interval, and hashes of custom endpoints.
Display names do not affect learning.

Scope separates objective text, strategy/algorithm versions, and fingerprints
of the tracked container/harness environment source. It is not a guarantee of
bit-identical Docker builds or provider models: pin external images/model IDs for
controlled comparisons. Source edits intentionally start a new learning scope.
Fixed IDs are preferable to moving provider defaults.

## Rewards and exclusions

- Sole winner: 1.0.
- Timeout with k survivors: each survivor receives 1/k.
- Eliminated contestant: 0.1, including simultaneous elimination of everyone.
- Cancellation, interrupted referee, provider/policy errors, incomplete log
  draining, or infrastructure-invalid episode: no contestant gets an update.

Failures are latched for the match, even if a subsequent turn recovers. A failed
shell command alone is not an infrastructure error. Referee outcomes determine
rewards. Jev probabilities never enter selection or reward calculation.

## Stored records and recovery

Supabase stores immutable catalogs, match configuration metadata, per-contestant
decisions and rewards, and controller checkpoints. Checkpoints contain the
bounded source history and pooled/context-specific sufficient statistics. Policy
selection is rebuilt deterministically from committed scored outcomes; the UI
shows its content-addressed snapshot version.

Full agent logs remain in the existing local `.runs` recordings. Supabase records
an artifact prefix referencing those local files, not a publicly downloadable
object-storage URL. Full transcript export/upload is outside this version.

Each match also has `<match-id>.learning.json`, available in the recording
export dialog. Outcomes are written atomically to `.runs/learning-outbox/`
before upload. A background worker retries every 30 seconds. Database finalization
locks the match and commits all rewards and the checkpoint in one transaction.
Duplicate submissions cannot count an outcome twice.

Interrupted registration intents are persisted in `.runs/learning-registrations/`
and resolved as skipped. A restart skips unfinished episodes; an already queued
outcome takes priority over an older local snapshot. Pending results must sync
before another learning match starts, so new choices cannot silently use stale
history. Ordinary matches continue working during database outages.

This version assumes one active dashboard/controller instance. Both agents learn
against changing opponents, so reward trends alone do not demonstrate causal
improvement. Use learning-disabled baseline matches with comparable settings;
checkpoint-versus-frozen-baseline evaluation is a future extension. Historical
matches without strategy assignments are not imported as bandit experience.

## Checks

```
python3 -m unittest discover -s tests
node --check dashboard.js
node --test tests/test_learning_ui.cjs
python3 tests/integration_learning.py
```

The Postgres integration test uses a disposable Docker container with no exposed
ports, no credentials, and no `.env` access. Its default image is
`pgvector/pgvector:pg16`; set `LEARNING_TEST_POSTGRES_IMAGE` to another compatible
Postgres image if needed. Tests never start paid agent matches.

## Future phase: intermediate feedback

Retain the planned follow-up: evaluate Jev-based intermediate rewards and
within-match controller decisions. Before enabling it, compare estimated
probability changes against verified effects, investigate reward gaming and
noise, and choose a sequential decision interface supported by each harness.
A once-per-match bandit cannot attribute a probability change to an individual
tool action. This follow-up is deliberately not enabled in v1.
