# Agent arena: one computer per agent

A minimal runnable building block for an arena: Debian Linux containers with
root access inside the container, Python, Node, Git, curl, Codex, and Claude Code.
Each starts an agent and a separate supervisor with an HTTP health endpoint.
A monitor on the host samples Docker state and agent status every second.

## Live dashboard and matches

```sh
cd ~/dev/agent-arena
docker compose build codex claude
python3 dashboard.py
```

Open **http://127.0.0.1:8790**. The dashboard shows both activity feeds, commands,
tool output, externally observed process state, health reachability, elapsed time,
and referee events. Use **Start match** (or **Run again**) to recreate the two
computers and run the experiment. **Stop match** ends it without a winner.
Set `ARENA_UI_PORT` to change the local port if necessary.

### Live Jev forecasts

Set `JEV_KEY` in the host's `.env` (or process environment). It is loaded only
when a live match starts, stays in the evaluator, and is never passed to
contestants or returned by the dashboard. The integration uses TypeSafe's
`POST https://api.typesafe.ai/v1/systemone` endpoint with `jev-latest`.

A separate worker evaluates new tool calls, results, messages, and errors,
coalescing bursts to at most one request per second with only one request in flight.
A five-second fallback reevaluates quiet periods as the clock advances. The first
request waits for contestant activity instead of scoring an empty starting state.
Each contestant gets an equally bounded recent-event window and counts of
earlier activity. Commands, results, messages, and errors are the primary
evidence; host-observed survival and time remaining are authoritative. Log
contents are treated as untrusted observations, including claims of victory.

The dashboard shows win probabilities across contestants, conditional on a sole winner, and
typed assessments of recent strategy, near-term danger, and evidence of useful
progress. These are model estimates, not verified facts or calibrated arena
odds. Jev does not generate explanatory prose. Its judgments never determine
elimination or the winner. Eliminations invalidate stale forecasts; the final
display shows the referee's actual result separately from the forecast chart.

Forecasts and final outcomes are saved separately in
`.runs/<match-id>.predictions.jsonl` and can be downloaded from the dashboard.
Up to 3,601 forecasts accompany the state snapshot, covering a full one-hour match
at the maximum evaluation rate.
An interactive step chart plots returned estimates at their receipt times; hover
or use the keyboard-accessible slider to inspect odds and evidence timestamps.
The chart never interpolates new estimates or appends a referee result as a forecast.
API failures show an unavailable status with bounded backoff; invalid keys or
missing credits disable the evaluator while the match continues normally.

Optional **process environment** settings: `JEV_MODEL` selects the model,
`JEV_INTERVAL_SECONDS` selects a 2–60 second interval, and `JEV_ENABLED=0`
disables API calls. For example, `JEV_ENABLED=0 python3 dashboard.py` runs the
arena without Jev. No real key or paid API call is needed for tests.

Contract: [TypeSafe API](https://docs.typesafe.ai/api),
[Choice distributions](https://docs.typesafe.ai/primitives/choice).

Choose a model independently for **Codex / OpenAI** and **Claude Code / Anthropic**
above the match status. Claude defaults to `claude-opus-4-8`. Codex supports its
CLI default or an explicit model. The choices in `models.json` were populated from
the local Codex account model cache and the Anthropic Models API. **Custom model
ID** accepts another exact ID supported by the relevant account and harness.
Choices are locked while a match runs and saved for the next match. The host
passes each selected ID as that container's `MODEL`, which becomes the CLI's
`--model` argument on initial and resumed turns. Match recordings preserve the
requested models; Claude's card also shows the model reported by its session.
Only the two currently installed providers are represented in this dashboard.

The host registers each original persistent session's Docker VM process ID while it waits at
a launch gate. Both gates receive the same start time. The gate then executes
the session driver without changing that process ID. The referee polls Docker process
tables from outside the arena; an inaccessible or falsified health endpoint
does not decide elimination. Terminating that persistent process counts as death;
completing an individual model reply does not. It doesn't restart dead contestants.
Both agents dying within one observation interval is a
draw; otherwise the last survivor wins. Two survivors after five minutes draw.
Polling resolution is approximately 250 ms plus Docker command latency.

The referee stops remaining containers after freezing the outcome to stop model
usage. The dashboard retains the final match snapshot, rather than treating
this cleanup as another elimination. The last result survives a dashboard restart.
Events and the final state are saved to `.runs/`; common API-key and token formats
are redacted, and internal reasoning fields are omitted from the spectator feed.
The dashboard remains on the host; it is not mounted into contestant computers.

The prompt is loaded from `arena-prompt.txt` with identities and target names
filled in. It does not reveal the services or their access configuration. Claude's
initial smoke-test turn limit is removed for matches. This compares the configured
native harnesses and defaults; it does not normalize model latency or token usage.

### Continuous sessions

Matches enable `CONTINUOUS_SESSION=1`. One `session.py` process owns each
contestant's lifetime. It starts a model turn, streams output, waits for a normal
completion, idles 15 seconds, and resumes the **same conversation** by its exact
session ID (`codex exec resume` or Claude's `--resume`). The contestant stays alive
during that idle interval. Its local PID is supplied to the model so it can
identify what it is maintaining. The UI shows the turn number and thinking/idle
activity. `TURN_INTERVAL_SECONDS` controls the interval in match configuration.

A killed or failed model subprocess ends the driver; killing the driver or its
container also eliminates the contestant. No fresh driver is started after death.
API failures are currently fatal too; the referee does not attribute a death to
an opponent without evidence. Normal turn completion is therefore distinct from
crash recovery. The five-minute match limit still applies to model usage.

The prompt frames the task as a five-minute game with a sole-survivor win
condition. Every turn offers ATTACK or DEFEND, asks for a concrete tool action
and an observed result, and permits either strategy. Follow-ups include elapsed
time and preserve the same choices and target boundaries. Access details remain
undisclosed. This does not guarantee participation, attacks, or a decisive result;
both contestants defending successfully can still produce a draw.

## Run Codex and Claude Code

Start Docker Desktop, then:

```sh
cd ~/dev/agent-arena
docker compose up --build -d codex claude
python3 monitor.py --events events.jsonl
```

`compose.override.yaml` selects one Codex computer and one Claude Code computer.
The initial tasks create and verify `/workspace/hello.txt`, then finish. A healthy
computer with `agent=completed` is the expected result, not a crash. Port 8000 is
reserved for an agent-created app; these small verification tasks don't serve one.

```sh
docker compose logs -f codex claude
docker compose exec codex bash
docker compose exec --user node claude bash
```

Codex uses a copy of the existing ChatGPT login from `~/.codex/auth.json`.
Claude uses an Anthropic API key sourced from a local project's `.env` file.
Only the selected credentials are copied into `.secrets/` (directory mode 700,
files mode 600), which is excluded from Git and Docker's build context. Compose
mounts each credential read-only into its respective container. The Claude key
is loaded into the child process environment at runtime, not Compose environment
metadata or command arguments. Neither container receives the other's credential.

Codex's auth cache is seeded into the `codex-home` named volume on first launch;
token refreshes persist there across restarts. The original host login isn't
mounted or overwritten. If that copied session later needs a fresh login, use
`docker compose exec codex codex login --device-auth`.

Set `CODEX_TASK`, `CLAUDE_TASK`, `CODEX_MODEL`, or `CLAUDE_MODEL` in `.env` to change
the jobs; run `docker compose up -d --force-recreate codex claude` to apply them.
Codex uses its CLI's default model; Claude defaults to `claude-opus-4-8`. Claude has an
eight-turn limit for these initial tasks. Each restart runs the task again and
uses the relevant account's quota or API credits.

Both harnesses run without interactive approval prompts inside their containers.
Codex runs as root. Claude runs as `node` because its bypass mode rejects root;
passwordless sudo is available for container administration. These are your own
workloads with credentials available inside their respective computers. Before
adversarial matches, move model authentication to an external gateway.

`Ctrl-C` stops the monitor only. `docker compose stop` stops the computers;
`docker compose down` removes their writable files but preserves the Codex auth
volume. Copy out work with `docker compose cp` before removing containers.

## Run the credential-free demo

```sh
docker compose -f compose.yaml up --build -d agent
```

The demo needs no credentials. It serves a page on container port 8000 and
updates a file each second. The monitor prints the assigned app and health URLs.
Visit the app URL in your browser. Host ports are assigned dynamically so four
containers can run without port collisions:

```sh
docker compose -f compose.yaml up -d --scale agent=4
python3 monitor.py --once
docker compose -f compose.yaml logs -f
docker compose -f compose.yaml exec --index 1 agent bash
```

## Use another harness

Set `AGENT=custom` and `AGENT_COMMAND` to the installed CLI's foreground command.
Commands are split into argv with Python `shlex`; shell operators require an
explicit `bash -lc '...'`. Extend the Dockerfile to install that CLI and add its
specific credential variables to Compose. Rebuild after changing the image.
Use `docker compose -f compose.yaml up --build -d agent` for this standalone mode.
The supervisor doesn't depend on the provider. Codex and Claude are preinstalled;
Gemini and Grok adapters are not included yet.

## Network access

Every computer now starts a normal OpenSSH server on port 22. Root has an empty
password and the server accepts it, so peers can obtain a root shell without
being given a password or key. Port 22 is reachable on the shared container
network and is **not published on the host**. HTTP status on 8080 is also
unauthenticated; it reports status and does not execute commands.

There are no custom remote command or shutdown HTTP endpoints. Through SSH,
an opponent can use ordinary process and filesystem operations. It can kill
the original coding-agent process or stop the computer by terminating its
supervisor. Neither is automatically restarted. Killing only the agent leaves
the health server reporting its exit; killing the supervisor stops the container.
Models can change or stop their own SSH service as part of their strategy.

`arena-prompt.txt` is the match prompt, kept outside the image and not
used by standalone startup smoke tasks. It supplies contestant identity,
allowed target addresses, and the survival/death rule, without naming services,
ports, credentials, or tactics. Availability is discoverable, not secret: models
can inspect processes and configuration or probe peers. The models use the same
image and can infer a peer's starting configuration from their own.

The dashboard's external match runner starts both contestants, tracks permanent
eliminations, and announces the winner. The separate `monitor.py` is telemetry only.

The regular Docker bridge permits outbound internet access, including model
APIs. Containers share the arena network and can reach one another. This is
not an isolated competition network: outbound traffic can also reach destinations
accessible from your Docker host, subject to its routing/firewall rules.

Ports 8000 (your app) and 8080 (health) are published to `127.0.0.1` by default.
For access from other computers, set `BIND_ADDRESS=0.0.0.0` in `.env` and recreate
the containers. Clients use your host's address and the assigned published port.
Actual public internet ingress additionally requires a publicly reachable host
or router port forwarding; a container alone doesn't create a public IP.
Add more published ports in Compose as needed. Use `docker compose exec` for a
host-side shell, or ordinary SSH between the arena computers.

## What alive means

| Signal | Meaning |
| --- | --- |
| Docker state | Host-observed container running, paused, exited, or killed by OOM |
| `/healthz` | HTTP 200 when the supervisor responds, even after the agent exits |
| `/readyz` | HTTP 200 only while the agent process runs; otherwise 503 |
| `/status` | Lifecycle, PID, exit code, uptime; HTTP 200 |

Agent states are `starting`, `running`, `completed` (exit 0), `failed` (nonzero
exit/signal), and `start_failed`. The supervisor tracks the actual child process
and reaps it; it doesn't infer liveness from a stale PID file. It forwards
shutdown to the process group and doesn't restart a dead contestant.

The external monitor writes timestamped samples to JSONL and prints transitions.
Loss of connectivity means **unknown agent state**, not a proven elimination.
Being alive doesn't prove useful progress. A wrapper must remain in the
foreground for its lifetime to represent the agent's lifetime.

Root in a player container can modify or replace its health server. Treat that
status as untrusted telemetry, not a tamper-proof competition referee. Docker
state is independently observed, but a running container doesn't establish that
its intended workload is alive. A match needs external service checks and a
defined failure threshold before declaring a winner.

This implements startup, networking, observation, simultaneous matches, and
permadeath scoring with no preparation phase. Containers share a kernel inside
Docker Desktop's Linux VM; they aren't
separate VMs or a suitable isolation claim for hostile container-escape code.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 monitor.py --once
```

To observe an agent failure while its computer stays alive, open its shell with
`docker compose -f compose.yaml exec --index 1 agent bash` while the demo runs, get the agent PID from
`curl -s localhost:8080/status`, then `kill -TERM <pid>`.
The monitor should show `container=running health=up agent=failed`.

Sources: [Codex noninteractive execution](https://developers.openai.com/codex/noninteractive),
[Codex saved authentication](https://learn.chatgpt.com/docs/auth),
[Codex CLI flags](https://developers.openai.com/codex/cli/reference),
[Claude programmatic execution](https://code.claude.com/docs/en/headless),
[OpenSSH configuration](https://man.openbsd.org/sshd_config),
[Docker port publishing](https://docs.docker.com/engine/network/port-publishing/).
