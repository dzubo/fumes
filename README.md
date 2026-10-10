# fumes

*How close are you to running on fumes?* One table of your remaining AI provider
limits, from a plain shell — no browser, no agent session.

```
$ ./fumes.py
claude
  5-hour session  ██████████░░░░░░  60%         resets in 3h 42m
  7-day           █████░░░░░░░░░░░  34%         resets in 1d 2h
opencode
  go 5-hour       █████░░░░░░░░░░░  23%         resets in 1h 32m
  go week         ████████░░░░░░░░  46%         resets in 5d 19h
  go month        ███████████████░  89%         resets in 11d 21h
  zen month       ----------------       $0.26  resets in 9d 20h   uncapped
```

Every row is the provider's own number: a percentage the server computed, or a
charge it levied. The only marker is `uncapped`, for pay-as-you-go spend that
no limit governs.

## Install

Single file, Python 3.12+, one dependency. The repo carries a `pyproject.toml`,
a `uv.lock` and a `.python-version`, so [uv](https://docs.astral.sh/uv/)
reproduces the environment exactly:

```bash
git clone git@github.com:dzubo/fumes.git && cd fumes
uv run ./fumes.py
```

`uv run` builds `.venv` from the lockfile on first use and re-checks it on every
run, so there is no activate step and no `pip install`. It also pins *which*
environment you get: a virtualenv that happens to be active elsewhere in your
shell is reported and ignored rather than silently used. The sync check costs
~20-30ms over calling the venv's Python directly, which is cheap enough for a
statusline or a cron entry.

Without uv, it is still one file and one dependency:

```bash
pip install httpx
./fumes.py
```

```
./fumes.py                  # table
./fumes.py --json           # normalized records, for a statusline or cron
./fumes.py -p claude        # one provider (repeatable)
./fumes.py -a work          # one account (repeatable)
./fumes.py --no-history     # skip the snapshot append
./fumes.py --version        # also stamped into every history.jsonl line
```

### A shortcut worth adding

Examples throughout this README are written `./fumes.py`; read them as
`uv run ./fumes.py` under uv. Having to stand in the repo is a nuisance for
something you check between other tasks, so give yourself a `fumes` that works
from anywhere — a function in `~/.bashrc`, pointed at your clone:

```bash
# fumes - AI provider limits. --project pins the env to the repo's uv.lock, so
# it works from any directory and ignores whatever venv is active.
fumes() { uv run --project ~/projects/fumes ~/projects/fumes/fumes.py "$@"; }
```

`source ~/.bashrc` to pick it up in shells that are already open. Arguments pass
straight through, so every invocation below works as `fumes --json`,
`fumes -p claude`, and so on. This keeps `uv.lock` as the single source of truth
for what gets installed, which the two alternatives give up:

- **PEP 723 inline metadata.** Change the shebang to
  `#!/usr/bin/env -S uv run --script` and declare `httpx` in a `# /// script`
  block; then `./fumes.py` runs from any directory, including through a symlink
  on your `PATH`. The cost is a second place declaring the dependency, since
  `--script` builds an isolated environment from the inline block and ignores
  `uv.lock`.
- **`uv tool install --editable .`** with a `[project.scripts]` entry point puts
  a real `fumes` on `PATH`. Keep `--editable`: without it uv copies the script
  into the tool's own venv, and because `history.jsonl` and `calibration.json`
  are written next to the script, your snapshots and fitted caps would start
  landing there instead of in the clone.

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
| `name` | What the table calls it and what `-a` selects. Must be unique, and must match `[A-Za-z0-9][A-Za-z0-9_-]*` — see below. |
| `provider` | `claude` or `opencode`. The adapter that knows how to read the folder. |
| `folder` | Where that provider keeps its state — Claude Code's config dir (holding `.credentials.json`), OpenCode's data dir (holding `auth.json`). `~` and `$VARS` expand. |
| `binary` | The CLI that owns the folder. **Never executed** — it appears in hints, e.g. which command to run to refresh an expired token. |
| `service_key` | OpenCode only: a **service account key** from [console.opencode.ai](https://console.opencode.ai) (`oc_sk_...`). Unlocks the Zen spend row. Go-plan rows never need it. |
| `console_url` | OpenCode only: where the usage export lives. Defaults to `https://opencode.ai/console`. |

Only `provider` is required; the rest fall back to defaults. An OpenCode account
needs no key of its own for the Go rows — fumes reuses the API key opencode
keeps in `auth.json` — but a `service_key` is what unlocks the Zen row, because
the console's usage export accepts service account keys only. Without one that
row is simply absent, not an error.

The name is an identifier, not a label. Tools that read this one's output put it
into dotted key paths (`data.by_account.claude.records.0.pct`) and into the
regexes that match them — so a dot would split the path and a space or a `(`
would break the match. Names are checked when the settings file loads and
anything outside `[A-Za-z0-9][A-Za-z0-9_-]*` is rejected with the offending
entry named:

```
error: /home/you/.config/fumes/settings.json accounts[1]: account name
'work.claude' is not valid - start with a letter or digit and use only letters,
digits, '-' and '_' (the name is used as a key by this tool and others)
```

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
  go 5-hour       ██████░░░░░░░░░░   35%         resets in 4h 4m
  zen month       ----------------       $0.96  resets in 12d 3h   uncapped
```

The heading is the account name; the provider follows in parentheses unless the
name already is the provider.

## Providers

| Provider | Source | Reads |
|---|---|---|
| `claude` | **live** | `GET api.anthropic.com/api/oauth/usage` with the token in `<folder>/.credentials.json` (default `$CLAUDE_CONFIG_DIR`, else `~/.claude`) |
| `opencode` | **live** | `GET opencode.ai/zen/go/v1/usage` with the Go API key in `<folder>/auth.json` (default `$OPENCODE_DATA_DIR`, else `$XDG_DATA_HOME/opencode`, else `~/.local/share/opencode`); plus, with a `service_key`, the console's `GET /api/v2/usage/export` CSV for Zen spend |

**Claude** reuses the OAuth token Claude Code already maintains. That file is read
**read-only** on purpose: Claude Code owns it and refreshes the ~3h token on use,
so refreshing here would race a running agent. An expired token is reported, not
repaired — run any Claude Code command and try again.

**OpenCode** reads two things, both live:

- **Go plan** — `GET /zen/go/v1/usage` returns the console's own counters: the
  rolling 5-hour, weekly and monthly percentages with their reset instants. It
  accepts the regular Go API key opencode maintains in `auth.json`, so there is
  nothing to configure and nothing to calibrate — the server computed the
  percentage, so the client shows it. A workspace without a Go subscription
  gets a 403 and simply no Go rows.
- **Zen pay-as-you-go** — the console's v2
  [usage export](https://opencode.ai/console/guides/usage) (`/api/v2/usage/export`,
  `range=30d`) streams daily rollups as CSV: one row per UTC day, member or
  service account, provider and model. It accepts **service account keys only** —
  create one in the console, put it on the account as `service_key`. Rows whose
  provider is `opencode` are charged to the balance and roll up into the calendar
  month; Go-plan rows (`provider=opencode-go`) carry no dollars (the plan meters
  its own dollar-equivalents the CSV never exposes), and free models carry zero
  cost, so neither can inflate the spend. The v1 org-wide export
  (`/api/v1/usage/export`) was deprecated in September 2026 — organizations
  moved to v2 get a bare 403 from it.

Earlier versions rolled OpenCode spend up from its local SQLite `message` table
and fitted caps to console readings because no usage API existed. One does now,
so that machinery — `calibrate`, `calibration.json`, assumed caps, carried
offsets — is gone. Your `calibration.json`, if you have one, is no longer read;
delete it whenever you like.

## Data and privacy

Nothing leaves your machine except requests to the issuers of the credentials it
sends: `api.anthropic.com` for Claude, `opencode.ai` for OpenCode (one or two
requests per run — the usage endpoint, and the export CSV when a `service_key`
is configured). No telemetry, no third parties.

Two local files, both gitignored:

- `history.jsonl` — one snapshot per report run. Kept because the percentages
  exist nowhere else once a window rolls.
- `settings.json` — your account list. Paths and login names, but personal —
  and a `service_key` is a credential, so keep the file out of anything you
  share or sync. See `settings.example.json`.

Credentials are read at call time, used in an `Authorization` header, and never
written to either file or to any error message.

## Caveats

- `api.anthropic.com/api/oauth/usage` is **undocumented**. It works today; it can
  change or disappear without notice.
- Same for `opencode.ai/zen/go/v1/usage` — the percentages are the console's own
  counters, but the endpoint isn't in the docs. The usage export, by contrast,
  is documented and versioned (`/api/v1/usage/export`).
- The export's ranges start at midnight UTC, and `30d` is the widest — which
  still covers the whole current calendar month, so the Zen row never clips. The
  Go windows need nothing from the CSV at all. The v2 export reports daily
  rollups, which can lag the most recent requests by a little; it answers 503
  until the day's rollup exists.
- The Go percentages arrive floored to whole percents; a bar can read one point
  lower than the console if you check them between requests.
- Only Claude and OpenCode so far. Adding a provider means one function returning
  `Record`s plus an entry in `PROVIDERS` and `PROVIDER_DEFAULTS`; adding another
  *account* of an existing provider is settings only.
- The default Claude account now follows `$CLAUDE_CONFIG_DIR` when it is set,
  where it used to always read `~/.claude`. If your shell exports it, that's the
  account you'll see — name both folders in `settings.json` to see both.
- v0.6 changed the `--json` shape: records lost `calibrated` and `carried`, and
  OpenCode Go rows are percent-native (`unit: "percent"`, `limit: 100`) instead
  of dollar-native. The version is stamped into every `history.jsonl` line.
- September 2026: OpenCode moved usage exports to `/api/v2/usage/export` and
  began 403-ing the v1 org-wide endpoint. v0.6.1 follows the v2 CSV — the Zen
  row now sums `provider=opencode` rows from the daily rollups instead of
  `billing_source=credit` rows from the deprecated export.

## License

MIT
