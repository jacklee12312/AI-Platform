from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import GatewayError


@dataclass(frozen=True)
class ModelEntry:
    id: str
    capability: str
    backend: str
    base_url: str | None
    upstream_model: str | None
    enabled: bool
    guard: bool
    modes: tuple[str, ...]
    metadata: dict[str, Any]

    @property
    def health_url(self) -> str | None:
        if not self.base_url:
            return None
        return f"{self.base_url.rstrip('/')}/health"


class Registry:
    def __init__(
        self,
        path: str | Path,
        *,
        mode_path: str | Path | None = None,
        default_mode: str = "copaw",
        installation_path: str | Path | None = None,
    ):
        self.path = Path(path)
        self.mode_path = Path(mode_path) if mode_path else None
        self.default_mode = default_mode
        self.installation_path = Path(installation_path) if installation_path else None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.service_config = raw.get("services", {})
        self.models: dict[str, ModelEntry] = {}
        for item in raw.get("models", []):
            entry = ModelEntry(
                id=item["id"],
                capability=item["capability"],
                backend=item.get("backend", "pending"),
                base_url=item.get("base_url"),
                upstream_model=item.get("upstream_model"),
                enabled=bool(item.get("enabled", True)),
                guard=bool(item.get("guard", False)),
                modes=tuple(str(mode) for mode in item.get("modes", [])),
                metadata=dict(item.get("metadata", {})),
            )
            if entry.id in self.models:
                raise ValueError(f"duplicate model id: {entry.id}")
            self.models[entry.id] = entry

    def active_mode(self) -> str:
        if self.mode_path:
            try:
                value = self.mode_path.read_text(encoding="utf-8").strip()
                if value:
                    return value
            except OSError:
                pass
        return self.default_mode

    def is_active(self, entry: ModelEntry, mode: str | None = None) -> bool:
        if not entry.modes:
            return True
        return (mode or self.active_mode()) in entry.modes

    def get(self, model_id: str, capability: str | None = None) -> ModelEntry:
        entry = self.models.get(model_id)
        if not entry:
            raise GatewayError(
                404,
                f"Unknown model: {model_id}",
                "model_not_found",
                "invalid_request_error",
                "model",
            )
        entry = self._effective(entry, self._installation_state())
        if capability and entry.capability != capability:
            raise GatewayError(
                400,
                f"Model {model_id} does not support {capability}",
                "unsupported_capability",
                "invalid_request_error",
                "model",
            )
        if not entry.enabled or not entry.base_url:
            raise GatewayError(
                503,
                f"Model {model_id} is registered but not deployed",
                "model_unavailable",
                "service_unavailable_error",
                "model",
            )
        if not self.is_active(entry):
            raise GatewayError(
                503,
                f"Model {model_id} is not active in mode {self.active_mode()}",
                "model_inactive",
                "service_unavailable_error",
                "model",
                details={"active_mode": self.active_mode(), "model_modes": list(entry.modes)},
            )
        return entry

    def entries(self) -> list[ModelEntry]:
        state = self._installation_state()
        return [self._effective(entry, state) for entry in self.models.values()]

    def _installation_state(self):
        if self.installation_path is None:
            return None
        try:
            raw = json.loads(self.installation_path.read_text(encoding='utf-8'))
            if raw.get('schema_version') != 1 or not isinstance(raw.get('models'), dict):
                return {}
            return raw['models']
        except FileNotFoundError:
            return None  # Existing full deployments retain their registration policy.
        except (OSError, ValueError, AttributeError):
            return {}  # Unreadable managed state must not reopen admission.

    @staticmethod
    def _effective(entry, state):
        if state is None:
            return entry
        row = state.get(entry.id, {})
        ready = isinstance(row, dict) and row.get('installed') is True and row.get('enabled') is True
        return replace(entry, enabled=entry.enabled and ready)
