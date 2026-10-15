"""Build a bounded, browser-friendly manifest from task result files."""

from __future__ import annotations

import csv
import json
import math
import mimetypes
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from agent_web.workspaces import WorkspaceManager


class ResultPresenter:
    MAX_FILES = 500
    MAX_RECORDS = 100
    MAX_CELL_CHARS = 2_000
    MEDIA_SUFFIXES = {
        "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"},
        "video": {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"},
        "audio": {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"},
    }

    def __init__(self, workspaces: WorkspaceManager):
        self.workspaces = workspaces

    def build(self, owner_id: str, task: dict[str, Any]) -> dict[str, Any]:
        workspace = self.workspaces.ensure_user_workspace(owner_id)
        raw_path = str(task.get("result_relative_path") or "")
        if not raw_path:
            return self._empty(task, "任务没有结果文件")
        root = Path(raw_path).resolve()
        if not root.is_relative_to(workspace) or not root.exists():
            return self._empty(task, "结果文件不存在或不在当前用户工作区")

        files = self._files(root, workspace)
        records: list[dict[str, Any]] = []
        source = root if root.is_file() else self._tabular_candidate(root)
        if source:
            records = self._read_records(source)
        enriched = [self._enrich_record(record, source.parent if source else root, workspace) for record in records]
        numeric, categorical = self._statistics(records)
        direct_media = [item for item in files if item["kind"] in {"image", "video", "audio"}]
        return {
            "task_id": task["id"],
            "name": task.get("name") or "处理结果",
            "available": True,
            "message": "",
            "root": self._relative(root, workspace),
            "file_count": len(files),
            "total_bytes": sum(int(item["size_bytes"]) for item in files),
            "files": files[: self.MAX_FILES],
            "direct_media": direct_media[:48],
            "records": enriched[: self.MAX_RECORDS],
            "record_count_sampled": len(records),
            "numeric_stats": numeric,
            "categorical_stats": categorical,
            "truncated": len(files) >= self.MAX_FILES or len(records) >= self.MAX_RECORDS,
        }

    def resolve_file(self, owner_id: str, relative_path: str) -> Path:
        path = self.workspaces.resolve_user_path(owner_id, relative_path)
        if not path.is_file():
            raise ValueError("结果文件不存在")
        return path

    @staticmethod
    def _empty(task: dict[str, Any], message: str) -> dict[str, Any]:
        return {
            "task_id": task["id"], "name": task.get("name") or "处理结果",
            "available": False, "message": message, "files": [], "records": [],
            "direct_media": [], "numeric_stats": [], "categorical_stats": [],
        }

    def _files(self, root: Path, workspace: Path) -> list[dict[str, Any]]:
        candidates: Iterable[Path] = [root] if root.is_file() else root.rglob("*")
        result = []
        for path in candidates:
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            result.append({
                "name": path.name,
                "path": self._relative(path, workspace),
                "size_bytes": stat.st_size,
                "kind": self._kind(path),
                "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            })
            if len(result) >= self.MAX_FILES:
                break
        return result

    @staticmethod
    def _tabular_candidate(root: Path) -> Path | None:
        priority = {".jsonl": 0, ".json": 1, ".csv": 2, ".tsv": 3}
        candidates = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in priority]
        return min(candidates, key=lambda path: (priority[path.suffix.lower()], path.name)) if candidates else None

    def _read_records(self, path: Path) -> list[dict[str, Any]]:
        suffix = path.suffix.lower()
        try:
            if suffix == ".jsonl":
                result = []
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if len(result) >= self.MAX_RECORDS:
                            break
                        try:
                            value = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        result.append(value if isinstance(value, dict) else {"value": value})
                return result
            if suffix == ".json":
                value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(value, list):
                    return [item if isinstance(item, dict) else {"value": item} for item in value[: self.MAX_RECORDS]]
                return [value] if isinstance(value, dict) else [{"value": value}]
            if suffix in {".csv", ".tsv"}:
                with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
                    reader = csv.DictReader(handle, delimiter="\t" if suffix == ".tsv" else ",")
                    return [dict(row) for _, row in zip(range(self.MAX_RECORDS), reader)]
            if path.stat().st_size <= 1024 * 1024:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                try:
                    return [{"value": float(text)}]
                except ValueError:
                    return [{"text": text[: self.MAX_CELL_CHARS]}] if text else []
        except (OSError, ValueError, json.JSONDecodeError, csv.Error):
            return []
        return []

    def _enrich_record(self, record: dict[str, Any], base: Path, workspace: Path) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        media = []
        for key, value in record.items():
            clean[str(key)] = self._safe_value(value)
            values = value if isinstance(value, list) else [value]
            for candidate in values:
                if not isinstance(candidate, str):
                    continue
                resolved = self._resolve_media(candidate, base, workspace)
                if resolved:
                    media.append({
                        "field": str(key), "kind": self._kind(resolved),
                        "path": self._relative(resolved, workspace), "name": resolved.name,
                    })
        return {"fields": clean, "media": media}

    def _resolve_media(self, value: str, base: Path, workspace: Path) -> Path | None:
        if value.startswith(("http://", "https://", "data:")):
            return None
        candidate = Path(value).expanduser()
        candidate = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
        if candidate.is_file() and candidate.is_relative_to(workspace) and self._kind(candidate) in {"image", "video", "audio"}:
            return candidate
        return None

    def _statistics(self, records: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
        numbers: dict[str, list[float]] = defaultdict(list)
        categories: dict[str, Counter[str]] = defaultdict(Counter)
        for record in records:
            for key, value in record.items():
                if isinstance(value, bool) or value is None:
                    continue
                number = self._number(value)
                if number is not None:
                    numbers[str(key)].append(number)
                elif isinstance(value, str) and len(value) <= 120:
                    categories[str(key)][value] += 1
        numeric = []
        for field, values in list(numbers.items())[:12]:
            numeric.append({
                "field": field, "count": len(values), "min": min(values), "max": max(values),
                "mean": sum(values) / len(values), "bins": self._histogram(values),
            })
        categorical = []
        for field, counts in list(categories.items())[:12]:
            if len(counts) <= 20:
                categorical.append({"field": field, "values": [
                    {"label": label, "count": count} for label, count in counts.most_common(8)
                ]})
        return numeric, categorical

    @staticmethod
    def _histogram(values: list[float], bin_count: int = 8) -> list[dict[str, Any]]:
        low, high = min(values), max(values)
        if math.isclose(low, high):
            return [{"from": low, "to": high, "count": len(values)}]
        width = (high - low) / bin_count
        counts = [0] * bin_count
        for value in values:
            index = min(bin_count - 1, int((value - low) / width))
            counts[index] += 1
        return [
            {"from": low + index * width, "to": low + (index + 1) * width, "count": count}
            for index, count in enumerate(counts)
        ]

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
        if isinstance(value, str):
            try:
                number = float(value.strip())
                return number if math.isfinite(number) else None
            except ValueError:
                return None
        return None

    def _safe_value(self, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[: self.MAX_CELL_CHARS]
        if isinstance(value, list):
            return [self._safe_value(item) for item in value[:30]]
        if isinstance(value, dict):
            return {str(key): self._safe_value(item) for key, item in list(value.items())[:30]}
        return str(value)[: self.MAX_CELL_CHARS]

    def _kind(self, path: Path) -> str:
        suffix = path.suffix.lower()
        for kind, suffixes in self.MEDIA_SUFFIXES.items():
            if suffix in suffixes:
                return kind
        if suffix in {".json", ".jsonl", ".csv", ".tsv", ".parquet"}:
            return "table"
        if suffix in {".txt", ".md", ".log", ".yaml", ".yml"}:
            return "text"
        return "file"

    @staticmethod
    def _relative(path: Path, workspace: Path) -> str:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
