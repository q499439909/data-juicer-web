from __future__ import annotations

from typing import Protocol


class DatasetProvider(Protocol):
    @property
    def available(self) -> bool: ...

    def list_for_user(self, user: dict) -> list[dict]: ...


class UnavailableDatasetProvider:
    @property
    def available(self) -> bool:
        return False

    def list_for_user(self, user: dict) -> list[dict]:
        return []
