"""Create (or update) the Entra ID app registration used by Container Apps authentication.

Run after the first ``azd provision`` (the redirect URI needs the app's URL), then provision again
to switch the deployment to ``AUTH_MODE=easyauth``::

    azd provision                      # creates the app; auth is disabled by default
    python scripts/setup_auth.py       # app registration + azd environment values
    azd provision                      # turns Easy Auth on

The script is safe to re-run: it reuses the registration, refreshes the redirect URI, and only
creates a new client secret when none is stored in the azd environment (or with --rotate-secret).
Use --dry-run to see what would be changed without changing anything.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable

CALLBACK_PATH = "/.auth/login/aad/callback"
SECRET_NAME = "azd-easyauth"


class SetupError(RuntimeError):
    pass


class Cli:
    """Runs az/azd commands. In dry-run mode only read-only commands are executed."""

    def __init__(self, dry_run: bool, echo: Callable[[str], None] = print) -> None:
        self.dry_run = dry_run
        self.echo = echo

    def _run(self, args: list[str]) -> str:
        exe = shutil.which(args[0])
        if exe is None:
            raise SetupError(f"'{args[0]}' was not found on PATH")
        result = subprocess.run([exe, *args[1:]], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise SetupError(f"'{' '.join(args[:3])}' failed: {result.stderr.strip() or result.stdout.strip()}")
        return result.stdout.strip()

    def read(self, args: list[str]) -> str:
        return self._run(args)

    def write(self, args: list[str], *, secret_index: int | None = None, dry_run_result: str = "") -> str:
        shown = [("***" if i == secret_index else a) for i, a in enumerate(args)]
        if self.dry_run:
            self.echo(f"[dry-run] {' '.join(shown)}")
            return dry_run_result
        self.echo(f"> {' '.join(shown)}")
        return self._run(args)


def parse_env_values(text: str) -> dict[str, str]:
    """Parse the KEY="value" lines printed by ``azd env get-values``."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            values[key.strip()] = value.strip().strip('"')
    return values


def setup(cli: Cli, *, env: str | None = None, rotate_secret: bool = False) -> None:
    env_flag = ["-e", env] if env else []
    values = parse_env_values(cli.read(["azd", "env", "get-values", *env_flag]))

    endpoint = values.get("SERVICE_API_ENDPOINT_URL", "")
    if not endpoint:
        raise SetupError("SERVICE_API_ENDPOINT_URL is not set in the azd environment. Run 'azd provision' first.")
    tenant_id = values.get("AZURE_TENANT_ID", "")
    display_name = f"rfp-draft-agent-{values.get('AZURE_ENV_NAME', env or 'default')}"
    redirect_uri = endpoint.rstrip("/") + CALLBACK_PATH

    app_id = cli.read(
        ["az", "ad", "app", "list", "--display-name", display_name,
         "--query", f"[?displayName=='{display_name}'] | [0].appId", "-o", "tsv"]
    )
    if app_id:
        cli.echo(f"Using existing app registration '{display_name}' ({app_id})")
        cli.write(["az", "ad", "app", "update", "--id", app_id, "--web-redirect-uris", redirect_uri,
                   "--enable-id-token-issuance", "true"])
    else:
        cli.echo(f"Creating app registration '{display_name}'")
        app_id = cli.write(
            ["az", "ad", "app", "create", "--display-name", display_name, "--sign-in-audience", "AzureADMyOrg",
             "--web-redirect-uris", redirect_uri, "--enable-id-token-issuance", "true",
             "--query", "appId", "-o", "tsv"],
            dry_run_result="<new-app-id>",
        )

    # The service principal is what lets users in the tenant sign in to the app.
    if cli.dry_run:
        cli.echo("[dry-run] az ad sp create --id <app-id>   (only if the service principal does not exist yet)")
    else:
        try:
            cli.read(["az", "ad", "sp", "show", "--id", app_id])
        except SetupError:
            cli.write(["az", "ad", "sp", "create", "--id", app_id])

    secret = values.get("ENTRA_CLIENT_SECRET", "")
    if rotate_secret or not secret or values.get("ENTRA_CLIENT_ID") != app_id:
        secret = cli.write(
            ["az", "ad", "app", "credential", "reset", "--id", app_id, "--append", "--display-name", SECRET_NAME,
             "--years", "1", "--query", "password", "-o", "tsv"],
            dry_run_result="<new-secret>",
        )
    else:
        cli.echo("Keeping the existing client secret (use --rotate-secret to create a new one)")

    settings = [("ENTRA_CLIENT_ID", app_id), ("ENTRA_TENANT_ID", tenant_id), ("AUTH_MODE", "easyauth")]
    for name, value in settings:
        if value:
            cli.write(["azd", "env", "set", name, value, *env_flag])
    cli.write(["azd", "env", "set", "ENTRA_CLIENT_SECRET", secret, *env_flag], secret_index=4)

    cli.echo("")
    cli.echo(f"Sign-in redirect URI: {redirect_uri}")
    cli.echo("Done. Run 'azd provision' to turn on Easy Auth.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", help="azd environment name (defaults to the current one)")
    parser.add_argument("--rotate-secret", action="store_true", help="create a new client secret even if one exists")
    parser.add_argument("--dry-run", action="store_true", help="show the changes without making them")
    args = parser.parse_args(argv)
    try:
        setup(Cli(args.dry_run), env=args.env, rotate_secret=args.rotate_secret)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
