"""Server-side user workspace allocation and safe archive extraction."""

from __future__ import annotations

import shutil
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


class WorkspaceManager:
    _SUBDIRS = ("uploads", "datasets", "conversations", "tasks", "outputs")

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def ensure_user_workspace(self, user_id: str) -> Path:
        safe_id = self._safe_segment(user_id)
        workspace = (self.root / safe_id).resolve()
        for name in self._SUBDIRS:
            (workspace / name).mkdir(parents=True, exist_ok=True)
        return workspace

    def resolve_user_path(self, user_id: str, relative_path: str | Path) -> Path:
        workspace = self.ensure_user_workspace(user_id)
        candidate = (workspace / str(relative_path)).resolve()
        if not candidate.is_relative_to(workspace):
            raise ValueError("Path is outside user workspace")
        return candidate

    def list_files(self, user_id: str, relative_dir: str = "") -> list[dict]:
        directory = self.resolve_user_path(user_id, relative_dir or ".")
        if not directory.exists() or not directory.is_dir():
            raise ValueError("Workspace directory does not exist")
        workspace = self.ensure_user_workspace(user_id)
        rows = []
        for path in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            stat = path.stat()
            rows.append(
                {
                    "name": path.name,
                    "path": path.relative_to(workspace).as_posix(),
                    "is_dir": path.is_dir(),
                    "size": 0 if path.is_dir() else stat.st_size,
                    "updated_at": stat.st_mtime,
                }
            )
        return rows

    def extract_archive(self, user_id: str, archive_relative_path: str) -> Path:
        archive = self.resolve_user_path(user_id, archive_relative_path)
        destination = archive.with_suffix("")
        destination.mkdir(parents=True, exist_ok=True)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as handle:
                self._validate_archive_names(handle.namelist())
                self._validate_expanded_size(
                    archive.stat().st_size, (info.file_size for info in handle.infolist())
                )
                for info in handle.infolist():
                    target = (destination / info.filename).resolve()
                    self._require_inside(destination, target)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with handle.open(info) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as handle:
                names = [member.name for member in handle.getmembers()]
                self._validate_archive_names(names)
                self._validate_expanded_size(
                    archive.stat().st_size, (member.size for member in handle.getmembers())
                )
                for member in handle.getmembers():
                    if member.issym() or member.islnk():
                        raise ValueError("Archive links are not allowed")
                    target = (destination / member.name).resolve()
                    self._require_inside(destination, target)
                handle.extractall(destination, filter="data")
        else:
            raise ValueError("Only ZIP and TAR archives can be extracted")
        return destination

    @staticmethod
    def _safe_segment(value: str) -> str:
        if not value or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in value):
            raise ValueError("Unsafe workspace identifier")
        return value

    @staticmethod
    def _validate_archive_names(names: Iterable[str]) -> None:
        for name in names:
            pure = PurePosixPath(str(name).replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError("Archive contains an unsafe path")

    @staticmethod
    def _require_inside(root: Path, target: Path) -> None:
        if not target.is_relative_to(root.resolve()):
            raise ValueError("Archive path escapes destination")

    @staticmethod
    def _validate_expanded_size(archive_size: int, member_sizes: Iterable[int]) -> None:
        expanded = sum(max(0, int(size)) for size in member_sizes)
        maximum = max(1024**3, archive_size * 200)
        if expanded > maximum:
            raise ValueError("Archive expands beyond the configured safety limit")
