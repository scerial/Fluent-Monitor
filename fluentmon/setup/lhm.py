from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path

from fluentmon.setup import pins
from fluentmon.setup.runner import Context, StepError

WEB_SERVER_KEYS: dict[str, str] = {
    "runWebServerMenuItem": "true",
    "listenerIp": "127.0.0.1",
    "listenerPort": "8085",
}


def _app_settings(root: ET.Element) -> ET.Element | None:
    if root.tag == "appSettings":
        return root
    return root.find("appSettings")


def apply_settings(xml: bytes | None, keys: Mapping[str, str]) -> bytes:
    if xml is None:
        root = ET.Element("configuration")
    else:
        root = ET.fromstring(xml)
    app = _app_settings(root)
    if app is None:
        app = ET.SubElement(root, "appSettings")
    for key, value in keys.items():
        element = next((e for e in app.findall("add") if e.get("key") == key), None)
        if element is None:
            ET.SubElement(app, "add", {"key": key, "value": value})
        else:
            element.set("value", value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _has_keys(xml: bytes, keys: Mapping[str, str]) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    app = _app_settings(root)
    if app is None:
        return False
    for key, value in keys.items():
        element = next((e for e in app.findall("add") if e.get("key") == key), None)
        if element is None or element.get("value") != value:
            return False
    return True


class LhmStep:
    id = "lhm"
    undoable = True

    def done(self, ctx: Context) -> bool:
        entry = ctx.manifest.get(self.id)
        return entry is not None and ctx.backend.exists(pins.INSTALL_DIR / pins.LHM_EXE)

    def run(self, ctx: Context) -> dict:
        backend = ctx.backend
        downloaded = ctx.lhm_zip is None
        if ctx.lhm_zip is not None:
            archive = ctx.lhm_zip
        else:
            archive = ctx.config_path.parent / "downloads" / "lhm.zip"
            backend.mkdir(archive.parent)
            try:
                backend.fetch(pins.LHM_URL, archive)
            except OSError as exc:
                raise StepError(f"download of {pins.LHM_URL} failed: {exc}") from exc

        data = backend.read_bytes(archive)
        if data is None and not ctx.dry_run:
            raise StepError(f"cannot read {archive}")

        digest = ""
        if data is not None:
            digest = hashlib.sha256(data).hexdigest()
            if digest != pins.LHM_SHA256:
                if downloaded:
                    backend.delete(archive)
                raise StepError(f"LHM hash mismatch: expected {pins.LHM_SHA256}, got {digest}")

        installed = not backend.exists(pins.INSTALL_DIR)
        try:
            backend.unzip(archive, pins.INSTALL_DIR)
        except Exception as exc:
            raise StepError(f"cannot unpack {archive}: {exc}") from exc

        if not ctx.dry_run and not backend.exists(pins.INSTALL_DIR / pins.LHM_EXE):
            raise StepError(
                f"{pins.LHM_EXE} is not directly inside the LHM archive; its layout is not the one expected"
            )
        return {
            "folder": str(pins.INSTALL_DIR),
            "sha256": digest,
            "version": pins.LHM_VERSION,
            "installed": installed,
        }

    def undo(self, ctx: Context, entry: dict) -> None:
        if not entry.get("installed", True):
            return
        ctx.backend.stop_process(pins.LHM_EXE)
        ctx.backend.rmtree(Path(entry["folder"]))


class LhmSettingsStep:
    id = "lhm_settings"
    undoable = True

    def done(self, ctx: Context) -> bool:
        if ctx.manifest.get(self.id) is None:
            return False
        data = ctx.backend.read_bytes(pins.LHM_SETTINGS)
        return data is not None and _has_keys(data, WEB_SERVER_KEYS)

    def run(self, ctx: Context) -> dict:
        backend = ctx.backend
        existing = backend.read_bytes(pins.LHM_SETTINGS)
        try:
            data = apply_settings(existing, WEB_SERVER_KEYS)
        except ET.ParseError as exc:
            raise StepError(f"{pins.LHM_SETTINGS} is not valid XML: {exc}") from exc
        backup: Path | None = None
        if existing is not None:
            backup = pins.LHM_SETTINGS.with_name(pins.LHM_SETTINGS.name + ".fmon-backup")
            backend.copy(pins.LHM_SETTINGS, backup)
        backend.write_bytes(pins.LHM_SETTINGS, data)
        return {"settings": str(pins.LHM_SETTINGS), "backup": None if backup is None else str(backup)}

    def undo(self, ctx: Context, entry: dict) -> None:
        backup = entry.get("backup")
        if backup:
            ctx.backend.copy(Path(backup), pins.LHM_SETTINGS)
            ctx.backend.delete(Path(backup))
        else:
            ctx.backend.delete(pins.LHM_SETTINGS)
