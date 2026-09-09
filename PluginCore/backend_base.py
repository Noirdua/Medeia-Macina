"""Plugin-backed configured backend base types.

Concrete storage backends live with their owning plugins and are resolved
through PluginCore.backend_registry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class BackendBase(ABC):

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        """Return configuration schema for this backend.

        Returns a list of dicts:
        {
            "key": "PATH",
            "label": "Store Location",
            "group": "Storage",
            "type": "path",  # text|boolean|integer|float|path|secret|multiline
            "default": "",
            "required": True,
            "choices": ["/mnt/media", "/srv/data"],
            "placeholder": "/srv/data"
        }
        """
        return []

    @property
    def is_remote(self) -> bool:
        """True if this backend is a remote service rather than local disk."""
        return False

    @property
    def prefer_defer_tags(self) -> bool:
        """True if this backend prefers tags to be applied after add_file()."""
        return False

    @property
    def supports_url_association(self) -> bool:
        """True when this backend supports associating URLs to files."""
        return False

    def file_url(self, file_hash: str, **kwargs: Any) -> Optional[str]:
        """Return a fetch URL for ``file_hash``, or None if this backend has none."""
        return None

    @property
    def supports_note_association(self) -> bool:
        """True when this backend supports per-file named notes."""
        return False

    @property
    def supports_relationship_association(self) -> bool:
        """True when this backend supports file relationship links."""
        return False

    @abstractmethod
    def add_file(self, file_path: Path, **kwargs: Any) -> str:
        raise NotImplementedError

    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    def search(self, query: str, **kwargs: Any) -> list[Dict[str, Any]]:
        raise NotImplementedError(f"{self.name()} backend does not support searching")

    @abstractmethod
    def get_file(self, file_hash: str, **kwargs: Any) -> Path | str | None:
        raise NotImplementedError

    @abstractmethod
    def get_metadata(self, file_hash: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_tag(self, file_identifier: str, **kwargs: Any) -> Tuple[List[str], str]:
        raise NotImplementedError

    @abstractmethod
    def add_tag(self, file_identifier: str, tags: List[str], **kwargs: Any) -> bool:
        raise NotImplementedError

    @abstractmethod
    def delete_tag(self, file_identifier: str, tags: List[str], **kwargs: Any) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_url(self, file_identifier: str, **kwargs: Any) -> List[str]:
        raise NotImplementedError

    @abstractmethod
    def add_url(self, file_identifier: str, url: List[str], **kwargs: Any) -> bool:
        raise NotImplementedError

    def add_url_bulk(self, items: List[Tuple[str, List[str]]], **kwargs: Any) -> bool:
        """Optional bulk url association.

        Backends may override this to batch writes (single transaction / request).
        Default behavior is to call add_url() per file.
        """
        changed_any = False
        for file_identifier, urls in items or []:
            try:
                ok = self.add_url(file_identifier, urls, **kwargs)
                changed_any = changed_any or bool(ok)
            except Exception:
                continue
        return changed_any

    def delete_url_bulk(
        self,
        items: List[Tuple[str, List[str]]],
        **kwargs: Any,
    ) -> bool:
        """Optional bulk url deletion.

        Backends may override this to batch writes (single transaction / request).
        Default behavior is to call delete_url() per file.
        """
        changed_any = False
        for file_identifier, urls in items or []:
            try:
                ok = self.delete_url(file_identifier, urls, **kwargs)
                changed_any = changed_any or bool(ok)
            except Exception:
                continue
        return changed_any

    def set_note_bulk(self, items: List[Tuple[str, str, str]], **kwargs: Any) -> bool:
        """Optional bulk note set.

        Backends may override this to batch writes (single transaction / request).
        Default behavior is to call set_note() per file.
        """
        changed_any = False
        for file_identifier, name, text in items or []:
            try:
                ok = self.set_note(file_identifier, name, text, **kwargs)
                changed_any = changed_any or bool(ok)
            except Exception:
                continue
        return changed_any

    @abstractmethod
    def delete_url(self, file_identifier: str, url: List[str], **kwargs: Any) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_note(self, file_identifier: str, **kwargs: Any) -> Dict[str, str]:
        """Get notes for a file.

        Returns a mapping of note name/key -> note text.
        """
        raise NotImplementedError

    @abstractmethod
    def set_note(
        self,
        file_identifier: str,
        name: str,
        text: str,
        **kwargs: Any,
    ) -> bool:
        """Add or replace a named note for a file."""
        raise NotImplementedError

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any,
    ) -> bool:
        """Optional hook for handling `@N` selection semantics.

        Return True if the selection was handled and default behavior should be skipped.
        """
        _ = selected_items
        _ = ctx
        _ = stage_is_last
        return False

    @abstractmethod
    def delete_note(self, file_identifier: str, name: str, **kwargs: Any) -> bool:
        """Delete a named note for a file."""
        raise NotImplementedError