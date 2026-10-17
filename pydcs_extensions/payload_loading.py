"""Makes pydcs skip payload files it cannot parse instead of failing.

pydcs merges an aircraft's payloads from every payload directory (the user's
Mission Editor payloads, DCS and mod payloads, then Retribution's own). Its
``FlyingType.load_payloads`` only handles ``SyntaxError``, and it assigns the
payload table before reading the files. A mod payload file it can't read (the CJS
Super Hornet 2.4 files use Lua symbols such as ``CEN`` as pylon numbers) therefore
raises once and from then on leaves a half-filled table behind, without the
payloads of later directories. Retribution's default loadouts live in the last
directory, so the aircraft silently got empty default loadouts.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from dcs import lua
from dcs.payloads import PayloadDirectories
from dcs.unittype import FlyingType


def _load_payloads(cls: type[FlyingType]) -> dict[str, Any]:
    if FlyingType._UnitPayloadGlobals is None:
        from dcs import task

        FlyingType._UnitPayloadGlobals = {
            v.internal_name: v.id for k, v in task.MainTask.map.items()
        }

    FlyingType.scan_payload_dir()
    if cls.payloads is not None:
        return cls.payloads

    payloads: dict[str, Any] = {}
    cache = FlyingType._payload_cache or {}
    for payload_dir in PayloadDirectories.payload_dirs():
        if not payload_dir.exists():
            continue
        for payload_path in payload_dir.glob("*.lua"):
            if cache.get(payload_path) != cls.id or not payload_path.exists():
                continue
            try:
                payload_main = lua.loads(
                    payload_path.read_text(),
                    _globals=cast(dict[str, Any], FlyingType._UnitPayloadGlobals),
                )
                unit_payloads = payload_main["unitPayloads"]
                if unit_payloads["unitType"] != cls.id:
                    continue
                # Directories are iterated in decreasing order of preference, so
                # the first payload of a name wins.
                for load in unit_payloads["payloads"].values():
                    payloads.setdefault(load["name"], load)
            except Exception:
                logging.getLogger("pydcs").warning(
                    "Skipped payload file %s for %s: it could not be read",
                    payload_path,
                    cls.id,
                    exc_info=True,
                )
    cls.payloads = payloads
    return payloads


def install() -> None:
    FlyingType.load_payloads = classmethod(_load_payloads)  # type: ignore[method-assign, assignment]
