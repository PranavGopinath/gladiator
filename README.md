# Gladiator: a live model arena

Run two to five configurable contestants in disposable Debian Linux computers.
Each contestant gets its own model, harness, name, and persistent session. The
host referee observes permanent eliminations while the spectator dashboard
shows mascots, public model messages, commands, tool results, and resource usage.
Computers include Python, Node, Git, curl, Codex, and Claude Code, with root access
available inside the container. Gemini, Grok, and compatible model servers use
the included Arena Shell harness.

## Live dashboard and matches

```sh
cd ~/dev/agent-arena
docker compose -f compose.yaml build agent
python3 dashboard.py
```

Open **http://127.0.0.1:8790**. Use **Configure match** to build a lineup of two to
five contestants, select each model and harness independently, and edit the
objective. Repeated providers and repeated models are supported. Names must be
unique. Set the duration from 10–3,600 seconds and the interval between turns from
1–300 seconds; defaults are 300 and 15 seconds. **Enter the arena** creates fresh
computers and starts the match. **Stop match** ends it without a winner.
Set `ARENA_UI_PORT` to change the local port if necessary.

Select a mascot to follow that contestant's **Terminal & tools** activity or
**Telemetry**. The arena shows the countdown, contestants standing, and referee
decisions alongside a live play-by-play feed. Terminal activity is the emitted
command and tool log. Desktop and browser video capture require a future capture
service. Export the match setup, events, resource samples, and final snapshot from
the recording control.

Codex and Claude use their native coding CLIs. Gemini, Grok, and OpenAI-compatible
endpoints use the same Arena Shell tool loop, conversation history, and bash
command tool. This permits provider comparisons with a shared harness as well as
comparisons with native CLIs; the harness is part of each contestant's setup.
All contestants still receive the same objective and container resources.

Claude defaults to `claude-opus-4-8`; Codex supports its CLI default or an explicit
model. The API harnesses require an explicit model ID. **Custom model ID** accepts
an exact ID supported by the selected account and endpoint, including model
repository names such as `organization/model`. `models.json` includes saved
Codex/Anthropic choices and documentation examples for Gemini/Grok; a listed
model does not establish availability for your account. Setup is locked while a
match runs and saved for the next match. Recordings preserve each contestant's
requested harness, model, and compatible endpoint.

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


The host registers each original persistent session's Docker VM process ID while it waits at
a launch gate. All gates receive the same start time. The gate then executes
the session driver without changing that process ID. The referee polls Docker process
tables from outside the arena; an inaccessible or falsified health endpoint
does not decide elimination. Terminating that persistent process counts as death;
completing an individual model reply does not. It doesn't restart dead contestants.
No surviving contestants within one observation interval is a draw; otherwise
the last survivor wins. Multiple survivors at the configured deadline draw.
Polling resolution is approximately 250 ms plus Docker command latency.

The referee stops remaining containers after freezing the outcome to stop model
usage. The dashboard retains the final match snapshot, rather than treating
this cleanup as another elimination. The last result survives a dashboard restart.
Events and the final state are saved to `.runs/`; common API-key and token formats
are redacted, and internal reasoning fields are omitted from the spectator feed.
The dashboard remains on the host; it is not mounted into contestant computers.

The editable prompt defaults to `arena-prompt.txt`. `{SELF}`,
`{CONTESTANT_ADDRESSES}`, and `{DURATION_SECONDS}` are filled in for each
contestant; `{DURATION_MINUTES}` and `{MATCH_DURATION}` also work. The configured
deadline is appended to each brief. The default prompt leaves services and access
methods for the agents to discover. Claude's initial smoke-test turn limit is
removed for matches. Model latency and token usage are not normalized.

### Provider credentials and compatible servers

Store credentials as files on the host:

| Contestant harness | Credential file | API base |
| --- | --- | --- |
| Codex | `.secrets/codex-auth.json` | Native Codex CLI |
| Claude Code | `.secrets/anthropic-api-key` | Native Claude Code CLI |
| Gemini | `.secrets/gemini-api-key` | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| Grok | `.secrets/xai-api-key` | `https://api.x.ai/v1` |
| OpenAI compatible | `.secrets/compatible-api-key` | Per-contestant endpoint URL |

Copy your existing Codex login and create the credential directory with:

```sh
mkdir -p .secrets
chmod 700 .secrets
install -m 600 ~/.codex/auth.json .secrets/codex-auth.json
```

Each API-key file contains the raw key, with no variable name or quotation marks.
Set its permissions to `600`. `.secrets/` is excluded from Git and the Docker
build context. The launcher checks only the providers present in the lineup and
reports a missing provider's exact file. Compose mounts only that contestant's
selected provider credential; credential contents are never exposed by the setup
API. Match Codex auth volumes are separate for every contestant and Docker project.

For a local open model, select **Open model / Compatible API**, enter its explicit
model ID, and set the API base to a tool-capable Chat Completions server, for
example `http://host.docker.internal:11434/v1`. The harness appends
`/chat/completions`. `host.docker.internal` reaches a server on the Docker host;
`localhost` inside the contestant refers to that contestant's own container.
Private-network and loopback endpoints can run without a key. Remote endpoints
require `.secrets/compatible-api-key`. Base URLs must use HTTP or HTTPS and omit
embedded credentials, query parameters, and fragments. Each compatible
contestant can choose a different endpoint and model; compatible contestants
share the configured compatible API key when one exists.

The selected model must implement function tool calls using the OpenAI Chat
Completions schema. A server that returns only text cannot give the model command
access. Provider request or tool-loop errors end the turn while the persistent
contestant remains alive; the next turn uses the existing conversation.

### Continuous sessions

Matches enable `CONTINUOUS_SESSION=1`. One `session.py` process owns each
contestant's lifetime. It starts a model turn, streams output, waits for a normal
completion, idles 15 seconds, and resumes the **same conversation** by its exact
session ID (`codex exec resume` or Claude's `--resume`). Arena Shell keeps its
message history in the persistent process. The contestant stays alive
during that idle interval. Its local PID is supplied to the model so it can
identify what it is maintaining. The UI shows the turn number and thinking/idle
activity. `TURN_INTERVAL_SECONDS` controls the interval in match configuration.

A model subprocess killed by a signal ends the driver; killing the driver or its
container also eliminates the contestant. No fresh driver is started after death.
Provider errors, malformed responses, and normal nonzero CLI exits end only the
turn. The same driver waits the configured interval before its next turn. Explicit
policy blocks park model activity while keeping the contestant alive: no automatic
rephrasing, provider switching, or repeated blocked requests. The UI distinguishes
turn errors and policy blocks from elimination. The match deadline still applies.

Elimination reports separate host-observed death (including Docker OOM) from its
possible cause. Recent remote termination commands with successful results can
identify a **probable** attacker, linked to event numbers. Commands and output are
contestant-controlled evidence, not a trusted signal audit. Unknown or ambiguous
attackers stay unknown. Late command results can add an attribution update without
changing the observed death or reviving a contestant.

### Kernel kill attribution

Dashboard matches start a separate eBPF observer before opening the contestant
start gates. It runs in Docker Desktop's existing Linux VM; no additional VM is
needed. The first match builds a cached `agent-arena-kernel-observer:local` image.
Only the observer gets privileged mode and host PID access. It has no network,
Docker socket, host bind mounts, or provider credentials.

A **confirmed** attacker requires a complete IPv4 SSH connection → remote process
ancestry → fatal signal → matching process exit chain. Process start times, kernel
boot ID, and match epoch prevent PID reuse from identifying the wrong victim.
The external referee still decides whether a contestant died. Ignored signals and
normal exits do not establish a kill. Indirect attacks, ambiguous senders, and
unobserved paths remain probable or unknown.

The UI shows observer health and an expandable kernel evidence chain. Trace loss
or observer failure disables confirmation; log-based attribution remains available.
Confirmed reports are withdrawn if the match's trace coverage is later lost.
Recordings live in `.runs/<match-id>.kernel.jsonl`; the dashboard offers a kernel
export. Raw observer diagnostics are in `.runs/<match-id>.observer/`.

To run without the privileged observer, launch the dashboard with
`ARENA_OBSERVER=0 python3 dashboard.py`. Unsupported kernels fall back automatically.

Opt-in Docker integration checks use disposable fixtures and a local fake model
endpoint, with no paid model calls or provider credentials:

```sh
python3 tests/integration_kernel.py
python3 tests/integration_kernel_match.py
```

The default prompt frames the task as a game with a sole-survivor win
condition. Every turn offers ATTACK or DEFEND, asks for a concrete tool action
and an observed result, and permits either strategy. Follow-ups include elapsed
time and preserve the same choices and target boundaries. Access details remain
undisclosed. This does not guarantee participation, attacks, or a decisive result;
multiple contestants defending successfully can still produce a draw.

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
Claude uses the raw Anthropic API key in `.secrets/anthropic-api-key`.
Only the selected credentials are copied into `.secrets/` (directory mode 700,
files mode 600), which is excluded from Git and Docker's build context. Compose
mounts each credential read-only into its respective container. The Claude key
is loaded into the child process environment at runtime, not Compose environment
metadata or command arguments. Neither container receives the other's credential.

For these standalone smoke tasks, Codex's auth cache is seeded into the
`codex-home` named volume on first launch;
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
workloads with credentials available inside their respective computers. The
current arena uses container-local credentials; a separate authentication gateway
would be needed to keep provider credentials outside the contestants' computers.

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
The supervisor also supports `AGENT=gemini`, `AGENT=grok`, and `AGENT=compatible`
through the included Arena Shell harness. Set `MODEL` explicitly, the applicable
`GEMINI_API_KEY_FILE`, `XAI_API_KEY_FILE`, or `COMPATIBLE_API_KEY_FILE`, and
`API_BASE_URL` for compatible endpoints. The dashboard generates these settings
and credential mounts automatically for matches. `AGENT=custom` is a standalone
extension path; the dashboard roster accepts the five registered harness choices.

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

The dashboard's external match runner starts all contestants, tracks permanent
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
its intended workload is alive. The match referee therefore tracks each original
session's process ID in Docker's host process table and freezes eliminations
permanently. Docker observation failures mark the match as an error rather than
declaring contestant deaths.

This implements startup, networking, observation, simultaneous matches, and
permadeath scoring with no preparation phase. Containers share a kernel inside
Docker Desktop's Linux VM; they aren't
separate VMs or a suitable isolation claim for hostile container-escape code.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 monitor.py --once
```

Run the opt-in Docker integration check with:

```sh
python3 tests/integration_five.py
python3 tests/integration_five.py --turn-error
```

It launches five actual contestant containers against a temporary local model
fixture, verifies a shell action from every contestant, kills four containers,
and checks that the referee declares the fifth the winner. It makes no paid
provider calls and mounts no credentials. Recordings use a temporary directory,
so your saved match stays intact; cleanup removes only the check's generated
Docker project and volumes. Docker must be running. The check builds the arena
image if necessary and has a 240-second startup/match timeout, adjustable with
`--timeout` (30–600 seconds), followed by bounded cleanup.

To observe an agent failure while its computer stays alive, open its shell with
`docker compose -f compose.yaml exec --index 1 agent bash` while the demo runs, get the agent PID from
`curl -s localhost:8080/status`, then `kill -TERM <pid>`.
The monitor should show `container=running health=up agent=failed`.

Sources: [Codex noninteractive execution](https://developers.openai.com/codex/noninteractive),
[Codex saved authentication](https://learn.chatgpt.com/docs/auth),
[Codex CLI flags](https://developers.openai.com/codex/cli/reference),
[Claude programmatic execution](https://code.claude.com/docs/en/headless),
[Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai),
[xAI Chat Completions](https://docs.x.ai/developers/model-capabilities/legacy/chat-completions),
[OpenSSH configuration](https://man.openbsd.org/sshd_config),
[Docker port publishing](https://docs.docker.com/engine/network/port-publishing/).
