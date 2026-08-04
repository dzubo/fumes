# fumes

*How close are you to running on fumes?* One table of your remaining AI provider
limits, from a plain shell — no browser, no agent session.

```
$ ./fumes.py
claude
  5-hour session  ██████████░░░░░░  60%         resets in 3h 42m
  7-day           █████░░░░░░░░░░░  34%         resets in 1d 2h
opencode
  go 5-hour       ███████░░░░░░░░░  42%  $1.15  resets in 4h 32m   cal today
  go week         ██████████████░░  87%  $6.79  resets in 5d 20h   cal today
  go month        █░░░░░░░░░░░░░░░   6%  $1.15  resets in 30d 22h  cal today
  zen month       ----------------       $0.13  resets in 27d 21h  uncapped
```

The last column is how much to trust the row: nothing for Claude (the server said
so), `cal <age>` for a calibrated OpenCode window, `est` for one still on assumed
caps, `uncapped` for pay-as-you-go.

## Install

Single file, Python 3.10+, one dependency:

```bash
pip install httpx
git clone git@github.com:dzubo/fumes.git && cd fumes
./fumes.py
```

```
./fumes.py                  # table
./fumes.py --json           # normalized records, for a statusline or cron
./fumes.py -p claude        # one provider (repeatable)
./fumes.py --no-history     # skip the snapshot append
```

## Providers

| Provider | Source | Reads |
|---|---|---|
| `claude` | **live** | `GET api.anthropic.com/api/oauth/usage` with the token in `~/.claude/.credentials.json` |
| `opencode` | **local** | `~/.local/share/opencode/opencode*.db` (respects `$OPENCODE_DATA_DIR` / `$XDG_DATA_HOME`) |

**Claude** reuses the OAuth token Claude Code already maintains. That file is read
**read-only** on purpose: Claude Code owns it and refreshes the ~3h token on use,
so refreshing here would race a running agent. An expired token is reported, not
repaired — run any Claude Code command and try again.

**OpenCode** has no usage API. Verified against a live account: `/usage`,
`/account`, and `/billing` all 404 on `opencode.ai`, `api.opencode.ai`, and
`console.opencode.ai`; a real completion against `zen/go/v1` comes back with no
`x-ratelimit-*` headers; the Zen docs list only `/models`, `/responses`,
`/messages`, `/chat/completions`. So spend is rolled up from opencode's own
`message` table (`providerID`, `cost`, `time_created`) — the approach
[openusage](https://github.com/robinebers/openusage) documents. The database is
opened `mode=ro` with `PRAGMA query_only = 1`.

## Calibration

The local rollup and the OpenCode console disagree — badly. Measured against
console readings of 42% / 87% / 6%:

| Window | Community-quoted cap | Effective cap | Off by |
|---|---|---|---|
| Rolling 5h | $12.00 | $2.74 | 4.4× |
| Weekly | $30.00 | $7.80 | 3.8× |
| Monthly | $60.00 | $19.15 | 3.1× |

Two structural reasons: the Go plan isn't metered at opencode's local cost rates,
and usage from any *other* machine never reaches this database. The monthly
*window* was wrong too — it's billing-anchored (resets on a fixed day of the
month), not calendar.

So don't assume the caps, measure them. Read the percentages off the console and
hand them over:

```bash
./fumes.py calibrate --rolling 42 --weekly 87 --monthly 6 \
    --weekly-resets "5d 21h" --monthly-resets "30d 23h"
./fumes.py calibrate --show     # what's stored
./fumes.py calibrate --clear    # back to assumed caps
```

Each percentage is divided into the local spend for that window to get the
**effective cap** — the local-dollar figure that reproduces the console's number.
The optional countdowns move the window boundaries, and are applied *first*: a cap
fitted over the wrong window is meaningless. Results land in `calibration.json`.

Guard rails, because this is a fit to a single observation:

- A window holding less than **$0.25** of local spend is skipped — dividing a
  percentage into near-zero spend gives a garbage cap.
- A reading below **10%** prints the range it actually implies (6% means 5.5–6.5%,
  so ±8% on the cap) and suggests recalibrating later in the window.
- Console countdowns are rounded, so derived anchors carry ±1h.
- Every row shows its calibration age; after 14 days it says `stale`.

**It drifts.** Recalibrate whenever the table and the console disagree.

## Data and privacy

Nothing leaves your machine except one request to `api.anthropic.com` — the
issuer of the token it sends. No telemetry, no third parties.

Two local files, both gitignored:

- `history.jsonl` — one snapshot per report run. Kept because the Claude
  percentages exist nowhere else once a window rolls.
- `calibration.json` — the fitted caps and window anchors. See
  `calibration.example.json`.

Credentials are read at call time, used in an `Authorization` header, and never
written to either file or to any error message.

## Caveats

- `api.anthropic.com/api/oauth/usage` is **undocumented**. It works today; it can
  change or disappear without notice.
- The OpenCode rollup depends on their SQLite schema, which is likewise not a
  public contract. It reads `opencode.db` and `opencode-next.db`.
- The Go plan caps in `DEFAULT_CAPS` are community-observed starting points and
  are — as the table above shows — wrong. Calibrate.
- Only Claude and OpenCode so far. Adding a provider means one function returning
  `Record`s and one entry in `PROVIDERS`.

## License

MIT
