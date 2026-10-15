"""Recommend local or Ray execution without exposing scheduler complexity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ExecutionRecommendation:
    recommended: str
    allowed: tuple[str, ...]
    reason: str


class ExecutionPolicy:
    def __init__(self, ray_bytes_threshold: int = 2 * 1024**3, ray_file_threshold: int = 1000):
        self.ray_bytes_threshold = ray_bytes_threshold
        self.ray_file_threshold = ray_file_threshold

    def recommend(
        self,
        *,
        operator_names: Iterable[str],
        total_bytes: int,
        file_count: int,
    ) -> ExecutionRecommendation:
        names = tuple(str(name).strip().lower() for name in operator_names if str(name).strip())
        if any(name.startswith("ray_") for name in names):
            return ExecutionRecommendation("ray", ("ray",), "方案包含只能由 Ray 执行的分布式算子。")
        if any("api" in name for name in names):
            return ExecutionRecommendation(
                "local",
                ("local", "ray"),
                "方案包含 API 算子，默认使用本机以控制 API 并发和限流。",
            )
        if total_bytes >= self.ray_bytes_threshold or file_count >= self.ray_file_threshold:
            return ExecutionRecommendation(
                "ray",
                ("local", "ray"),
                "数据规模或文件数量较大，推荐 Ray 并行处理。",
            )
        return ExecutionRecommendation(
            "local",
            ("local", "ray"),
            "当前数据规模较小，本机启动更快。",
        )
