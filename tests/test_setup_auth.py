"""Tests for scripts/setup_auth.py using scripted az/azd output (nothing touches a real tenant)."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "setup_auth", Path(__file__).resolve().parents[1] / "scripts" / "setup_auth.py"
)
setup_auth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup_auth)

ENDPOINT = "https://app.example.azurecontainerapps.io"
CALLBACK = f"{ENDPOINT}/.auth/login/aad/callback"
BASE_ENV = (
    'AZURE_ENV_NAME="demo"\n'
    'AZURE_TENANT_ID="tenant-1"\n'
    f'SERVICE_API_ENDPOINT_URL="{ENDPOINT}"\n'
)


class FakeCli(setup_auth.Cli):
    """Records every command that would be executed and answers from a script."""

    def __init__(self, *, env_text=BASE_ENV, existing_app="", sp_exists=True, dry_run=False, no_signed_in_user=False):
        self.messages: list[str] = []
        super().__init__(dry_run, echo=self.messages.append)
        self.env_text = env_text
        self.existing_app = existing_app
        self.sp_exists = sp_exists
        self.no_signed_in_user = no_signed_in_user
        self.executed: list[list[str]] = []

    def _run(self, args):
        self.executed.append(args)
        if args[:3] == ["azd", "env", "get-values"]:
            return self.env_text
        if args[:4] == ["az", "ad", "app", "list"]:
            return self.existing_app
        if args[:4] == ["az", "ad", "app", "create"]:
            return "new-app-id"
        if args[:4] == ["az", "ad", "sp", "show"]:
            if self.sp_exists:
                return "{}"
            raise setup_auth.SetupError("service principal not found")
        if args[:5] == ["az", "ad", "app", "credential", "reset"]:
            return "generated-secret"
        if args[:4] == ["az", "ad", "signed-in-user", "show"]:
            if self.no_signed_in_user:
                raise setup_auth.SetupError("no signed-in user")
            return "me-oid"
        return ""

    def commands(self, *prefix):
        return [c for c in self.executed if c[: len(prefix)] == list(prefix)]

    def azd_settings(self):
        return {c[3]: c[4] for c in self.commands("azd", "env", "set")}


def test_parse_env_values():
    text = 'A="1"\nB=two\n\nnot a pair\nC="x=y"\n'
    assert setup_auth.parse_env_values(text) == {"A": "1", "B": "two", "C": "x=y"}


def test_new_app_is_created_with_the_callback_url_and_values_are_stored_in_azd():
    cli = FakeCli()
    setup_auth.setup(cli)

    (create,) = cli.commands("az", "ad", "app", "create")
    assert create[create.index("--display-name") + 1] == "rfp-draft-agent-demo"
    assert create[create.index("--web-redirect-uris") + 1] == CALLBACK
    assert create[create.index("--sign-in-audience") + 1] == "AzureADMyOrg"
    assert cli.azd_settings() == {
        "ENTRA_CLIENT_ID": "new-app-id",
        "ENTRA_TENANT_ID": "tenant-1",
        "AUTH_MODE": "easyauth",
        "ADMIN_OIDS": "me-oid",
        "ENTRA_CLIENT_SECRET": "generated-secret",
    }


def test_the_signed_in_user_becomes_an_administrator_unless_one_is_already_set():
    fresh = FakeCli()
    setup_auth.setup(fresh)
    assert fresh.azd_settings()["ADMIN_OIDS"] == "me-oid"

    configured = FakeCli(env_text=BASE_ENV + 'ADMIN_OIDS="someone-else"\n')
    setup_auth.setup(configured)
    assert "ADMIN_OIDS" not in configured.azd_settings()  # an existing value is left alone
    assert configured.commands("az", "ad", "signed-in-user") == []


def test_setup_continues_when_there_is_no_signed_in_user():
    cli = FakeCli(no_signed_in_user=True)
    setup_auth.setup(cli)

    assert "ADMIN_OIDS" not in cli.azd_settings()
    assert cli.azd_settings()["AUTH_MODE"] == "easyauth"
    assert any("set ADMIN_OIDS yourself" in message for message in cli.messages)


def test_service_principal_is_created_only_when_missing():
    missing = FakeCli(sp_exists=False)
    setup_auth.setup(missing)
    assert len(missing.commands("az", "ad", "sp", "create")) == 1

    present = FakeCli(sp_exists=True)
    setup_auth.setup(present)
    assert present.commands("az", "ad", "sp", "create") == []


def test_existing_app_is_reused_and_its_redirect_uri_refreshed():
    cli = FakeCli(existing_app="existing-app-id")
    setup_auth.setup(cli)

    assert cli.commands("az", "ad", "app", "create") == []
    (update,) = cli.commands("az", "ad", "app", "update")
    assert update[update.index("--id") + 1] == "existing-app-id"
    assert update[update.index("--web-redirect-uris") + 1] == CALLBACK
    assert cli.azd_settings()["ENTRA_CLIENT_ID"] == "existing-app-id"


def test_existing_secret_is_kept_on_rerun():
    env_text = BASE_ENV + 'ENTRA_CLIENT_ID="existing-app-id"\nENTRA_CLIENT_SECRET="old-secret"\n'
    cli = FakeCli(env_text=env_text, existing_app="existing-app-id")
    setup_auth.setup(cli)

    assert cli.commands("az", "ad", "app", "credential", "reset") == []
    assert cli.azd_settings()["ENTRA_CLIENT_SECRET"] == "old-secret"


def test_rotate_secret_creates_a_new_one():
    env_text = BASE_ENV + 'ENTRA_CLIENT_ID="existing-app-id"\nENTRA_CLIENT_SECRET="old-secret"\n'
    cli = FakeCli(env_text=env_text, existing_app="existing-app-id")
    setup_auth.setup(cli, rotate_secret=True)

    assert len(cli.commands("az", "ad", "app", "credential", "reset")) == 1
    assert cli.azd_settings()["ENTRA_CLIENT_SECRET"] == "generated-secret"


def test_secret_for_a_different_app_is_not_reused():
    env_text = BASE_ENV + 'ENTRA_CLIENT_ID="some-other-app"\nENTRA_CLIENT_SECRET="old-secret"\n'
    cli = FakeCli(env_text=env_text, existing_app="existing-app-id")
    setup_auth.setup(cli)

    assert cli.azd_settings()["ENTRA_CLIENT_SECRET"] == "generated-secret"


def test_requires_the_app_to_be_provisioned_first():
    cli = FakeCli(env_text='AZURE_ENV_NAME="demo"\n')
    with pytest.raises(setup_auth.SetupError, match="azd provision"):
        setup_auth.setup(cli)
    assert cli.commands("az") == []


def test_secret_is_never_printed():
    cli = FakeCli()
    setup_auth.setup(cli)

    assert not any("generated-secret" in message for message in cli.messages)
    assert any("ENTRA_CLIENT_SECRET ***" in message for message in cli.messages)


def test_dry_run_changes_nothing():
    cli = FakeCli(dry_run=True)
    setup_auth.setup(cli)

    executed = [" ".join(c[:4]) for c in cli.executed]
    assert executed == ["azd env get-values", "az ad app list", "az ad signed-in-user show"]
    assert any(message.startswith("[dry-run] az ad app create") for message in cli.messages)
    assert any("ENTRA_CLIENT_SECRET ***" in message for message in cli.messages)


def test_named_environment_is_passed_to_azd():
    cli = FakeCli()
    setup_auth.setup(cli, env="staging")

    azd_commands = cli.commands("azd")
    assert azd_commands
    assert all(c[-2:] == ["-e", "staging"] for c in azd_commands)
