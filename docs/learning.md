# Strategy learning

This is a contextual bandit around frozen agents, not LLM weight training.
Enable **Learn from this match** in match setup. The default is off. Every
contestant receives one strategy selected before the match; no paid matches are
started automatically. Normal matches keep their existing instructions.

## First-time setup

1. Run all SQL migrations in filename order from `supabase/migrations/` in your existing
   project's SQL editor, or apply it through your authorized migration tool.
   The final tables are `learning_strategies`, `learning_matches`,
   `learning_decisions`, and `learning_checkpoints`; RPCs are `learning_begin` and
   `learning_finish`. Apply only migrations your database has not run yet.
   `202610040002_learning_names.sql` preserves existing history when renaming
   the original tables. `202610040003_learning_experiments.sql` adds experiment
   storage and transactions. Migration tooling should track each application.
2. Provide `SUPABASE_URL` and `SUPABASE_SECRET_KEY` in the backend environment or
   host `.env`. The backend loads those values lazily. They must not be copied
   into the frontend, Compose environment, or contestant containers.
3. Restart `python3 dashboard.py`, refresh the page, open match setup, and enable
   learning, or use the Strategy experiments panel. Selected strategies appear during preparation. Missing storage or
   migration prevents a learning match from launching, with a visible error.

No publishable key, JWKS endpoint, browser database access, Python SDK, or GPU is
needed. The Python standard library calls Supabase's REST/RPC endpoints using the
backend key. Existing native and compatible-API harnesses are supported.
Application API keys do not grant schema-migration access.

## How selection works

There are five versioned choices in `arena_learning.py`: baseline (empty
addition), defense-first, investigation-first, verification-first, and
attack-first. Each experiment saves the catalog text it starts with.
The catalog hash changes whenever their text changes. Instructions never change
the game objective, permission boundaries, lifecycle rules, or provider policies.

One shared controller serves all contestants. For each strategy it computes a
mean reward across the most recent 200 scored learning matches in the same
scope. A contestant/matchup estimate combines its observed rewards with five
pseudo-observations at that shared mean. Unseen strategies start at 0.5.

Each contestant chooses independently from a frozen pre-match snapshot:
20% uniform exploration plus 80% exploitation, uniformly split among strategies
with the best estimated reward. With the current five choices, every strategy has at least a
4% selection probability (20% divided by the catalog size). With no data, selection is uniform. This probability
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

This version assumes one active dashboard/controller instance. In ordinary
learning mode both agents learn against changing opponents, so reward trends
alone do not demonstrate causal improvement. The experiment mode below holds
the opponent fixed and compares frozen strategies. Historical matches without
strategy assignments are not imported as bandit experience.

## One-sided training and frozen evaluation

Learning updates **strategy selection**, not the strategy text, individual tool
actions, or model weights. Ordinary matches still support mixed-model rosters
when learning is off. When learning is on, all contestants must use the same
harness and model, and equivalent compatible endpoints. Existing roster presets
are unchanged; configure the roster yourself before creating an experiment.

1. Configure exactly **two contestants**, with matching harnesses and explicit
   model IDs. Choose the objective, duration, and turn interval as usual.
2. In **Strategy experiments**, choose the learner (first contestant by default).
   Start fresh, or **Find checkpoints** and select a compatible checkpoint.
   Compatibility requires the same algorithm/catalog, task, models, endpoint
   identities, duration, and turn interval. Incompatible rows display a reason.
   The selector lists the latest 100 checkpoints. Imported history is copied;
   the source checkpoint remains unchanged. Its scope is retained as provenance;
   importing across environment revisions is allowed with matching task/context.
3. Click **Create experiment**, then **Run training match** for each attempt.
   Only the learner's outcome updates this experiment's controller. The opponent
   always receives the baseline instructions. The learner alternates physical
   seats on each training attempt, including excluded attempts.
4. After at least one scored training match, click **Freeze & evaluate**. The
   confirmation displays the training count. Freeze saves the controller's
   sufficient statistics, source history, version, and strategy catalog.
5. Click **Run next evaluation** for each of eight **valid** evaluation matches.
   Four use the baseline learner and four use the frozen learned controller,
   all against the fixed baseline opponent. Each arm uses each seat twice.
   The schedule is shuffled in balanced four-match blocks and persisted before
   any evaluation starts. Invalid/canceled attempts retry the same schedule slot
   on the next manual click.

Evaluation selects the highest estimated-reward strategy for the learner's seat,
with a stable strategy-ID tie break and zero exploration. The frozen controller
may still choose baseline if baseline scores best. No evaluation outcome updates
learning, produces a learning checkpoint, or enters the training window. Frozen
experiments cannot resume training; end one and create another, optionally
importing its training checkpoint. Configuration changes in ordinary match setup
do not alter an existing experiment. Environment/catalog changes block further
runs in that experiment rather than silently changing its conditions.

The panel previews the next arm, learner seat, opponent, and evaluation strategy.
The report includes win/loss/draw counts, win rate with a 95% Wilson interval,
median time to victory **among wins only**, win sample size, exclusions/reasons,
and individual runs with controller versions and local recording links. Elapsed
time runs from the common start to the referee outcome, excluding setup/cleanup.
Draws count in the win-rate denominator; canceled or infrastructure-invalid runs
do not. These are descriptive pilot measurements, not a significance test or
proof that training helped. Eight matches can produce wide uncertainty, and a
controller that wins less often can still have a faster median among its wins.
Jev probabilities are not rewards or evaluation metrics in this version.

Experiment matches have **no betting market**, during either training or
assessment. Ordinary matches retain their market. End an active experiment to
return to ordinary matches. Stop an active match before ending its experiment.
No experiment automatically starts a paid match.

### Experiment storage and recovery

Migration `202610040003_learning_experiments.sql` adds:

- `learning_experiments`: revisioned manifest, saved roster/learner, seed provenance,
  bounded training history, frozen snapshot, schedule, and run summaries.
- `learning_experiment_runs`: persisted selections and referee results for every
  training/evaluation attempt, including excluded attempts.
- `learning_experiment_commit`: one transaction for the manifest, run, and (only
  during training) the existing learning decision/reward/checkpoint records.
- `learning_checkpoint_catalog`: compact metadata for checkpoint compatibility.

All new tables enable RLS and deny browser roles; only the backend service role
can access the RPCs. Experiment history has an isolated `experiment:<UUID>`
scope. Each training checkpoint includes up to 200 source matches, including
imported seed matches until they age out. Immutable seed and freeze snapshots
retain provenance independently of that rolling window. Fixed-opponent decisions
are recorded but excluded from pooled/context estimates; legacy decisions without
a `learnable` flag remain eligible.

Local `.runs/experiments/` caches the current manifest. Mutation intents are
written to `.runs/experiment-outbox/` before network calls and retried every
30 seconds. Revision checks, transaction rollback, and duplicate request handling
prevent partial or repeated updates. Pending synchronization blocks new runs.
A restart restores the experiment and excludes any unfinished match; it never
launches the next one. An already queued final result takes precedence over
interruption recovery. **Resume by experiment ID** restores the database manifest
on a fresh dashboard installation, but recording links still need the original
local `.runs` files. Run one dashboard instance per database/controller.

The operator-token-protected `/api/experiments/{create,load,checkpoints,next,freeze,end}`
POST routes power these controls. GET `/api/experiments` exposes the current
summary, `/api/experiments/report` downloads that summary/report with provenance,
and `/api/experiments/recording?match_id=...` serves only a known run's local event
log. Provider/Supabase keys are never included in experiment records.

## Checks

```
python3 -m unittest discover -s tests
node --check dashboard.js
node --test tests/test_learning_ui.cjs tests/test_experiments_ui.cjs
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
