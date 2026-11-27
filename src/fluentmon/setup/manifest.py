from __future__ import annotations

from pathlib import Path

import orjson

from fluentmon.setup.backend import Backend


class ManifestError(ValueError):
    pass


class Manifest:
    def __init__(self, path: Path, backend: Backend) -> None:
        self._path = path
        self._backend = backend
        self._steps: list[dict] = []

    def load(self) -> None:
        data = self._backend.read_bytes(self._path)
        if data is None:
            self._steps = []
            return
        try:
            doc = orjson.loads(data)
        except orjson.JSONDecodeError as exc:
            raise ManifestError(f"{self._path} is not valid JSON: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("schema") != 1:
            raise ManifestError(f"{self._path} has an unsupported schema")
        steps = doc.get("steps")
        if not isinstance(steps, list) or not all(isinstance(e, dict) and "id" in e for e in steps):
            raise ManifestError(f"{self._path} has a malformed steps list")
        self._steps = steps

    def get(self, step_id: str) -> dict | None:
        for entry in self._steps:
            if entry["id"] == step_id:
                return entry
        return None

    def record(self, step_id: str, **fields: object) -> None:
        entry: dict = {"id": step_id, "done": True, "at": self._backend.now().isoformat()}
        entry.update(fields)
        normalized = orjson.loads(orjson.dumps(entry, default=str))
        for index, existing in enumerate(self._steps):
            if existing["id"] == step_id:
                self._steps[index] = normalized
                break
        else:
            self._steps.append(normalized)
        self._save()

    def remove(self, step_id: str) -> None:
        self._steps = [entry for entry in self._steps if entry["id"] != step_id]
        self._save()

    def entries(self) -> list[dict]:
        return list(self._steps)

    def _save(self) -> None:
        self._backend.mkdir(self._path.parent)
        data = orjson.dumps({"schema": 1, "steps": self._steps}, option=orjson.OPT_INDENT_2)
        self._backend.write_bytes(self._path, data)
