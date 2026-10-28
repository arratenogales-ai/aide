"""Validated configuration: .env (pydantic-settings) and the YAML files in config/.

The YAML files are validated at startup and every time they are edited from the panel. A
validation error prevents starting with a broken configuration.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aide.common.paths import Paths

Alias = Literal["opus", "sonnet", "haiku", "fable"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

REQUIRED_ROLES = (
    "triage",
    "quick_reply",
    "orchestrator",
    "orchestrator_small",
    "researcher",
    "reader",
    "builder",
    "writer",
    "navigator",
    "reviewer",
    "quick_reviewer",
    "light_reviewer",
    "summary",
    "maintenance",
    "email",
)


# Spanish weekday names still accepted as aliases of the English name.
SPANISH_DAYS = {
    "monday": "monday",
    "martes": "tuesday",
    "miercoles": "wednesday",
    "miércoles": "wednesday",
    "jueves": "thursday",
    "viernes": "friday",
    "saturday": "saturday",
    "sábado": "saturday",
    "sunday": "sunday",
}


class ConfigError(Exception):
    """The configuration is not valid."""


class Strict(BaseModel):
    """Base settings model that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


# --- .env -------------------------------------------------------------------------------


class Environment(BaseSettings):
    """Variables from .env. They are not exported to os.environ: they are read here and used by
    code."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    # Name of the person the agent serves: the bot, the panel and the prompts call them this. If
    # empty, neutral phrases are used (“the user”).
    USER_NAME: str = ""
    TELEGRAM_BOT_TOKEN: SecretStr | None = None
    TELEGRAM_ALLOWED_USER_ID: int | None = None
    GOOGLE_CREDENTIALS_PATH: str = "config/credentials.json"
    PANEL_HOST: str | None = None
    PANEL_PORT: int | None = None
    # Name of the Mac on the user's Tailscale network (`<mac>.<red>.ts.net`) to open the panel
    # from the phone with `tailscale serve`. If empty, the panel is only for this Mac.
    PANEL_TAILSCALE: str | None = None
    TIME_ZONE: str | None = None
    # The user's Gmail and Calendar account. It goes here, not in settings.yaml, so that
    # personal data does not enter the repository.
    GOOGLE_ACCOUNT: str | None = None
    CLAUDE_CODE_OAUTH_TOKEN: SecretStr | None = None
    # Transit only: with `claude.credential: api_key`, at startup it is saved in the keychain
    # and never reaches the agent's sessions.
    ANTHROPIC_API_KEY: SecretStr | None = None

    @field_validator("TELEGRAM_ALLOWED_USER_ID", "PANEL_PORT", "GOOGLE_ACCOUNT", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("PANEL_TAILSCALE", mode="before")
    @classmethod
    def _tailscale_name(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            return None
        name = value.strip().lower().rstrip(".")
        if not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", name):
            raise ValueError("PANEL_TAILSCALE must be the Mac's Tailscale name (x.y.ts.net)")
        return name

    @field_validator("USER_NAME", mode="before")
    @classmethod
    def _clean_name(cls, value: object) -> object:
        if value is None:
            return ""
        return " ".join(str(value).split())[:60]

    @field_validator(
        "TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", mode="before"
    )
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


def load_environment(paths: Paths) -> Environment:
    file = paths.env if paths.env.exists() else None
    return Environment(_env_file=file)  # type: ignore[call-arg]


# --- models.yaml -----------------------------------------------------------------------


class ModelRole(Strict):
    """A model assigned to a role, with its alias, id and effort level."""

    alias: Alias
    id: str = Field(min_length=3)
    effort: Effort


class FallbackModel(Strict):
    """A fallback model, with its alias and id."""

    alias: Alias
    id: str


class Models(Strict):
    """The model roles and fallback; validates that all required roles exist."""

    roles: dict[str, ModelRole]
    forceable: dict[Literal["opus", "sonnet", "maximum"], ModelRole]
    opus_limit_fallback: FallbackModel

    @model_validator(mode="after")
    def _complete_roles(self) -> Models:
        missing = [r for r in REQUIRED_ROLES if r not in self.roles]
        if missing:
            raise ValueError(f"roles missing in models.yaml: {', '.join(missing)}")
        return self


# --- settings.yaml -----------------------------------------------------------------------


class Cap(Strict):
    """A limit for a task: turns, dollar cost and an optional token budget."""

    turns: int = Field(gt=0)
    usd: float = Field(gt=0)
    # Token budget of the task, even if it does not use Chrome. Without it, only tasks
    # with the real Chrome have a token budget.
    tokens: int | None = Field(default=None, ge=100_000)


class Caps(Strict):
    """The limits per task size and type, with a factor used to extend them."""

    small: Cap
    medium: Cap
    large: Cap
    quick: Cap
    triage: Cap
    review: Cap
    light_review: Cap = Field(default_factory=lambda: Cap(turns=5, usd=0.2))
    extension_factor: float = Field(default=2.0, gt=1.0)


class QuickConversation(Strict):
    """Settings for quick conversations: inactivity minutes and maximum exchanges."""

    inactivity_minutes: int = Field(default=30, gt=0)
    max_exchanges: int = Field(default=20, gt=0)


class AllowedFolder(Strict):
    """A folder the agent may access, in reading or write mode."""

    path: str
    mode: Literal["reading", "write"] = "reading"

    def resolved(self) -> Path:
        return Path(self.path).expanduser().resolve()


class ClaudeAccount(Strict):
    """How the agent signs in to Claude (README, “Claude account”).

    - api_key: a Claude Console key stored in the keychain; Claude Code requests it with
      `apiKeyHelper` and it is never in the sessions' environment. It is what the Agent SDK
      documentation asks for applications built with it.
    - login: Claude Code's /login on this Mac. Needed for the real Chrome (Claude in Chrome
      does not work with an API key).
    """

    credential: Literal["api_key", "login"] = "api_key"


class RealBrowser(Strict):
    """Settings for the real Chrome: enabled flag, chosen browser and per-task limits."""

    # Off by default: needs `claude.credential: login` (see ClaudeAccount).
    enabled: bool = False
    # Browser the user chose in /chrome (this Mac's). Startup checks that it is the connected
    # one and the one in use.
    browser: str | None = None
    device_id: str | None = None
    # Limits for each task that uses the real Chrome: browser actions (on reaching it,
    # the agent delivers with what it has) and token usage (on exceeding it, the task stops and
    # asks the user whether to continue, as with the turn cap).
    max_actions: int = Field(default=60, ge=1, le=1000)
    max_tokens: int = Field(default=3_000_000, ge=100_000)
    # On spending this part of the budget, the policy stops allowing browsing and the agent
    # delivers with what it has; the budget cutoff remains as a backup.
    token_notice: float = Field(default=0.85, gt=0, le=1)


class Voice(Strict):
    """Voice note settings: transcription model and language."""

    model: str = "mlx-community/whisper-small-mlx"
    # Language of voice notes for Whisper. Empty: it detects it itself; "en", "es"... sets it.
    language: str = ""


class Scheduler(Strict):
    """Scheduler settings: default policy and time window in hours."""

    default_policy: Literal["run_if_recent", "run_always", "skip"] = "run_if_recent"
    window_hours: float = Field(default=12, gt=0)


class McpServer(Strict):
    """An MCP server config with a default permission level and per-name levels."""

    config: dict
    default_level: int = Field(default=2, ge=0, le=3)
    levels: dict[str, int] = Field(default_factory=dict)

    @field_validator("levels")
    @classmethod
    def _valid_levels(cls, levels: dict[str, int]) -> dict[str, int]:
        for name, level in levels.items():
            if level not in (0, 1, 2, 3):
                raise ValueError(f"invalid level for {name}: {level}")
        return levels


class GoogleSettings(Strict):
    """Google account settings: calendar, working hours and days, token validity."""

    # The user's Gmail and Calendar account: the program checks that the authorized one is this.
    # Normally it is set in .env (GOOGLE_ACCOUNT), which takes precedence over this value.
    account: str | None = None
    # The OAuth app is in "Testing" state: the refresh token expires after 7 days.
    validity_days: int = Field(default=7, ge=1)
    calendar: str = "primary"
    # Hours for finding free slots (local time) and working days (0 = Monday).
    start_hour: str = "09:00"
    end_hour: str = "18:00"
    working_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])


class SandboxSettings(Strict):
    """Sandbox settings: whether it is enabled and which domains are allowed."""

    enabled: bool = True
    # Domains Bash commands may connect to without asking.
    allowed_domains: list[str] = Field(default_factory=list)


class Maintenance(Strict):
    """Weekly maintenance: when it runs and what it keeps."""

    active: bool = True
    day: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ] = "sunday"  # fmt: skip
    hour: str = "19:00"
    # Internal data of finished tasks (with their final folder), deleted after this time.
    internal_data_days: int = Field(default=30, ge=7)
    log_days: int = Field(default=30, ge=7)
    test_leftover_days: int = Field(default=7, ge=1)
    backups_to_keep: int = Field(default=4, ge=1, le=20)
    # Check updates by trying them in a separate environment (takes a few minutes).
    test_updates: bool = True

    @field_validator("day", mode="before")
    @classmethod
    def _day_in_english(cls, value: object) -> object:
        """Also accepts the Spanish names (as before) and capitals: “Domingo” → sunday."""
        if isinstance(value, str):
            name = value.strip().lower()
            return SPANISH_DAYS.get(name, name)
        return value

    @field_validator("hour")
    @classmethod
    def _valid_hour(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("the time must be HH:MM")
        return value


class TelegramSettings(Strict):
    """Telegram settings, such as the text length limit."""

    text_limit: int = Field(default=3500, gt=100, le=4096)


RE_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}")


def valid_domain(value: object) -> str:
    """An email domain in lowercase (“PayPal.com.” → “paypal.com”), or ValueError with what is
    wrong, in English."""
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not a domain: write it as text, e.g. paypal.com")
    domain = value.strip().lower().rstrip(".")
    if not domain:
        raise ValueError("empty domain: remove the line or write a domain, e.g. paypal.com")
    if "@" in domain:
        raise ValueError(
            f"{value!r} looks like an email address: write only the domain (the part after @), "
            "e.g. paypal.com"
        )
    if "://" in domain or "/" in domain:
        raise ValueError(
            f"{value!r} looks like a web address: write only the domain, without http:// or a "
            "path, e.g. paypal.com"
        )
    if len(domain) > 253 or not RE_DOMAIN.fullmatch(domain):
        raise ValueError(f"{value!r} is not a valid domain, e.g. paypal.com")
    return domain


class EmailDomains(Strict):
    """Important senders by domain (each domain also covers its subdomains): an email from them is
    notified without asking the model, except bulk mailings. Empty by default; settings.yaml
    includes an example that each person replaces with their own."""

    bank: list[str] = Field(default_factory=list)
    insurance: list[str] = Field(default_factory=list)
    official: list[str] = Field(default_factory=list)

    @field_validator("bank", "insurance", "official", mode="before")
    @classmethod
    def _domains(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str) or not isinstance(value, list):
            raise ValueError('write a list of domains, one per line starting with "- "')
        output: list[str] = []
        for x in value:
            domain = valid_domain(x)
            if domain not in output:
                output.append(domain)
        return output

    def by_category(self) -> dict[str, tuple[str, ...]]:
        """The lists per category, in the order they are tried."""
        return {
            "bank": tuple(self.bank),
            "insurance": tuple(self.insurance),
            "official": tuple(self.official),
        }


class EmailSettings(Strict):
    """Gmail reading: read-only, off until the user turns it on."""

    active: bool = False
    # How often, in minutes, new emails are checked (API only, no model).
    minutes: float = Field(default=5, ge=1, le=120)
    # Banks, insurers and official bodies that are always notified (except bulk mailings).
    domains: EmailDomains = EmailDomains()


class NoWifi(Strict):
    """Days without wifi, when the Mac only connects now and then using the phone's data. It only
    changes what is automatic; what the user launches runs as usual. It does not turn off by itself."""

    active: bool = False
    # How often, in minutes, email is checked (besides when the connection returns).
    email_minutes: float = Field(default=30, ge=5, le=240)
    # Wait of each Telegram request when there are no messages: longer, fewer requests.
    telegram_polling_seconds: int = Field(default=50, ge=10, le=50)


class MorningSummary(Strict):
    """The morning summary: by code, instead of the rain warning, and only on days with
    something."""

    active: bool = False
    # From what time messages are sent and until when (later, it is not sent that day), in local
    # time.
    hour: str = Field(default="07:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    until: str = Field(default="11:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    # The rain warning configuration (places and thresholds), relative to agent/.
    weather: str = "projects/rain-alert/config.json"

    @model_validator(mode="after")
    def _order(self) -> MorningSummary:
        if self.until <= self.hour:
            raise ValueError("morning_summary: 'until' must be later than 'hour'")
        return self


class Settings(Strict):
    """The main application settings, including time zone, caps, folders and voice."""

    time_zone: str = "UTC"
    caps: Caps
    simultaneous_heavy_tasks: int = Field(default=1, ge=1, le=2)
    resume_session_hours: float = Field(default=12, gt=0)
    quick_conversation: QuickConversation = QuickConversation()
    approval_reminder_hours: float = Field(default=4, gt=0)
    periodic_check_minutes: float = Field(default=5, gt=0)
    review_max_rounds: int = Field(default=2, ge=0, le=5)
    allowed_folders: list[AllowedFolder] = Field(default_factory=list)
    real_browser: RealBrowser = RealBrowser()
    voice: Voice = Voice()
    scheduler: Scheduler = Scheduler()
    maintenance: Maintenance = Maintenance()
    mcp_servers: dict[str, McpServer] = Field(default_factory=dict)
    telegram: TelegramSettings = TelegramSettings()
    sandbox: SandboxSettings = SandboxSettings()
    google: GoogleSettings = GoogleSettings()
    email: EmailSettings = EmailSettings()
    no_wifi: NoWifi = NoWifi()
    morning_summary: MorningSummary = MorningSummary()
    claude: ClaudeAccount = ClaudeAccount()

    @model_validator(mode="after")
    def _chrome_needs_login(self) -> Settings:
        if self.claude.credential == "api_key" and self.real_browser.enabled:
            raise ValueError(
                "real_browser needs claude.credential: login (Claude in Chrome only works with "
                "Claude Code's /login, not with an API key); turn real_browser off or change "
                "the credential"
            )
        return self

    @field_validator("time_zone")
    @classmethod
    def _valid_zone(cls, zone: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown time zone: {zone}") from e
        return zone

    @field_validator("mcp_servers")
    @classmethod
    def _mcp_names(cls, servers: dict[str, McpServer]) -> dict[str, McpServer]:
        for name in servers:
            if name in ("aide", "claude-in-chrome") or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                raise ValueError(f"invalid or reserved MCP server name: {name}")
        return servers


# --- permissions.yaml ----------------------------------------------------------------------


class PathPermissions(Strict):
    """Path rules: forbidden, forbidden to write, needing approval and free to read."""

    forbidden: list[str]
    forbidden_write: list[str]
    write_approval: list[str]
    free_read: list[str]


class BashPermissions(Strict):
    """Rules for Bash commands: forbidden, approval and notice patterns."""

    # Commands searched for in command position (not at any word in the text).
    forbidden_commands: list[str] = Field(default_factory=list)
    commands_approval: list[str] = Field(default_factory=list)
    # Regular expressions over the text without heredocs or quoted content.
    forbidden: list[str]
    approval: list[str]
    notice: list[str]

    @field_validator("forbidden", "approval", "notice")
    @classmethod
    def _valid_regex(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"invalid regular expression {pattern!r}: {e}") from e
        return patterns


class ChromePermissions(Strict):
    """Rules for the real Chrome: forbidden, approval-only and account domains, pages."""

    forbidden_domains: list[str]
    domains_only_with_approval: list[str]
    domains_with_account: list[str]
    send_words: list[str]
    # Pages never opened in the real Chrome (“domain/path” without www, with wildcards):
    # payment, billing, bank, taxes, account settings and purchases. They apply to any task.
    forbidden_pages: list[str] = Field(default_factory=list)


class Permissions(Strict):
    """All permission rules: own tool levels, paths, Bash and Chrome."""

    own_tools: dict[str, int]
    default_external_mcp_level: int = Field(default=2, ge=0, le=3)
    paths: PathPermissions
    bash: BashPermissions
    chrome: ChromePermissions

    @field_validator("own_tools")
    @classmethod
    def _levels(cls, levels: dict[str, int]) -> dict[str, int]:
        for name, level in levels.items():
            if level not in (0, 1, 2, 3):
                raise ValueError(f"invalid level for {name}: {level}")
        return levels


# --- loading ----------------------------------------------------------------------------


def read_yaml(path: Path) -> dict:
    """Reads a YAML mapping from a file, raising ConfigError on any problem."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"{path} does not exist") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path.name}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping")
    return data


class Config:
    """All the program's configuration, already validated."""

    def __init__(
        self,
        paths: Paths,
        environment: Environment,
        models: Models,
        settings: Settings,
        permissions: Permissions,
    ) -> None:
        self.paths = paths
        self.environment = environment
        self.models = models
        self.settings = settings
        self.permissions = permissions

    @classmethod
    def load(cls, paths: Paths, models_file: str | None = None) -> Config:
        """Loads and validates the models, settings and permissions files into a Config."""
        models_name = models_file or "models.yaml"
        try:
            models = Models.model_validate(read_yaml(paths.config / models_name))
            settings = Settings.model_validate(read_yaml(paths.config / "settings.yaml"))
            permissions = Permissions.model_validate(read_yaml(paths.config / "permissions.yaml"))
        except ConfigError:
            raise
        except Exception as e:  # pydantic.ValidationError
            raise ConfigError(str(e)) from e
        return cls(paths, load_environment(paths), models, settings, permissions)

    @property
    def time_zone(self) -> str:
        return self.environment.TIME_ZONE or self.settings.time_zone

    @property
    def google_account(self) -> str | None:
        """The user's Google account: the one in .env and, if there is none, the one in
        settings.yaml."""
        return self.environment.GOOGLE_ACCOUNT or self.settings.google.account

    @property
    def user(self) -> str:
        """The user's name (USER_NAME in .env), or an empty string if not set."""
        return self.environment.USER_NAME

    @property
    def panel_host(self) -> str:
        return self.environment.PANEL_HOST or "127.0.0.1"

    @property
    def panel_port(self) -> int:
        return self.environment.PANEL_PORT or 8765

    @property
    def panel_tailscale(self) -> str | None:
        return self.environment.PANEL_TAILSCALE

    @property
    def real_browser(self) -> bool:
        return self.settings.real_browser.enabled

    @property
    def uses_api_key(self) -> bool:
        """Whether the agent signs in to Claude with the keychain API key (and not with /login)."""
        return self.settings.claude.credential == "api_key"

    def role(self, name: str) -> ModelRole:
        return self.models.roles[name]
