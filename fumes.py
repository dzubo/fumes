#!/usr/bin/env python3
"""
fumes - how much is left before you are running on fumes? One view of AI provider limits and spend.

Providers:
    claude    live  - Claude Code's OAuth token against api.anthropic.com/api/oauth/usage.
                      Authoritative: these are the server's own numbers.
    opencode  local - rolled up from opencode's own SQLite accounting
                      (~/.local/share/opencode/opencode*.db). OpenCode exposes no
                      usage API, so windows and caps are applied client-side and
                      are only as good as the last calibration (see below).

Accounts:
    Each provider can be configured any number of times - a work and a personal
    Claude Code login, two OpenCode data dirs - by listing them in settings.json
    beside this file. An account is a name, a provider, and the folder that
    provider keeps its state in, so two accounts never read each other's numbers.
    See settings.example.json. Without a settings.json, one account per provider
    is assumed at the usual locations, which is what earlier versions did.

Usage:
    ./fumes.py                  # table
    ./fumes.py --json           # normalized records
    ./fumes.py -p claude        # one provider (repeatable)
    ./fumes.py -a work          # one account (repeatable)
    ./fumes.py --no-history     # don't append a snapshot
    ./fumes.py --version        # also stamped into every history.jsonl line

    # teach it the real OpenCode Go numbers, read off console.opencode.ai
    ./fumes.py calibrate -a opencode --rolling 42 --weekly 87 --monthly 6 \
        --weekly-resets "5d 21h" --monthly-resets "30d 23h"
    ./fumes.py calibrate --show          # every account
    ./fumes.py calibrate -a opencode --clear

Every report run appends a snapshot to history.jsonl beside this file (gitignored)
so burn-rate and trends are recoverable later. Calibration is stored per account
in calibration.json, because two accounts on the same plan still have their own
caps.

Dependencies:
    pip install httpx
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from calendar import monthrange
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
HISTORY_FILE = HERE / "history.jsonl"
CALIBRATION_FILE = HERE / "calibration.json"
SETTINGS_NAME = "settings.json"
SETTINGS_ENV = "FUMES_SETTINGS"
TIMEOUT = 15.0

# Stamped into every history.jsonl snapshot: the file has already changed shape
# once, so a reader shouldn't have to sniff which version wrote a given line.
VERSION = "0.2.0"

CLAUDE_CREDENTIALS_NAME = ".credentials.json"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BETA = "oauth-2025-04-20"

# OpenCode Go fallbacks, used until `calibrate` replaces them. Unofficial:
# OpenCode publishes no usage API and no documented limits, so these are the
# community-observed figures (openusage's docs/providers/opencode.md) applied to
# opencode's own local cost accounting. Expect them to be wrong - the console's
# percentages are the only ground truth, hence `calibrate`.
GO_SESSION_HOURS = 5
DEFAULT_CAPS = {"session": 12.0, "week": 30.0, "month": 60.0}

# Dividing a percentage into near-zero spend produces a garbage cap, so a window
# needs at least this much local spend before it can be calibrated.
MIN_CALIBRATION_SPEND = 0.25
CALIBRATION_STALE_DAYS = 14

WINDOW_FLAGS = {"session": "rolling", "week": "weekly", "month": "monthly"}


class ProviderError(Exception):
    """An account could not be read. Never fatal - the other accounts still print."""


class ConfigError(Exception):
    """settings.json is unusable. Fatal: guessing at a broken config is worse."""


@dataclass(frozen=True)
class Account:
    """One login of one provider. `folder` is where that provider keeps its state."""

    name: str  # what the table and -a call it; unique across the config
    provider: str
    folder: Path
    binary: str  # only ever named in hints, never executed


@dataclass
class Record:
    account: str
    provider: str
    window: str  # stable key: 5h | 7d | session | week | month
    label: str  # human label for the table
    used: float
    limit: float | None  # None means uncapped
    unit: str  # "percent" | "usd"
    pct: float | None
    resets_at: str | None  # ISO 8601
    source: str  # "live" | "local"
    calibrated: bool = False  # local windows only: is the cap measured or assumed?
    note: str | None = None


# --------------------------------------------------------------------------- #
# claude
# --------------------------------------------------------------------------- #


def claude_config_dir() -> Path:
    """Where Claude Code keeps credentials when no account overrides it."""
    if env := os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(env)
    return Path.home() / ".claude"


def _refresh_hint(account: Account) -> str:
    """The command that re-mints this account's token. Printed, never run."""
    if account.folder == claude_config_dir():
        return account.binary
    return f"CLAUDE_CONFIG_DIR={account.folder} {account.binary}"


def fetch_claude(account: Account, _calibration: dict) -> list[Record]:
    """Read the OAuth token Claude Code already maintains, then ask the server.

    Nothing to calibrate here - the server hands over its own percentages.
    """
    credentials = account.folder / CLAUDE_CREDENTIALS_NAME
    try:
        creds = json.loads(credentials.read_text())["claudeAiOauth"]
    except FileNotFoundError:
        raise ProviderError(f"no credentials at {credentials} - is Claude Code set up there?")
    except (KeyError, json.JSONDecodeError) as exc:
        raise ProviderError(f"unreadable credentials: {exc}")

    # Deliberately read-only: Claude Code owns this file and refreshes the token
    # itself. Refreshing here would race it, so an expired token is just reported.
    expires_at = creds.get("expiresAt")
    if expires_at and expires_at / 1000 <= datetime.now(timezone.utc).timestamp():
        when = datetime.fromtimestamp(expires_at / 1000).strftime("%H:%M")
        raise ProviderError(
            f"OAuth token expired at {when} - run `{_refresh_hint(account)}` to refresh"
        )

    headers = {
        "Authorization": f"Bearer {creds['accessToken']}",
        "anthropic-beta": CLAUDE_BETA,
    }
    try:
        response = httpx.get(CLAUDE_USAGE_URL, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        raise ProviderError(f"HTTP {exc.response.status_code} from {CLAUDE_USAGE_URL}")
    except httpx.HTTPError as exc:
        raise ProviderError(f"request failed: {exc}")

    plan = creds.get("subscriptionType")
    records = []
    for key, window, label in (("five_hour", "5h", "5-hour session"), ("seven_day", "7d", "7-day")):
        block = data.get(key)
        if not block:
            continue
        used = float(block.get("utilization", 0.0))
        records.append(
            Record(
                account=account.name,
                provider="claude",
                window=window,
                label=label,
                used=used,
                limit=100.0,
                unit="percent",
                pct=used,
                resets_at=block.get("resets_at"),
                source="live",
                note=plan,
            )
        )

    # Extra usage (pay-per-use past the plan limits) only matters when it's armed.
    extra = data.get("extra_usage") or {}
    spend = data.get("spend") or {}
    if extra.get("is_enabled") and spend.get("used"):
        used_money = _minor(spend["used"])
        cap = _minor(spend.get("limit")) if spend.get("limit") else None
        records.append(
            Record(
                account=account.name,
                provider="claude",
                window="extra",
                label="extra usage",
                used=used_money,
                limit=cap,
                unit="usd",
                pct=(used_money / cap * 100 if cap else None),
                resets_at=None,
                source="live",
                note=spend["used"].get("currency"),
            )
        )
    if not records:
        raise ProviderError("usage endpoint returned no windows")
    return records


def _minor(money: dict) -> float:
    """{amount_minor: 692, exponent: 2} -> 6.92"""
    return money["amount_minor"] / (10 ** money.get("exponent", 2))


# --------------------------------------------------------------------------- #
# opencode - calibration
# --------------------------------------------------------------------------- #
#
# OpenCode meters the Go plan server-side and shows only percentages, on a page
# no API backs. Locally all we have is opencode's own per-message `cost`. Those
# two disagree - by roughly 4x when first measured - because the plan is not
# metered at the local cost rates, and because usage from other machines never
# reaches this database at all.
#
# So: read the console's percentages, divide the local spend by each, and keep
# the resulting *effective cap* - the local-dollar figure that reproduces the
# console's percentage. Same trick for the window boundaries, which the console
# gives away through its countdowns (the monthly window turned out to be
# billing-anchored, not calendar).
#
# This is a fit to one observation, not a discovered constant. It drifts.
# Recalibrate when the console and the table disagree; every record says how old
# its calibration is.
#
# The fit is per account: two OpenCode logins are metered separately, and each
# one's console shows its own percentages. So calibration.json is keyed by
# account name.

CALIBRATION_VERSION = 2


def load_calibration(accounts: list[Account] | None = None) -> dict[str, dict]:
    """{account name: calibration block}, migrating the old single-account file."""
    try:
        data = json.loads(CALIBRATION_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data.get("accounts"), dict):
        return data["accounts"]
    if "windows" in data:
        # Written before accounts existed, so it describes whichever OpenCode
        # account came first - back then there could only be the one.
        owner = next((a.name for a in accounts or [] if a.provider == "opencode"), "opencode")
        return {owner: data}
    return {}


def save_calibration(calibrations: dict[str, dict]) -> None:
    if not calibrations:
        CALIBRATION_FILE.unlink(missing_ok=True)
        return
    payload = {"version": CALIBRATION_VERSION, "accounts": calibrations}
    CALIBRATION_FILE.write_text(json.dumps(payload, indent=2) + "\n")


# The unit must not run into another letter ('30d23h' is two tokens, '30 dogs' is
# none), but it may run straight into the next digit.
_DURATION_TOKEN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m)(?![a-z])", re.I
)


def parse_duration(text: str) -> timedelta:
    """'5d 21h' / '5 days 21 hours' / '4 hours 37 minutes' -> timedelta."""
    matches = _DURATION_TOKEN.findall(text)
    if not matches:
        raise ValueError(f"cannot read a duration from {text!r} - try '5d 21h'")
    total = timedelta()
    for amount, unit in matches:
        unit = unit.lower()
        value = float(amount)
        if unit.startswith("d"):
            total += timedelta(days=value)
        elif unit.startswith(("h", "hr")):
            total += timedelta(hours=value)
        else:
            total += timedelta(minutes=value)
    return total


def window_bounds(now: datetime, calibration: dict) -> dict[str, tuple[datetime, datetime | None]]:
    """(start, reset) per window. A rolling window has no reset instant - see below."""
    windows = calibration.get("windows", {})
    hours = windows.get("session", {}).get("hours", GO_SESSION_HOURS)
    return {
        "session": (now - timedelta(hours=hours), None),
        "week": _weekly_bounds(now, windows.get("week", {}).get("reset_at")),
        "month": _monthly_bounds(now, windows.get("month", {})),
        # Zen is billed separately from the Go plan, so calibrating the Go
        # monthly anchor must not drag Zen's month off the calendar.
        "calendar_month": _monthly_bounds(now, {}),
    }


def _weekly_bounds(now: datetime, reset_at: str | None) -> tuple[datetime, datetime]:
    """Step a known reset instant forward in 7-day hops until it lands after now."""
    period = timedelta(days=7)
    if reset_at:
        anchor = datetime.fromisoformat(reset_at)
    else:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        anchor = midnight - timedelta(days=midnight.weekday()) + period  # next UTC Monday
    reset = anchor + period * math.ceil((now - anchor) / period)
    if reset <= now:
        reset += period
    return reset - period, reset


def _monthly_bounds(now: datetime, config: dict) -> tuple[datetime, datetime]:
    """Calendar-stepped window anchored on a day-of-month (default: the 1st)."""
    day = config.get("anchor_day", 1)
    hour, minute = (int(part) for part in config.get("anchor_time", "00:00").split(":"))

    def occurrence(year: int, month: int) -> datetime:
        # Anchored on the 31st? Short months clamp to their last day.
        clamped = min(day, monthrange(year, month)[1])
        return datetime(year, month, clamped, hour, minute, tzinfo=timezone.utc)

    def step(moment: datetime, months: int) -> datetime:
        index = moment.year * 12 + (moment.month - 1) + months
        return occurrence(index // 12, index % 12 + 1)

    current = occurrence(now.year, now.month)
    if current <= now:
        return current, step(current, 1)
    return step(current, -1), current


# --------------------------------------------------------------------------- #
# opencode - reading
# --------------------------------------------------------------------------- #


def opencode_data_dir() -> Path:
    """Where OpenCode keeps its databases when no account overrides it."""
    if env := os.environ.get("OPENCODE_DATA_DIR"):
        return Path(env)
    if xdg := os.environ.get("XDG_DATA_HOME"):
        return Path(xdg) / "opencode"
    return Path.home() / ".local" / "share" / "opencode"


def _opencode_dbs(data_dir: Path) -> list[Path]:
    # OpenCode partitions its database by release channel.
    dbs = [p for p in (data_dir / "opencode.db", data_dir / "opencode-next.db") if p.exists()]
    if not dbs:
        raise ProviderError(f"no opencode database under {data_dir}")
    return dbs


def _opencode_auth(data_dir: Path) -> dict:
    try:
        return json.loads((data_dir / "auth.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


@dataclass
class Spend:
    """Local spend per provider per window, plus the oldest message in each."""

    totals: dict[str, dict[str, float]] = field(default_factory=dict)
    oldest: dict[str, float] = field(default_factory=dict)

    def get(self, provider: str, window: str) -> float:
        return self.totals.get(provider, {}).get(window, 0.0)


def read_spend(dbs: list[Path], bounds: dict[str, tuple[datetime, datetime | None]]) -> Spend:
    spend = Spend(totals={"opencode-go": {}, "opencode": {}})
    floor_ms = int(min(start for start, _ in bounds.values()).timestamp() * 1000)
    for db in dbs:
        for created_ms, blob in _read_messages(db, floor_ms):
            try:
                message = json.loads(blob)
            except json.JSONDecodeError:
                continue
            provider = message.get("providerID")
            if provider not in spend.totals:
                continue
            cost = float(message.get("cost") or 0.0)
            if not cost:
                continue
            created = created_ms / 1000
            for window, (start, _) in bounds.items():
                if created >= start.timestamp():
                    totals = spend.totals[provider]
                    totals[window] = totals.get(window, 0.0) + cost
                    key = f"{provider}:{window}"
                    spend.oldest[key] = min(spend.oldest.get(key, created), created)
    return spend


def _read_messages(db: Path, floor_ms: int):
    """Read-only pull of every message since floor_ms. Never writes to the db."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise ProviderError(f"cannot open {db.name}: {exc}")
    try:
        conn.execute("PRAGMA query_only = 1")
        yield from conn.execute(
            "SELECT time_created, data FROM message WHERE time_created >= ?", (floor_ms,)
        )
    except sqlite3.Error as exc:
        raise ProviderError(f"cannot read {db.name}: {exc}")
    finally:
        conn.close()


def fetch_opencode(account: Account, calibration: dict) -> list[Record]:
    """Roll up opencode's own per-message cost accounting into the plan windows."""
    data_dir = account.folder
    dbs = _opencode_dbs(data_dir)
    auth = _opencode_auth(data_dir)
    windows = calibration.get("windows", {})
    age = _calibration_age(calibration)

    now = datetime.now(timezone.utc)
    bounds = window_bounds(now, calibration)
    spend = read_spend(dbs, bounds)

    records = []
    if "opencode-go" in auth:
        hours = windows.get("session", {}).get("hours", GO_SESSION_HOURS)
        for window, label in (
            ("session", f"go {hours}-hour"),
            ("week", "go week"),
            ("month", "go month"),
        ):
            config = windows.get(window, {})
            cap = config.get("cap", DEFAULT_CAPS[window])
            used = spend.get("opencode-go", window)
            resets = bounds[window][1]
            if resets is None:
                # A rolling window has no reset instant - the oldest spend simply
                # ages out. Report that moment; it's when budget first frees up.
                first = spend.oldest.get("opencode-go:session")
                resets = datetime.fromtimestamp(first + hours * 3600, timezone.utc) if first else None
            records.append(
                Record(
                    account=account.name,
                    provider="opencode",
                    window=window,
                    label=label,
                    used=round(used, 4),
                    limit=cap,
                    unit="usd",
                    pct=used / cap * 100 if cap else None,
                    resets_at=resets.isoformat() if resets else None,
                    source="local",
                    calibrated="cap" in config,
                    note=f"calibrated {age}" if "cap" in config else "assumed cap, never calibrated",
                )
            )
    if "opencode" in auth:
        used = spend.get("opencode", "calendar_month")
        records.append(
            Record(
                account=account.name,
                provider="opencode",
                window="month",
                label="zen month",
                used=round(used, 4),
                limit=None,
                unit="usd",
                pct=None,
                resets_at=bounds["calendar_month"][1].isoformat(),
                source="local",
                note="pay-as-you-go, uncapped",
            )
        )
    if not records:
        raise ProviderError(f"no opencode or opencode-go key in {data_dir / 'auth.json'}")
    return records


def _calibration_days(calibration: dict) -> int | None:
    stamp = calibration.get("calibrated_at")
    if not stamp:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).days


def _calibration_age(calibration: dict) -> str:
    days = _calibration_days(calibration)
    if days is None:
        return "never"
    if days >= CALIBRATION_STALE_DAYS:
        return f"{days}d ago, stale"
    return "today" if days < 1 else f"{days}d ago"


# --------------------------------------------------------------------------- #
# accounts - settings.json
# --------------------------------------------------------------------------- #
#
# A provider is code; an account is one login of it. Everything a provider needs
# to tell one login from another lives in a folder - ~/.claude for Claude Code,
# ~/.local/share/opencode for OpenCode - so an account is little more than a name
# pointing at a folder. Adding a second Claude Code login is therefore a settings
# entry, not a code change.

PROVIDERS = {"claude": fetch_claude, "opencode": fetch_opencode}

# Per provider: where its state lives by default, and the CLI that owns it.
PROVIDER_DEFAULTS = {
    "claude": (claude_config_dir, "claude"),
    "opencode": (opencode_data_dir, "opencode"),
}


def settings_file() -> Path | None:
    """$FUMES_SETTINGS, else settings.json beside the script, else under XDG."""
    if env := os.environ.get(SETTINGS_ENV):
        path = Path(env).expanduser()
        if not path.exists():
            raise ConfigError(f"{SETTINGS_ENV} points at {path}, which does not exist")
        return path
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    for candidate in (HERE / SETTINGS_NAME, xdg / "fumes" / SETTINGS_NAME):
        if candidate.exists():
            return candidate
    return None


def default_accounts() -> list[Account]:
    """No settings.json: one account per provider, where it has always looked."""
    return [
        Account(name=provider, provider=provider, folder=folder(), binary=binary)
        for provider, (folder, binary) in PROVIDER_DEFAULTS.items()
    ]


def load_accounts() -> list[Account]:
    path = settings_file()
    if path is None:
        return default_accounts()
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}")

    entries = data.get("accounts")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f'{path} needs a non-empty "accounts" list - see settings.example.json')

    accounts: list[Account] = []
    for index, entry in enumerate(entries):
        account = _read_account(entry, f"{path} accounts[{index}]")
        # Names key the calibration file and select on the command line, so a
        # duplicate would silently point two logins at one set of caps.
        if any(existing.name == account.name for existing in accounts):
            raise ConfigError(f"duplicate account name {account.name!r} in {path}")
        accounts.append(account)
    return accounts


def _read_account(entry: object, where: str) -> Account:
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} is not an object")
    provider = entry.get("provider")
    if provider not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS))
        raise ConfigError(f"{where} has provider {provider!r} - known providers are {known}")
    folder_default, binary_default = PROVIDER_DEFAULTS[provider]
    folder = entry.get("folder")
    return Account(
        name=str(entry.get("name") or provider),
        provider=provider,
        folder=_expand(folder) if folder else folder_default(),
        binary=str(entry.get("binary") or binary_default),
    )


def _expand(folder: object) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(folder))))


def select_accounts(accounts: list[Account], names: list[str] | None,
                    providers: list[str] | None) -> list[Account]:
    """Apply -a and -p, keeping the order the settings file declared."""
    if names:
        known = {account.name for account in accounts}
        if unknown := [name for name in names if name not in known]:
            raise ConfigError(
                f"no account named {', '.join(repr(n) for n in unknown)} - "
                f"configured: {', '.join(sorted(known))}"
            )
    chosen = [
        account for account in accounts
        if (not names or account.name in names)
        and (not providers or account.provider in providers)
    ]
    if not chosen:
        raise ConfigError("no account matches those filters")
    return chosen


def calibration_account(accounts: list[Account], name: str | None) -> Account:
    """Which account `calibrate` is talking about. Only OpenCode has caps to fit."""
    candidates = [account for account in accounts if account.provider == "opencode"]
    if not candidates:
        raise ConfigError("no opencode account configured - nothing to calibrate")
    if name:
        for account in candidates:
            if account.name == name:
                return account
        raise ConfigError(
            f"no opencode account named {name!r} - "
            f"configured: {', '.join(a.name for a in candidates)}"
        )
    if len(candidates) > 1:
        raise ConfigError(
            "several opencode accounts configured - pick one with -a: "
            + ", ".join(a.name for a in candidates)
        )
    return candidates[0]


# --------------------------------------------------------------------------- #
# calibrate command
# --------------------------------------------------------------------------- #


def calibrate(args) -> int:
    accounts = load_accounts()
    calibrations = load_calibration(accounts)

    # `--show` without an account is the only whole-file view: everything at once.
    if args.show and not args.account:
        if not calibrations:
            print("no calibration yet - run `calibrate --rolling N --weekly N --monthly N`")
            return 0
        print(json.dumps(calibrations, indent=2))
        return 0

    account = calibration_account(accounts, args.account)
    calibration = calibrations.get(account.name, {})

    if args.clear:
        if not calibration:
            print(f"{account.name} was never calibrated - nothing to clear")
            return 0
        calibrations.pop(account.name, None)
        save_calibration(calibrations)
        print(f"calibration cleared for {account.name} - back to assumed caps {DEFAULT_CAPS}")
        return 0

    if args.show:
        if not calibration:
            print(f"no calibration yet for {account.name} - "
                  "run `calibrate --rolling N --weekly N --monthly N`")
            return 0
        print(json.dumps(calibration, indent=2))
        return 0

    observed = {w: getattr(args, flag) for w, flag in WINDOW_FLAGS.items() if getattr(args, flag) is not None}
    if not observed:
        print("nothing to calibrate - pass at least one of --rolling / --weekly / --monthly",
              file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    windows = calibration.setdefault("windows", {})
    moved = []

    # Anchors first: a cap derived over the wrong window is meaningless.
    if args.weekly_resets:
        reset = now + parse_duration(args.weekly_resets)
        windows.setdefault("week", {})["reset_at"] = reset.isoformat()
        moved.append(f"weekly window now resets {reset:%a %d %b %H:%M} UTC")
    if args.monthly_resets:
        reset = now + parse_duration(args.monthly_resets)
        month = windows.setdefault("month", {})
        month["anchor_day"], month["anchor_time"] = reset.day, f"{reset:%H:%M}"
        moved.append(f"monthly window now anchored on day {reset.day} at {reset:%H:%M} UTC")

    bounds = window_bounds(now, calibration)
    spend = read_spend(_opencode_dbs(account.folder), bounds)

    rows, skipped, coarse = [], [], []
    for window, percent in observed.items():
        used = spend.get("opencode-go", window)
        config = windows.setdefault(window, {})
        was = config.get("cap", DEFAULT_CAPS[window])
        if percent <= 0:
            skipped.append(f"{window}: console reads 0% - nothing to divide into")
            continue
        if used < MIN_CALIBRATION_SPEND:
            skipped.append(
                f"{window}: only ${used:.2f} of local spend in this window "
                f"(need ${MIN_CALIBRATION_SPEND:.2f}) - cap left at ${was:.2f}"
            )
            continue
        cap = used / (percent / 100)
        config.update({
            "cap": round(cap, 4),
            "observed_pct": percent,
            "local_spend": round(used, 4),
            "at": now.isoformat(),
        })
        rows.append((window, used, percent, cap, was))
        if percent < 10:
            # The console rounds to whole percent, so a small reading carries a
            # large relative error: 6% is really 5.5-6.5%, i.e. +/-8% on the cap.
            span = (used / (percent / 100 + 0.005), used / (percent / 100 - 0.005))
            coarse.append(
                f"{window}: {percent:.0f}% is a coarse reading - cap is somewhere in "
                f"${span[0]:.2f}-${span[1]:.2f}. Recalibrate later in the window."
            )

    calibration["provider"] = "opencode-go"
    calibration["calibrated_at"] = now.isoformat()
    calibrations[account.name] = calibration
    save_calibration(calibrations)

    print(f"calibrated {account.name} against the OpenCode console "
          f"at {now.astimezone():%Y-%m-%d %H:%M}\n")
    if rows:
        print(f"  {'window':<8} {'local':>8} {'console':>8} {'effective cap':>14} {'was':>9}")
        for window, used, percent, cap, was in rows:
            print(f"  {window:<8} {'$%.2f' % used:>8} {'%.0f%%' % percent:>8} "
                  f"{'$%.2f' % cap:>14} {'$%.2f' % was:>9}")
    for line in moved:
        print(f"\n  {line}")
    for line in coarse:
        print(f"\n  heads up {line}")
    for line in skipped:
        print(f"\n  skipped {line}")
    print(f"\n  written to {CALIBRATION_FILE}")
    if args.weekly_resets or args.monthly_resets:
        print("  note: countdowns on the console are rounded, so derived anchors are +/- 1h")
    return 0


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

BAR_WIDTH = 16
GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _color(pct: float | None, enabled: bool) -> str:
    if not enabled or pct is None:
        return ""
    return GREEN if pct < 60 else YELLOW if pct < 85 else RED


def _bar(pct: float | None) -> str:
    if pct is None:
        return "-" * BAR_WIDTH
    filled = min(BAR_WIDTH, round(pct / 100 * BAR_WIDTH))
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def _until(iso: str | None) -> str:
    if not iso:
        return ""
    seconds = (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds()
    if seconds < 0:
        return "now"
    days, rem = divmod(int(seconds), 86400)
    hours, minutes = divmod(rem // 60, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _basis(rec: Record, ages: dict[str, str]) -> str:
    """How much to trust this row: server-given, calibrated, or guessed."""
    if rec.source == "live":
        return ""
    if rec.limit is None:
        return "uncapped"
    return f"cal {ages.get(rec.account, 'never')}" if rec.calibrated else "est"


def calibration_notice(account: Account, records: list[Record], calibration: dict) -> list[str]:
    """Say out loud what the `est` marker only whispers.

    An uncalibrated cap is not a rounding error - it has measured 3-4x too high,
    which makes the bar read comfortably low exactly when it shouldn't. The
    command comes with the account already filled in, so the fix is a paste.
    """
    capped = [
        rec for rec in records
        if rec.account == account.name and rec.source == "local" and rec.limit is not None
    ]
    if not capped:  # nothing here has a cap to be wrong about, e.g. Zen only
        return []
    if any(not rec.calibrated for rec in capped):
        return [
            "caps are assumed, not measured - typically 3-4x too high, so these read low.",
            "Read the percentages off console.opencode.ai, then:",
            f"./fumes.py calibrate -a {account.name} --rolling N --weekly N --monthly N",
        ]
    days = _calibration_days(calibration)
    if days is not None and days >= CALIBRATION_STALE_DAYS:
        return [f"calibration is {days}d old and drifts - recheck it against console.opencode.ai"]
    return []


def _heading(account: Account, color: bool) -> str:
    """The account's name, plus its provider when the name doesn't give it away."""
    if account.name == account.provider:
        return account.name
    dim, undim = (DIM, RESET) if color else ("", "")
    return f"{account.name} {dim}({account.provider}){undim}"


def render_table(accounts: list[Account], records: list[Record],
                 errors: list[tuple[Account, str]], color: bool,
                 calibrations: dict[str, dict]) -> str:
    ages = {name: _calibration_age(block) for name, block in calibrations.items()}
    cells = []
    for rec in records:
        cells.append((
            rec.label,
            _bar(rec.pct),
            f"{rec.pct:.0f}%" if rec.pct is not None else "",
            f"${rec.used:.2f}" if rec.unit == "usd" else "",
            f"resets in {_until(rec.resets_at)}" if rec.resets_at else "",
            _basis(rec, ages),
            _color(rec.pct, color),
        ))
    widths = [max((len(cell[i]) for cell in cells), default=0) for i in range(6)]

    lines = []
    failed = {account.name: message for account, message in errors}
    for account in accounts:
        rows = [cell for rec, cell in zip(records, cells) if rec.account == account.name]
        if not rows and account.name not in failed:
            continue
        lines.append(_heading(account, color))
        for cell in rows:
            tint, off = (cell[6], RESET) if cell[6] else ("", "")
            dim, undim = (DIM, RESET) if color else ("", "")
            lines.append(
                f"  {cell[0]:<{widths[0]}}  {tint}{cell[1]}{off}  {cell[2]:>{widths[2]}}  "
                f"{cell[3]:>{widths[3]}}  {dim}{cell[4]:<{widths[4]}}  {cell[5]}{undim}".rstrip()
            )
        if message := failed.get(account.name):
            lines.append(f"  {RED if color else ''}{message}{RESET if color else ''}")
        notice = calibration_notice(account, records, calibrations.get(account.name, {}))
        for index, line in enumerate(notice):
            tint, off = (YELLOW, RESET) if color else ("", "")
            lines.append(f"  {tint}{'!' if index == 0 else ' '} {line}{off}")
    return "\n".join(lines) if lines else "nothing to report"


def snapshot(accounts: list[Account], records: list[Record],
             errors: list[tuple[Account, str]], calibrations: dict[str, dict]) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "accounts": [
            {"name": a.name, "provider": a.provider, "folder": str(a.folder)} for a in accounts
        ],
        "records": [asdict(r) for r in records],
        "errors": [{"account": a.name, "provider": a.provider, "message": m} for a, m in errors],
        "calibrated_at": {
            name: block.get("calibrated_at") for name, block in calibrations.items()
        },
    }


def report(args) -> int:
    configured = load_accounts()
    # Ownership of a legacy calibration is positional, so it has to be resolved
    # against every configured account: -a must not decide who inherits it.
    calibrations = load_calibration(configured)
    accounts = select_accounts(configured, args.account, args.provider)

    records: list[Record] = []
    errors: list[tuple[Account, str]] = []
    for account in accounts:
        try:
            records.extend(PROVIDERS[account.provider](account, calibrations.get(account.name, {})))
        except ProviderError as exc:
            errors.append((account, str(exc)))

    payload = snapshot(accounts, records, errors, calibrations)
    if not args.no_history:
        with HISTORY_FILE.open("a") as handle:
            handle.write(json.dumps(payload) + "\n")

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_table(accounts, records, errors, sys.stdout.isatty(), calibrations))
    return 1 if errors and not records else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="How much is left across your AI providers.")
    parser.add_argument("-V", "--version", action="version", version=f"fumes {VERSION}")
    sub = parser.add_subparsers(dest="command")

    show = sub.add_parser("report", help="print current usage (default)")
    show.add_argument("-p", "--provider", choices=sorted(PROVIDERS), action="append",
                      help="limit to one provider (repeatable)")
    show.add_argument("-a", "--account", action="append",
                      help="limit to one configured account (repeatable)")
    show.add_argument("--json", action="store_true", help="emit normalized records")
    show.add_argument("--no-history", action="store_true", help="skip the history.jsonl snapshot")
    show.set_defaults(func=report)

    fit = sub.add_parser("calibrate", help="fit OpenCode Go caps to the console's percentages")
    fit.add_argument("-a", "--account", help="which opencode account (needed if you have several)")
    fit.add_argument("--rolling", type=float, help="rolling-window %% shown on the console")
    fit.add_argument("--weekly", type=float, help="weekly %% shown on the console")
    fit.add_argument("--monthly", type=float, help="monthly %% shown on the console")
    fit.add_argument("--weekly-resets", metavar="DUR", help="its weekly countdown, e.g. '5d 21h'")
    fit.add_argument("--monthly-resets", metavar="DUR", help="its monthly countdown, e.g. '30d 23h'")
    fit.add_argument("--show", action="store_true", help="print the stored calibration")
    fit.add_argument("--clear", action="store_true", help="forget it and use assumed caps")
    fit.set_defaults(func=calibrate)

    # No subcommand (or only flags) means `report` - but leave the parser's own
    # flags alone, so that bare --help lists the subcommands instead of just
    # report's own options, and --version doesn't become `report --version`.
    argv = sys.argv[1:]
    top_level = ("-h", "--help", "-V", "--version")
    if not argv or (argv[0].startswith("-") and argv[0] not in top_level):
        argv.insert(0, "report")
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ProviderError, ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
