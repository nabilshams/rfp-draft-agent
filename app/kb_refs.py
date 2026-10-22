"""Opaque references to knowledge-base documents.

A document's blob URL contains its folder path (``client/project/area/file``), which can name a customer, and it
does not open for users anyway (the storage account is private). Retrieval therefore hands the model, and the user,
a reference instead: ``/kb/doc/<ref>``. The reference is a fixed-length hash of the blob name, so it reveals
nothing; the app resolves it back to the blob and checks the caller's access before serving the document.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Iterable

REF_PATTERN = r"^[0-9a-f]{24}$"
_REF = re.compile(REF_PATTERN)
_PATH_PREFIX = "/kb/doc/"


def doc_ref(blob_name: str) -> str:
    return hashlib.sha256(blob_name.encode("utf-8")).hexdigest()[:24]


def doc_path(blob_name: str) -> str:
    """The reference link that stands for a knowledge-base blob in prompts, answers and evidence."""
    return f"{_PATH_PREFIX}{doc_ref(blob_name)}"


def is_ref(value: str) -> bool:
    return bool(_REF.match(value))


def blob_name_of_url(source_url: str, storage_account_url: str, kb_container: str) -> str | None:
    """The blob name inside one of this app's knowledge-base blob URLs, or ``None`` for any other URL."""
    prefix = f"{storage_account_url}/{kb_container}/"
    return source_url[len(prefix) :] if source_url.startswith(prefix) and len(source_url) > len(prefix) else None


def opaque_url(source_url: str, storage_account_url: str, kb_container: str) -> str:
    """*source_url* with a knowledge-base blob URL replaced by its reference link; other URLs are unchanged."""
    blob_name = blob_name_of_url(source_url, storage_account_url, kb_container)
    return doc_path(blob_name) if blob_name else source_url


class RefIndex:
    """Finds the blob behind a reference.

    The names of the knowledge-base blobs are listed when a reference isn't known yet (a new document, or a restart)
    and remembered. A name is only ever used after the caller's access to that blob has been checked.
    """

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._lock = threading.Lock()

    def resolve(self, ref: str, list_names: Callable[[], Iterable[str]]) -> str | None:
        with self._lock:
            known = self._names.get(ref)
        if known:
            return known
        names = {doc_ref(name): name for name in list_names()}
        with self._lock:
            self._names = names
        return names.get(ref)

    def forget(self) -> None:
        with self._lock:
            self._names = {}


refs = RefIndex()
