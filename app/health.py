"""Checks that the app can reach the Azure services it depends on, without revealing any detail.

Used at startup (the results are logged, so a misconfigured network or missing role is visible in the container
logs straight away) and by ``GET /health/dependencies``. Each check reports one of:

* ``ok``           – the call worked
* ``refused``      – the service answered 401/403: a missing role, a firewall or a network policy
* ``unreachable``  – no answer (DNS, private endpoint or firewall problem)
* ``error``        – anything else
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ServiceRequestError

from app import azure_clients
from app.config import Settings

logger = logging.getLogger(__name__)

OK, REFUSED, UNREACHABLE, ERROR = "ok", "refused", "unreachable", "error"


def classify(exc: BaseException) -> str:
    """Map an Azure SDK exception to one of the outcomes above."""
    if isinstance(exc, ClientAuthenticationError):
        return REFUSED
    if isinstance(exc, HttpResponseError):
        return REFUSED if exc.status_code in (401, 403) else ERROR
    if isinstance(exc, ServiceRequestError):
        return UNREACHABLE
    return ERROR


def _storage(settings: Settings) -> None:
    azure_clients.container(settings, settings.storage_rfp_container).get_container_properties()


def _search(settings: Settings) -> None:
    next(iter(azure_clients.search_index_client(settings).list_index_names()), None)


def _agents(settings: Settings) -> None:
    next(iter(azure_clients.agents_client(settings).list_agents(limit=1)), None)


def _ledger(settings: Settings) -> None:
    from app.ledger.store import store_for

    store = store_for(settings)
    if store is not None:
        store.ping()


CHECKS: dict[str, Callable[[Settings], None]] = {"storage": _storage, "search": _search, "agents": _agents}


def _configured_checks(settings: Settings) -> dict[str, Callable[[Settings], None]]:
    """The checks that apply: the ledger is only checked where it is configured."""
    checks = dict(CHECKS)
    if settings.ledger_backend or settings.cosmos_endpoint:
        checks["ledger"] = _ledger
    return checks


def check_dependencies(settings: Settings) -> dict[str, str]:
    """Run every check; never raises. Blocking: call from a worker thread."""
    results: dict[str, str] = {}
    for name, check in _configured_checks(settings).items():
        try:
            check(settings)
            results[name] = OK
        except Exception as exc:
            results[name] = classify(exc)
            logger.warning("Dependency check '%s' failed (%s): %s: %s", name, results[name], type(exc).__name__, exc)
    return results
