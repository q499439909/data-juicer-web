"""Derive conservative scheduler resource requests from Data-Juicer plans."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class ResourceRequest:
    resource_class: str
    cpus: float
    gpus: float


_GPU_HINTS = ("cuda", "gpu", "vllm", "visual", "image_caption", "video_caption")


def detect_resources(plan: dict[str, Any]) -> ResourceRequest:
    recipe = plan.get("recipe") if isinstance(plan.get("recipe"), dict) else {}
    process = recipe.get("process", []) if isinstance(recipe, dict) else []
    try:
        cpus = float(recipe.get("np", 4))
    except (TypeError, ValueError):
        cpus = 4.0
    gpus = 0.0
    for step in process if isinstance(process, list) else []:
        if not isinstance(step, dict):
            continue
        if "name" in step:
            name = str(step.get("name", ""))
            params = step.get("params", {})
        elif len(step) == 1:
            name, params = next(iter(step.items()))
            name = str(name)
        else:
            continue
        params = params if isinstance(params, dict) else {}
        raw_cpus = params.get("num_cpus", params.get("cpu_required"))
        raw_gpus = params.get("num_gpus", params.get("gpu_required"))
        try:
            if raw_cpus is not None:
                cpus = max(cpus, float(raw_cpus))
        except (TypeError, ValueError):
            pass
        try:
            if raw_gpus is not None:
                gpus = max(gpus, float(raw_gpus))
        except (TypeError, ValueError):
            pass
        if raw_gpus is None and any(hint in name.lower() for hint in _GPU_HINTS):
            gpus = max(gpus, 1.0)
    # Use whole-GPU reservations at the Web layer. Ray may subdivide a GPU
    # internally, but conservative outer reservations prevent local and Ray
    # jobs from concurrently oversubscribing the same device.
    gpus = float(math.ceil(gpus)) if gpus > 0 else 0.0
    return ResourceRequest("gpu" if gpus > 0 else "cpu", max(cpus, 0.1), gpus)
