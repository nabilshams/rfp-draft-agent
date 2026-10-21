"""azd post-provision hook: warn loudly when the deployment is open to anyone on the internet.

azd passes the environment values to hooks as environment variables. This never fails the deployment; it only
prints a warning (the app logs one at startup too) so it can't be missed in the ``azd up`` output.
"""

from __future__ import annotations

import os
import sys


def exposure_warning(env: dict[str, str]) -> str | None:
    """A warning if requests are neither authenticated nor restricted by IP range, else ``None``."""
    auth_mode = (env.get("AUTH_MODE") or "disabled").strip().lower()
    ip_ranges = (env.get("ALLOWED_IP_RANGES") or "").strip()
    url = env.get("SERVICE_API_ENDPOINT_URL") or "the app URL"

    if auth_mode != "disabled" or ip_ranges:
        return None

    return "\n".join(
        [
            "",
            "!" * 72,
            "WARNING: this deployment has NO AUTHENTICATION and is open to the internet.",
            f"  Anyone who finds {url} can read and change its data.",
            "  Before loading real RFPs or knowledge-base documents, do one of:",
            "    python scripts/setup_auth.py && azd provision   # sign-in with Entra ID (recommended)",
            "    azd env set ALLOWED_IP_RANGES <cidr[,cidr]> && azd provision   # restrict by IP",
            "!" * 72,
            "",
        ]
    )


def main() -> int:
    message = exposure_warning(dict(os.environ))
    if message:
        print(message, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
