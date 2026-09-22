#!/usr/bin/env python3
"""
fumes - how much is left before you are running on fumes? One view of AI provider limits and spend.

Providers:
    claude    live  - Claude Code's OAuth token against api.anthropic.com/api/oauth/usage.
                      Authoritative: these are the server's own numbers.
    opencode  live  - the Go plan's percentages from opencode's usage endpoint
                      (zen/go/v1/usage), authenticated with the API key opencode
                      already keeps in auth.json; Zen pay-as-you-go spend rolled
                      up from the console's usage export CSV, which needs a
                      service account key (see below).

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

    # Zen spend needs a service account key - create one at console.opencode.ai,
    # then put it on the account:
    #   {"name": "opencode", "provider": "opencode", "service_key": "oc_sk_..."}

Every report run appends a snapshot to history.jsonl beside this file (gitignored)
so burn-rate and trends are recoverable later.

Dependencies:
    pip install httpx
"""

import argparse
import csv
import json
import os
import re
import sys
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
HISTORY_FILE = HERE / "history.jsonl"
SETTINGS_NAME = "settings.json"
SETTINGS_ENV = "FUMES_SETTINGS"
TIMEOUT = 15.0

# Stamped into every history.jsonl snapshot: the file has already changed shape
# twice, so a reader shouldn't have to sniff which version wrote a given line.
VERSION = "0.6.0"

CLAUDE_CREDENTIALS_NAME = ".credentials.json"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BETA = "oauth-2025-04-20"

# The Go plan's percentages, straight from opencode's own metering. The key is
# the one opencode maintains in auth.json - deliberately read-only, like Claude
# Code's credentials. Undocumented endpoint: works today, can change.
ZEN_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"

# Zen spend comes from the console's usage export (documented:
# opencode.ai/console/guides/usage). Service account keys only; ranges start at
# midnight UTC, and 30d is the widest - which still covers the whole current
# calendar month, so the Zen row never needs a longer window.
DEFAULT_CONSOLE_URL = "https://opencode.ai/console"
USAGE_EXPORT_PATH = "/api/v1/usage/export"
EXPORT_RANGE = "30d"

GO_SESSION_HOURS = 5

# The CSV tags each record with how it was funded. Go-plan rows say `go` (and
# carry zero charge - the plan meters its own dollar-equivalents the CSV never
# sees); pay-as-you-go rows say `credit`. Web Search charges ride along on
# org-wide exports under their own service label.
GO_BILLING_SOURCE = "go"
CREDIT_BILLING_SOURCE = "credit"
WEB_SEARCH_SERVICE = "web-search"

# 100,000,000 micro-cents is one dollar - the export's own convention.
MICRO_CENTS_PER_DOLLAR = 100_000_000

# An account name is an identifier, not a label: downstream consumers put it in
# dotted key paths and in regexes matching them. A dot would split such a path,
# a space or a metacharacter would break the match - so allow only what is safe
# in all of those places.
ACCOUNT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class ProviderError(Exception):
    """An account could not be read. Never fatal - the other accounts still print.

    `records` carries what was fetched before the failure: one account can read
    from two endpoints, and a broken one shouldn't take the working one's rows
    with it.
    """

    def __init__(self, message: str, records: list["Record"] | None = None):
        super().__init__(message)
        self.records = records or []


class ConfigError(Exception):
    """settings.json is unusable. Fatal: guessing at a broken config is worse."""


@dataclass(frozen=True)
class Account:
    """One login of one provider. `folder` is where that provider keeps its state."""

    name: str  # what the table and -a call it; unique across the config
    provider: str
    folder: Path
    binary: str  # only ever named in hints, never executed
    exclude: tuple[str, ...] = ()  # record labels this account should not report
    service_key: str = ""  # opencode only: console service account key (Zen spend)
    console_url: str = ""  # opencode only: where the usage export lives


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
    source: str  # "live" - every record now comes from a server
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


def fetch_claude(account: Account) -> list[Record]:
    """Read the OAuth token Claude Code already maintains, then ask the server.

    The server hands over its own percentages; nothing is computed client-side.
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
    if not creds.get("accessToken") or not expires_at:
        raise ProviderError(f"no valid token - run `{_refresh_hint(account)}` to log in")
    if expires_at / 1000 <= datetime.now(timezone.utc).timestamp():
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
# opencode - reading
# --------------------------------------------------------------------------- #


def opencode_data_dir() -> Path:
    """Where OpenCode keeps its state when no account overrides it."""
    if env := os.environ.get("OPENCODE_DATA_DIR"):
        return Path(env)
    if xdg := os.environ.get("XDG_DATA_HOME"):
        return Path(xdg) / "opencode"
    return Path.home() / ".local" / "share" / "opencode"


def _opencode_auth(data_dir: Path) -> dict:
    """auth.json holds the API keys opencode itself maintains, one per login."""
    try:
        return json.loads((data_dir / "auth.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_go_usage(account: Account, api_key: str) -> list[Record]:
    """Ask opencode for the Go plan's own percentages.

    The plan is metered server-side - rolling 5h, weekly, monthly - and this
    endpoint hands those counters over with their reset instants, so no window
    is computed client-side at all. There is nothing to calibrate: these are
    the numbers the console shows. A 403 means this workspace has no Go
    subscription and yields no rows rather than an error - the Zen row, if
    any, still belongs to this account.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = httpx.get(ZEN_USAGE_URL, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            raise ProviderError(
                f"Go API key rejected - run `{account.binary}` and reconnect Go with /connect"
            )
        if status == 403:
            return []
        raise ProviderError(f"HTTP {status} from {ZEN_USAGE_URL}")
    except httpx.HTTPError as exc:
        raise ProviderError(f"request failed: {exc}")

    records = []
    for key, window, label in (
        ("rolling", "session", f"go {GO_SESSION_HOURS}-hour"),
        ("weekly", "week", "go week"),
        ("monthly", "month", "go month"),
    ):
        block = (data.get("usage") or {}).get(key)
        if not block:
            continue
        pct = float(block.get("percent", 0))
        records.append(
            Record(
                account=account.name,
                provider="opencode",
                window=window,
                label=label,
                used=pct,
                limit=100.0,
                unit="percent",
                pct=pct,
                # Reset instants come straight from the server - no derived
                # anchors, no countdown rounding to fudge. The 0% rolling block
                # is the one exception: a closed block resets on the next
                # message, not at a knowable instant, so it reports no
                # countdown rather than the server's made-up "+5h".
                resets_at=block.get("resetsAt") if (pct > 0 or window != "session") else None,
                source="live",
            )
        )
    return records


def _calendar_month(now: datetime) -> tuple[datetime, datetime]:
    """(start, end) of the UTC month `now` sits in."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=monthrange(start.year, start.month)[1])


def _export_error(response: httpx.Response) -> str:
    """Turn the export's error body into a sentence worth reading."""
    try:
        body = json.loads(response.text)
        detail = body.get("message") or (body.get("error") or {}).get("message")
    except (json.JSONDecodeError, AttributeError):
        detail = None
    if response.status_code == 401:
        reason = detail or "service API key missing, invalid, expired or revoked"
        return f"usage export rejected: {reason} - create a service account key in the console"
    if response.status_code == 403:
        return f"usage export rejected: {detail or 'this service account may not read usage'}"
    return f"HTTP {response.status_code} from the usage export{': ' + detail if detail else ''}"


def fetch_zen_spend(account: Account, service_key: str, console_url: str) -> Record:
    """Roll the console's usage export up into Zen's calendar month.

    The export streams the console's own accounting as CSV, newest first. Only
    records charged to the pay-as-you-go balance count here - `credit` - plus
    Web Search rows, which bill the same balance under their own service label.
    BYOK and free usage carry no charge and Go-plan rows carry no dollars at
    all, so nothing else can inflate the spend.
    """
    url = console_url.rstrip("/") + USAGE_EXPORT_PATH
    headers = {"Authorization": f"Bearer {service_key}", "Accept": "text/csv"}
    month_start, month_end = _calendar_month(datetime.now(timezone.utc))
    micro_cents = 0
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            with client.stream(
                "GET", url, params={"scope": "organization", "range": EXPORT_RANGE}, headers=headers
            ) as response:
                if response.status_code != 200:
                    response.read()
                    raise ProviderError(_export_error(response))
                for row in csv.DictReader(response.iter_lines()):
                    if row.get("billing_source") == CREDIT_BILLING_SOURCE or row.get(
                        "service"
                    ) == WEB_SEARCH_SERVICE:
                        created = row.get("created_at") or ""
                        try:
                            created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        except (ValueError, TypeError):
                            continue
                        if created_at < month_start:
                            continue
                        try:
                            micro_cents += int(row.get("cost_micro_cents") or 0)
                        except (ValueError, TypeError):
                            continue
    except httpx.HTTPError as exc:
        raise ProviderError(f"request failed: {exc}")
    except csv.Error as exc:
        raise ProviderError(f"cannot read the usage export: {exc}")
    return Record(
        account=account.name,
        provider="opencode",
        window="month",
        label="zen month",
        used=round(micro_cents / MICRO_CENTS_PER_DOLLAR, 4),
        limit=None,
        unit="usd",
        pct=None,
        resets_at=month_end.isoformat(),
        source="live",
        note="pay-as-you-go, uncapped",
    )


def fetch_opencode(account: Account) -> list[Record]:
    """Read this account's numbers from opencode's own servers.

    Go windows come from the usage endpoint, authenticated with the key
    opencode already maintains in auth.json - read-only, like Claude Code's
    credentials. Zen spend needs the console's usage export, which accepts
    service account keys only, so an account without a service_key simply has
    no Zen row. The two endpoints fail independently: whatever worked still
    reports, with the failure named alongside it.
    """
    auth = _opencode_auth(account.folder)
    records = []
    problems = []
    if go_key := (auth.get("opencode-go") or {}).get("key"):
        try:
            records.extend(fetch_go_usage(account, go_key))
        except ProviderError as exc:
            problems.append(f"Go usage: {exc}")
    if account.service_key:
        try:
            records.append(fetch_zen_spend(account, account.service_key, account.console_url))
        except ProviderError as exc:
            problems.append(f"Zen spend: {exc}")
    if not records:
        if problems:
            raise ProviderError("; ".join(problems))
        raise ProviderError(
            f"no opencode-go key in {account.folder / 'auth.json'} and no service_key "
            "on this account - nothing to ask the server for"
        )
    if problems:
        raise ProviderError("; ".join(problems), records=records)
    return records


# --------------------------------------------------------------------------- #
# accounts - settings.json
# --------------------------------------------------------------------------- #
#
# A provider is code; an account is one login of it. Everything a provider needs
# to tell one login from another lives either in a folder - ~/.claude for Claude
# Code, ~/.local/share/opencode for OpenCode - or, for credentials the console
# hands out separately, on the account itself: an OpenCode service_key. Adding a
# second Claude Code login is therefore a settings entry, not a code change.

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
        # Names are what -a selects on and what downstream tools key off, so a
        # duplicate would silently point two logins at one identity.
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
    name = str(entry.get("name") or provider)
    # fullmatch, not match: `$` would let a trailing newline through.
    if not ACCOUNT_NAME.fullmatch(name):
        raise ConfigError(
            f"{where}: account name {name!r} is not valid - start with a letter or "
            "digit and use only letters, digits, '-' and '_' (the name is used as a "
            "key by this tool and others)"
        )
    exclude = entry.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(x, str) for x in exclude):
        raise ConfigError(f"{where}: 'exclude' must be a list of record labels")
    service_key = entry.get("service_key")
    if service_key is not None and not isinstance(service_key, str):
        raise ConfigError(f"{where}: 'service_key' must be a string")
    console_url = entry.get("console_url")
    if console_url is not None and not isinstance(console_url, str):
        raise ConfigError(f"{where}: 'console_url' must be a string")
    return Account(
        name=name,
        provider=provider,
        folder=_expand(folder) if folder else folder_default(),
        binary=str(entry.get("binary") or binary_default),
        exclude=tuple(exclude),
        service_key=str(service_key or "").strip(),
        console_url=str(console_url or DEFAULT_CONSOLE_URL),
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


def _basis(rec: Record) -> str:
    """How much to trust this row: server-given, or pay-as-you-go."""
    if rec.limit is None:
        return "uncapped"
    # Everything else is the server's own number - a percentage it computed or
    # a charge it levied - so the row needs no disclaimer.
    return ""


def _heading(account: Account, color: bool) -> str:
    """The account's name, plus its provider when the name doesn't give it away."""
    if account.name == account.provider:
        return account.name
    dim, undim = (DIM, RESET) if color else ("", "")
    return f"{account.name} {dim}({account.provider}){undim}"


def render_table(accounts: list[Account], records: list[Record],
                 errors: list[tuple[Account, str]], color: bool) -> str:
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
    return "\n".join(lines) if lines else "nothing to report"


def snapshot(accounts: list[Account], records: list[Record],
             errors: list[tuple[Account, str]]) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "accounts": [
            {"name": a.name, "provider": a.provider, "folder": str(a.folder)} for a in accounts
        ],
        "records": [asdict(r) for r in records],
        "errors": [{"account": a.name, "provider": a.provider, "message": m} for a, m in errors],
    }


def report(args) -> int:
    configured = load_accounts()
    accounts = select_accounts(configured, args.account, args.provider)

    records: list[Record] = []
    errors: list[tuple[Account, str]] = []
    for account in accounts:
        try:
            fetched = PROVIDERS[account.provider](account)
            # An account's exclude list drops records by label, so a login can
            # carry a subscription whose numbers this output should not report.
            records.extend(r for r in fetched if r.label not in account.exclude)
        except ProviderError as exc:
            errors.append((account, str(exc)))
            records.extend(r for r in exc.records if r.label not in account.exclude)

    payload = snapshot(accounts, records, errors)
    if not args.no_history:
        with HISTORY_FILE.open("a") as handle:
            handle.write(json.dumps(payload) + "\n")

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_table(accounts, records, errors, sys.stdout.isatty()))
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
