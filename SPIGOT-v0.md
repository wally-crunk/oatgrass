# SPIGOT v0

SPIGOT v0 is OATGRASS's local SQLite coordination contract for Gazelle request pacing.
It is published so OATGRASS users and future developers can inspect the behavior, but it
is not a stable public ecosystem protocol for arbitrary peer tools.

API id: `local.oatgrass/spigot/v0`

## Scope

SPIGOT v0 coordinates Gazelle calls made by OATGRASS processes running as the same OS
user on one machine. It is not a network-filesystem, shared-home, or multi-machine
coordination protocol.

The SQLite database lives in a predictable per-user temporary directory as
`spigot-v0.sqlite3`. OATGRASS resolves the directory as follows:

- POSIX/macOS: `/tmp/oatgrass-spigot-<uid>/spigot-v0.sqlite3`
- Windows: `%TEMP%\oatgrass-spigot-<sanitized-user>\spigot-v0.sqlite3`

OATGRASS creates the containing directory with owner-only permissions where the
platform supports POSIX modes. Peer tools that implement this v0 contract should use the
same path resolver or honor `OATGRASS_SPIGOT_PATH` when it is set for tests and local
experiments.

SPIGOT v0 state is intentionally ephemeral. It only needs to coordinate active local
processes; request events, reservations, participants, and penalty boxes are expiry-based,
and the file does not need to survive uninstalling OATGRASS or long idle periods.

If the database cannot be opened, initialized, migrated, locked, or safely used,
OATGRASS fails closed before making Gazelle calls.

## Identity

Limiter state is scoped by tracker name, canonical server key, account identity, and
bucket name.

Before account verification, OATGRASS uses the bootstrap account identity `bootstrap`.
After an index response reveals username and UID, later requests use a deterministic
non-secret identity derived from tracker name, UID, and username.

When different machine-names from the same domain reveal the same account
credentials, for example `foo.bar.xyz` -> `johnny_user`, UID `12345`, and
`beta.bar.xyz` -> `johnny_user`, UID `12345`, they are treated as the same server for
pacing. Tailscale `.ts.net` names are the only special case in v0: they use the
tailnet domain, so `nova.jay-kitefin.ts.net` uses `jay-kitefin.ts.net`, not all
of `ts.net`.

Raw API keys are never stored in SPIGOT state.

## Tables

`metadata`

- `api_id`: must be `local.oatgrass/spigot/v0`
- `created_by`: creator app/version
- `created_monotonic`: creation timestamp used by the local process clock
- `created_at_utc`: human-readable UTC creation timestamp

`participants`

- `session_uuid`
- `app_version`
- `slow_declaration`
- `heartbeat_monotonic`
- `clean_exit_monotonic`

Participants heartbeat at least every 10 minutes. A participant is stale after 3 missed
heartbeat intervals. On normal exit, OATGRASS marks the session clean and removes its
active scope rows and unused reservations.

`participant_scopes`

- `session_uuid`
- tracker, server, and account scope fields
- last-seen timestamp

Participant scope rows let running sessions notice when other OATGRASS sessions start
or stop sharing a tracker/account pacing scope. They are presence metadata, not a
notice protocol.

`request_events`

- tracker, server, account, and bucket scope fields
- source action name
- raw request/window facts
- request start and expiry timestamps

Raw request facts are stored. `--slow N` is not pre-applied to events.

`reservations`

- `session_uuid`
- tracker, server, account, and bucket scope fields
- source action name and raw request/window facts
- reserved request timestamp and expiry timestamp

Reservations assign the next legal future request slot so concurrent sessions do not
all wake and race for the same opening. A session may hold at most one outstanding
reservation per tracker/server/account/bucket scope. If a process crashes, its unused
reservation remains only until its expiry timestamp and then self-clears.

`penalty_boxes`

- tracker, server, and account scope fields
- penalty expiry timestamp

Throttle responses, including HTTP 429 and HTTP 200 JSON failures containing
`Rate limit exceeded`, write shared penalty-box state. Later requests obey the more
conservative wait from proactive bucket pacing and penalty-box state.

## Buckets

Limiter state uses named buckets/scopes. For v0.6, OATGRASS enforces built-in tracker
default behavior and the OPS shared `ajax` bucket. OPS limited Ajax actions share that
bucket because inspected Gazelle source uses one user-wide cache key.

For OPS, built-in source-derived Ajax action rules are:

- `browse`: 5 requests / 10 seconds
- `collage`: 5 requests / 60 seconds
- `torrentgroup`: 15 requests / 60 seconds
- `user`: 4 requests / 60 seconds
- `usersearch`: 5 requests / 60 seconds
- `wiki`: 5 requests / 60 seconds

Unknown OPS actions use the tracker default behavior and are not treated as independent
safe buckets. RED keeps existing site-wide behavior; OATGRASS does not invent RED
endpoint-specific limits.

Pacing behavior is selected by named scheme id:

- OPS: `ops_pacing_scheme_2025`
- RED: `red_pacing_scheme_2025`

Scheme ids have a calendar year for general ordering reasons. If a tracker's actual
behavior changes, we update the scheme id, not just edit the existing one in place.

## Slow Mode

Each participant records its declared `--slow N`, or baseline `1` when unset. At pacing
decision time, OATGRASS uses the maximum active participant declaration and applies it
once. Stale participants stop contributing after heartbeat expiry.

## Transaction Rule

OATGRASS does not hold SQLite transactions during sleeps, application waits, or network
I/O. A transaction reads, computes, reserves, and commits quickly. If a wait is needed,
OATGRASS releases the transaction, sleeps in short chunks, and re-checks state before
starting the Gazelle request.

## Reserved Future Work

Structured notices, pause/shutdown actions, acknowledgement, takeover, and peer-tool
compatibility are reserved for later evaluation. OATGRASS v0.6 machine behavior does
not depend on notices.
