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
./fumes.py -a work          # one account (repeatable)
./fumes.py --no-history     # skip the snapshot append
```

## Accounts

Every provider can be configured more than once — a work and a personal Claude
Code login, two OpenCode data dirs. Copy `settings.example.json` to
`settings.json` beside the script and list them:

```json
{
  "accounts": [
    { "name": "claude",      "provider": "claude",   "folder": "~/.claude",                "binary": "claude" },
    { "name": "claude-work", "provider": "claude",   "folder": "~/.claude-work",           "binary": "claude" },
    { "name": "opencode",    "provider": "opencode", "folder": "~/.local/share/opencode",  "binary": "opencode" }
  ]
}
```

| Field | Meaning |
|---|---|
| `name` | What the table calls it and what `-a` selects. Must be unique — it also keys the calibration. |
| `provider` | `claude` or `opencode`. The adapter that knows how to read the folder. |
| `folder` | Where that provider keeps its state — Claude Code's config dir (holding `.credentials.json`), OpenCode's data dir (holding `auth.json` and `opencode*.db`). `~` and `$VARS` expand. |
| `binary` | The CLI that owns the folder. **Never executed** — it appears in hints, e.g. which command to run to refresh an expired token. |

Only `provider` is required; the rest fall back to that provider's defaults.
Everything an account needs to be told apart lives in its folder, so a second
login is a settings entry, not a code change.

Accounts are read from `$FUMES_SETTINGS`, else `settings.json` beside the
script, else `~/.config/fumes/settings.json`. **With no settings file at all,
one account per provider is assumed at the usual locations** — which is exactly
what earlier versions did, so nothing needs configuring to keep working.

Each account is fetched independently: one that can't be read prints its error
under its own heading and the rest still print.

```
$ ./fumes.py
claude-work (claude)
  5-hour session  ████████████████  100%         resets in 2h 45m
  7-day           █████████░░░░░░░   57%         resets in 1d 1h
claude-old (claude)
  OAuth token expired at 22:16 - run `CLAUDE_CONFIG_DIR=/home/you/.claude-old claude` to refresh
opencode (opencode)
  go 5-hour       ██████░░░░░░░░░░   35%  $0.96  resets in 4h 4m    cal today
```

The heading is the account name; the provider follows in parentheses unless the
name already is the provider.

## Providers

| Provider | Source | Reads |
|---|---|---|
| `claude` | **live** | `GET api.anthropic.com/api/oauth/usage` with the token in `<folder>/.credentials.json` (default `$CLAUDE_CONFIG_DIR`, else `~/.claude`) |
| `opencode` | **local** | `<folder>/opencode*.db` (default `$OPENCODE_DATA_DIR`, else `$XDG_DATA_HOME/opencode`, else `~/.local/share/opencode`) |

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
./fumes.py calibrate --show          # what's stored, for every account
./fumes.py calibrate --clear         # back to assumed caps
./fumes.py calibrate -a work ...     # which account you're reading off the console
```

Calibration is **per account**: two OpenCode logins are metered separately and
each console shows its own percentages, so each gets its own caps and window
anchors. `-a` is optional while only one OpenCode account is configured, and
required once there are several. `--show` without `-a` prints them all.

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

Three local files, all gitignored:

- `history.jsonl` — one snapshot per report run. Kept because the Claude
  percentages exist nowhere else once a window rolls.
- `calibration.json` — the fitted caps and window anchors, keyed by account. See
  `calibration.example.json`.
- `settings.json` — your account list. Paths and login names, no secrets, but
  personal. See `settings.example.json`.

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
  `Record`s plus an entry in `PROVIDERS` and `PROVIDER_DEFAULTS`; adding another
  *account* of an existing provider is settings only.
- The default Claude account now follows `$CLAUDE_CONFIG_DIR` when it is set,
  where it used to always read `~/.claude`. If your shell exports it, that's the
  account you'll see — name both folders in `settings.json` to see both.

## License

MIT
