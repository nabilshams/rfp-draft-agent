"""The post-provision hook that warns about open, unauthenticated deployments."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_exposure.py"
spec = importlib.util.spec_from_file_location("check_exposure", SCRIPT)
check_exposure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_exposure)


@pytest.mark.parametrize("env", [{}, {"AUTH_MODE": "disabled"}, {"AUTH_MODE": " DISABLED ", "ALLOWED_IP_RANGES": " "}])
def test_open_and_unauthenticated_deployments_get_a_warning(env):
    warning = check_exposure.exposure_warning({**env, "SERVICE_API_ENDPOINT_URL": "https://app.example"})
    assert "NO AUTHENTICATION" in warning
    assert "https://app.example" in warning
    assert "setup_auth.py" in warning and "ALLOWED_IP_RANGES" in warning


@pytest.mark.parametrize(
    "env",
    [{"AUTH_MODE": "easyauth"}, {"AUTH_MODE": "jwt"}, {"AUTH_MODE": "disabled", "ALLOWED_IP_RANGES": "203.0.113.4/32"}],
)
def test_authenticated_or_ip_restricted_deployments_get_none(env):
    assert check_exposure.exposure_warning(env) is None


def test_the_hook_never_fails_the_deployment():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env={"AUTH_MODE": "disabled"})
    assert result.returncode == 0
    assert "NO AUTHENTICATION" in result.stderr
