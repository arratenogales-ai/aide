"""Aide startup: checks, services and clean shutdown."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from collections.abc import Callable

from dotenv import dotenv_values

from aide.common import api_key
from aide.common.config import Config, ConfigError
from aide.common.environment import EnvironmentReport, clean_environment
from aide.common.logs import configure_log, log_secret
from aide.common.paths import Paths, default_paths

log = logging.getLogger("aide")


def prepare(
    paths: Paths | None = None, models_file: str | None = None, console: bool = True
) -> tuple[Config, EnvironmentReport]:
    """Loads the configuration and cleans the environment before any SDK session."""
    paths = paths or default_paths()
    paths.ensure()
    config = Config.load(paths, models_file)
    in_env_file = bool(
        paths.env.exists()
        and (dotenv_values(paths.env).get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    )
    report = clean_environment(
        config.real_browser,
        long_token_in_env_file=in_env_file,
        api_key=config.uses_api_key,
    )

    secrets = [
        config.environment.TELEGRAM_BOT_TOKEN,
        config.environment.CLAUDE_CODE_OAUTH_TOKEN,
        config.environment.ANTHROPIC_API_KEY,
    ]
    for secret in secrets:
        if secret is not None:
            log_secret(secret.get_secret_value())
    if config.uses_api_key:
        save_received_key(config, report)
    configure_log(paths.logs, console=console)

    if report.removed_credentials or report.other_removed_sources:
        log.warning(
            "credentials removed from the environment: %s",
            ", ".join(report.removed_credentials + report.other_removed_sources),
        )
    if report.inherited_removed:
        log.info("inherited variables removed: %s", ", ".join(report.inherited_removed))
    for notice in report.notices():
        log.warning(notice)
    return config, report


def save_received_key(config: Config, report: EnvironmentReport) -> None:
    """With the API key: if it arrived through ANTHROPIC_API_KEY (the environment or .env, as
    in the Agent SDK quickstart), it is saved in the keychain, which is where `apiKeyHelper`
    asks for it. It is no longer in the environment, so the agent's sessions do not see it."""
    received = report.collected_key
    if received is None and config.environment.ANTHROPIC_API_KEY is not None:
        received = config.environment.ANTHROPIC_API_KEY.get_secret_value().strip()
        report.key_in_env_file = True
    if received:
        log_secret(received)
        if api_key.read() != received:
            api_key.save(received)
    saved = api_key.read()
    if saved:
        log_secret(saved)


def request_key(entry: Callable[[str], str] = getpass.getpass) -> int:
    """`aide --set-api-key`: saves the API key in the keychain without it being shown."""
    value = entry("Paste your Claude Console API key (it won't be shown): ").strip()
    if not api_key.valid_shape(value):
        print("That doesn't look like a Claude Console API key (sk-ant-...). Nothing saved.")
        return 1
    api_key.save(value)
    print("Saved in the macOS keychain. Restart Aide to use it.")
    return 0


async def check(config: Config, report: EnvironmentReport) -> int:
    """--check mode: environment cleanup and authentication check."""
    from aide.core.authentication import check_authentication

    for notice in report.notices():
        print(f"WARNING: {notice}")
    result = await check_authentication(config)
    print(result.text())
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> None:
    """Command line entry point of the agent, with options to check the environment."""
    parser = argparse.ArgumentParser(prog="aide", description="Your personal agent")
    parser.add_argument(
        "--check",
        dest="check",
        action="store_true",
        help="only check the environment and the authentication",
    )
    parser.add_argument(
        "--models",
        dest="models",
        default=None,
        help="alternative models file in config/",
    )
    parser.add_argument(
        "--set-api-key",
        dest="save_key",
        action="store_true",
        help="save your Claude Console API key in the macOS keychain",
    )
    args = parser.parse_args(argv)
    if args.save_key:
        sys.exit(request_key())
    try:
        config, report = prepare(models_file=args.models)
    except ConfigError as e:
        print(f"Invalid configuration: {e}", file=sys.stderr)
        sys.exit(2)

    if args.check:
        sys.exit(asyncio.run(check(config, report)))

    from aide.application import run

    sys.exit(asyncio.run(run(config, report)))


if __name__ == "__main__":
    main()
