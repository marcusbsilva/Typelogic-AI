"""Small, explicit accelerator admission policy for the training tools.

Serving remains CPU-only. The XPU profile is an experimental training path and
always selects FP16 conservatively, even if the device advertises BF16.
"""
from __future__ import annotations

import math


def resolve_training_backend(torch, device_type: str, *, min_memory_gib: float) -> dict:
    """Validate an accelerator and return its torch module and training policy.

    ``min_memory_gib`` is an admission threshold, not proof that a full run fits.
    The caller must still run the longest-row smoke test before training.
    """
    if device_type not in ("cuda", "xpu"):
        raise ValueError(f"Unsupported training device: {device_type}")
    if isinstance(min_memory_gib, bool) or not isinstance(min_memory_gib, (int, float)) or not math.isfinite(min_memory_gib) or min_memory_gib <= 0:
        raise ValueError("min_memory_gib must be a positive finite number")

    backend = getattr(torch, device_type, None)
    if backend is None or not callable(getattr(backend, "is_available", None)) or not backend.is_available():
        raise RuntimeError(f"{device_type.upper()} is not available")
    properties = backend.get_device_properties(0)
    memory_bytes = int(properties.total_memory)
    required_bytes = int(min_memory_gib * 1024**3)
    if memory_bytes < required_bytes:
        available_gib = memory_bytes / 1024**3
        raise RuntimeError(
            f"{device_type.upper()} requires at least {min_memory_gib:g} GiB; "
            f"device reports {available_gib:.2f} GiB"
        )

    # Preserve the reviewed CUDA behavior. XPU BF16 training is deliberately
    # deferred until the Qwen3.5/Transformers kernels are validated on Intel GPU.
    bf16 = False
    if device_type == "cuda":
        check = getattr(backend, "is_bf16_supported", None)
        bf16 = bool(check(including_emulation=False)) if check else False

    return {
        "device": device_type,
        "backend": backend,
        "name": str(properties.name),
        "memory_bytes": memory_bytes,
        "memory_gib": memory_bytes / 1024**3,
        "min_memory_gib": float(min_memory_gib),
        "precision": "bf16" if bf16 else "fp16",
    }
