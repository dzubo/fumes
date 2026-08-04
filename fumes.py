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

Usage:
    ./fumes.py                  # table
    ./fumes.py --json           # normalized records
    ./fumes.py -p claude        # one provider
    ./fumes.py --no-history     # don't append a snapshot

    # teach it the real OpenCode Go numbers, read off console.opencode.ai
    ./fumes.py calibrate --rolling 42 --weekly 87 --monthly 6 \
        --weekly-resets "5d 21h" --monthly-resets "30d 23h"
    ./fumes.py calibrate --show
    ./fumes.py calibrate --clear

Every report run appends a snapshot to history.jsonl beside this file (gitignored)
so burn-rate and trends are recoverable later.

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
TIMEOUT = 15.0

CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
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
    """A provider could not be read. Never fatal - other providers still print."""


@dataclass
class Record:
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


def fetch_claude() -> list[Record]:
    """Read the OAuth token Claude Code already maintains, then ask the server."""
    try:
        creds = json.loads(CLAUDE_CREDENTIALS.read_text())["claudeAiOauth"]
    except FileNotFoundError:
        raise ProviderError(f"no credentials at {CLAUDE_CREDENTIALS} - is Claude Code installed?")
    except (KeyError, json.JSONDecodeError) as exc:
        raise ProviderError(f"unreadable credentials: {exc}")

    # Deliberately read-only: Claude Code owns this file and refreshes the token
    # itself. Refreshing here would race it, so an expired token is just reported.
    expires_at = creds.get("expiresAt")
    if expires_at and expires_at / 1000 <= datetime.now(timezone.utc).timestamp():
        when = datetime.fromtimestamp(expires_at / 1000).strftime("%H:%M")
        raise ProviderError(f"OAuth token expired at {when} - run any Claude Code command to refresh")

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


def load_calibration() -> dict:
    try:
        return json.loads(CALIBRATION_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_calibration(data: dict) -> None:
    CALIBRATION_FILE.write_text(json.dumps(data, indent=2) + "\n")


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


def fetch_opencode() -> list[Record]:
    """Roll up opencode's own per-message cost accounting into the plan windows."""
    data_dir = opencode_data_dir()
    dbs = _opencode_dbs(data_dir)
    auth = _opencode_auth(data_dir)
    calibration = load_calibration()
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


def _calibration_age(calibration: dict) -> str:
    stamp = calibration.get("calibrated_at")
    if not stamp:
        return "never"
    days = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).days
    if days >= CALIBRATION_STALE_DAYS:
        return f"{days}d ago, stale"
    return "today" if days < 1 else f"{days}d ago"


# --------------------------------------------------------------------------- #
# calibrate command
# --------------------------------------------------------------------------- #


def calibrate(args) -> int:
    if args.clear:
        CALIBRATION_FILE.unlink(missing_ok=True)
        print(f"calibration cleared - back to assumed caps {DEFAULT_CAPS}")
        return 0

    calibration = load_calibration()
    if args.show:
        if not calibration:
            print("no calibration yet - run `calibrate --rolling N --weekly N --monthly N`")
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
    spend = read_spend(_opencode_dbs(opencode_data_dir()), bounds)

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
    save_calibration(calibration)

    print(f"calibrated against the OpenCode console at {now.astimezone():%Y-%m-%d %H:%M}\n")
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

PROVIDERS = {"claude": fetch_claude, "opencode": fetch_opencode}

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


def _basis(rec: Record) -> str:
    """How much to trust this row: server-given, calibrated, or guessed."""
    if rec.source == "live":
        return ""
    if rec.limit is None:
        return "uncapped"
    return f"cal {_calibration_age(load_calibration())}" if rec.calibrated else "est"


def render_table(records: list[Record], errors: list[tuple[str, str]], color: bool) -> str:
    cells = []
    for rec in records:
        cells.append((
            rec.label,
            _bar(rec.pct),
            f"{rec.pct:.0f}%" if rec.pct is not None else "",
            f"${rec.used:.2f}" if rec.unit == "usd" else "",
            f"resets in {_until(rec.resets_at)}" if rec.resets_at else "",
            _basis(rec),
            _color(rec.pct, color),
        ))
    widths = [max((len(cell[i]) for cell in cells), default=0) for i in range(6)]

    lines = []
    for provider in PROVIDERS:
        rows = [(rec, cell) for rec, cell in zip(records, cells) if rec.provider == provider]
        if not rows:
            continue
        lines.append(provider)
        for _, cell in rows:
            tint, off = (cell[6], RESET) if cell[6] else ("", "")
            dim, undim = (DIM, RESET) if color else ("", "")
            lines.append(
                f"  {cell[0]:<{widths[0]}}  {tint}{cell[1]}{off}  {cell[2]:>{widths[2]}}  "
                f"{cell[3]:>{widths[3]}}  {dim}{cell[4]:<{widths[4]}}  {cell[5]}{undim}".rstrip()
            )
    for provider, message in errors:
        lines.append(f"{provider}\n  {RED if color else ''}{message}{RESET if color else ''}")
    return "\n".join(lines) if lines else "nothing to report"


def snapshot(records: list[Record], errors: list[tuple[str, str]]) -> dict:
    calibration = load_calibration()
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "records": [asdict(r) for r in records],
        "errors": [{"provider": p, "message": m} for p, m in errors],
        "calibrated_at": calibration.get("calibrated_at"),
    }


def report(args) -> int:
    wanted = args.provider or list(PROVIDERS)
    records: list[Record] = []
    errors: list[tuple[str, str]] = []
    for name in wanted:
        try:
            records.extend(PROVIDERS[name]())
        except ProviderError as exc:
            errors.append((name, str(exc)))

    payload = snapshot(records, errors)
    if not args.no_history:
        with HISTORY_FILE.open("a") as handle:
            handle.write(json.dumps(payload) + "\n")

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_table(records, errors, color=sys.stdout.isatty()))
    return 1 if errors and not records else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="How much is left across your AI providers.")
    sub = parser.add_subparsers(dest="command")

    show = sub.add_parser("report", help="print current usage (default)")
    show.add_argument("-p", "--provider", choices=sorted(PROVIDERS), action="append",
                      help="limit to one provider (repeatable)")
    show.add_argument("--json", action="store_true", help="emit normalized records")
    show.add_argument("--no-history", action="store_true", help="skip the history.jsonl snapshot")
    show.set_defaults(func=report)

    fit = sub.add_parser("calibrate", help="fit OpenCode Go caps to the console's percentages")
    fit.add_argument("--rolling", type=float, help="rolling-window %% shown on the console")
    fit.add_argument("--weekly", type=float, help="weekly %% shown on the console")
    fit.add_argument("--monthly", type=float, help="monthly %% shown on the console")
    fit.add_argument("--weekly-resets", metavar="DUR", help="its weekly countdown, e.g. '5d 21h'")
    fit.add_argument("--monthly-resets", metavar="DUR", help="its monthly countdown, e.g. '30d 23h'")
    fit.add_argument("--show", action="store_true", help="print the stored calibration")
    fit.add_argument("--clear", action="store_true", help="forget it and use assumed caps")
    fit.set_defaults(func=calibrate)

    # No subcommand (or only flags) means `report` - but leave -h alone, so that
    # bare --help lists the subcommands instead of just report's own options.
    argv = sys.argv[1:]
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv.insert(0, "report")
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ProviderError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
