from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from loguru import logger

from bot.adapter_overrides import (
    install_runtime_compatibility_overrides,
    verify_runtime_compatibility_targets,
)


@dataclass(frozen=True)
class CompatibilityPatchSpec:
    name: str
    intent: str
    target_family: str
    implementation: str


def build_patch_manifest(_project_root: Path) -> List[CompatibilityPatchSpec]:
    return [
        CompatibilityPatchSpec(
            name="drop-log-throttle",
            intent="Reduce noisy Polymarket quote-drop warnings with throttled runtime logging.",
            target_family="nautilus.polymarket.logging",
            implementation="runtime_override",
        ),
        CompatibilityPatchSpec(
            name="ticksize-log-throttle",
            intent="Reduce noisy Polymarket tick-size warnings with compact runtime logging.",
            target_family="nautilus.polymarket.logging",
            implementation="runtime_override",
        ),
        CompatibilityPatchSpec(
            name="trade-log-compact",
            intent="Reduce noisy Polymarket trade logs with compact runtime summaries.",
            target_family="nautilus.polymarket.execution",
            implementation="runtime_override",
        ),
        CompatibilityPatchSpec(
            name="execution-compat",
            intent="Avoid duplicate cancellation events and noisy cancel-without-venue-id warnings.",
            target_family="nautilus.polymarket.execution",
            implementation="runtime_override",
        ),
        CompatibilityPatchSpec(
            name="user-trade-decimal-fallback",
            intent="Fallback to trade-level numeric fields when Polymarket maker trade fields are empty.",
            target_family="nautilus.polymarket.schemas.user",
            implementation="runtime_override",
        ),
        CompatibilityPatchSpec(
            name="py-clob-http-fallback",
            intent="Use HTTP/1.1 fallback retry for py-clob helper request transport errors.",
            target_family="py_clob_client_v2.http_helpers",
            implementation="runtime_override",
        ),
    ]


def apply_compatibility_patches(*, project_root: Path, enabled: bool, mode: str = "runtime") -> None:
    """
    Install or verify local compatibility overrides without rewriting site-packages.

    mode:
    - off: no-op
    - verify: verify import/install targets exist
    - runtime/apply: install runtime overrides
    """
    normalized_mode = (mode or "runtime").strip().lower()
    if not enabled or normalized_mode == "off":
        return

    manifest = build_patch_manifest(project_root)
    for spec in manifest:
        logger.debug(
            "Compatibility patch manifest: "
            f"name={spec.name} target={spec.target_family} impl={spec.implementation} intent={spec.intent}"
        )

    if normalized_mode == "verify":
        for line in verify_runtime_compatibility_targets(project_root):
            logger.info(f"Compatibility runtime target: {line}")
        logger.info(f"Compatibility runtime manifest verified: {len(manifest)} targets")
        return

    install_runtime_compatibility_overrides()
    logger.info(f"Compatibility runtime overrides installed: {len(manifest)} targets")
