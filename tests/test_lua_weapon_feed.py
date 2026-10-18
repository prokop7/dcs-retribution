"""Runs the base plugin's weapon feed in a Lua 5.1 runtime with fake DCS objects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

lupa_lua51 = pytest.importorskip("lupa.lua51")

PLUGIN = Path("resources/plugins/base/dcs_retribution.lua")

FAKE_DCS = """
fake_now = 0
fake_scheduled = {}
fake_errors = {}
fake_encoded = nil
timer = {
    getTime = function() return fake_now end,
    scheduleFunction = function(fn, arg, t) fake_scheduled[#fake_scheduled + 1] = fn end,
}
world = { event = {
    S_EVENT_SHOT = 1, S_EVENT_HIT = 2, S_EVENT_CRASH = 5, S_EVENT_DEAD = 8,
    S_EVENT_MISSION_END = 12, S_EVENT_KILL = 28, S_EVENT_UNIT_LOST = 30,
} }
Object = { Category = { UNIT = 1, WEAPON = 2 } }
Weapon = { Category = { SHELL = 0, MISSILE = 1, ROCKET = 2, BOMB = 3, TORPEDO = 4 } }
mist = {
    Logger = { new = function(self, name, level)
        return {
            info = function() end,
            error = function(_, message) fake_errors[#fake_errors + 1] = message end,
        }
    end },
    scheduleFunction = function() end,
    addEventHandler = function(handler) fake_handler = handler end,
    message = { add = function() end },
    getHeading = function() return 0 end,
}
json = { encode = function(self, value) fake_encoded = value; return "{}" end }
os = { getenv = function() return nil end, time = function() return 0 end }

function make_unit(name, type_name, coalition, x, z)
    return {
        getName = function() return name end,
        getTypeName = function() return type_name end,
        getCoalition = function() return coalition end,
        getCategory = function() return Object.Category.UNIT end,
        getPoint = function() return { x = x, y = 3000, z = z } end,
    }
end

function make_weapon(id, type_name, coalition, desc, target)
    local weapon = { id_ = id, alive = true, x = 0, z = 0 }
    weapon.isExist = function(self) return self.alive end
    weapon.getName = function(self) return tostring(id) end
    weapon.getTypeName = function(self) return type_name end
    weapon.getCoalition = function(self) return coalition end
    weapon.getCategory = function(self) return Object.Category.WEAPON end
    weapon.getDesc = function(self) return desc end
    weapon.getTarget = function(self) return target end
    weapon.getPoint = function(self)
        if not self.alive then error("Object doesn't exist") end
        return { x = self.x, y = 500, z = self.z }
    end
    return weapon
end

function fire_event(id, initiator, weapon, target)
    fake_handler({ id = id, initiator = initiator, weapon = weapon, target = target })
end

function tick(now)
    fake_now = now
    for _, fn in ipairs(fake_scheduled) do fn(nil, now) end
end
"""

HARM = '{category = 1, missileCategory = 6, guidance = 5, displayName = "AGM-88C"}'
SAM = "{category = 1, missileCategory = 2, guidance = 1}"
SHELL = "{category = 0}"


def _py(value: Any) -> Any:
    """Converts Lua tables to Python lists (1..n keys) and dicts."""
    if lupa_lua51.lua_type(value) != "table":
        return value
    items = {
        int(k) if isinstance(k, float) and k.is_integer() else k: _py(v)
        for k, v in value.items()
    }
    if items and all(isinstance(k, int) for k in items):
        if sorted(items) == list(range(1, len(items) + 1)):
            return [items[i] for i in range(1, len(items) + 1)]
    return items if items else []


@pytest.fixture
def lua() -> Any:
    runtime = lupa_lua51.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(FAKE_DCS)
    runtime.execute(PLUGIN.read_text(encoding="utf-8"))
    runtime.execute(f"""
        hornet = make_unit("Hornet-1", "FA-18C_hornet", 2, 0, 0)
        tor = make_unit("SA15-1", "Tor 9A331", 1, 30000, 40000)
        harm_def = {HARM}
        sam_def = {SAM}
        """)
    return runtime


def _fates(lua: Any) -> dict[int, dict[str, Any]]:
    return {fate["shot"]: fate for fate in _py(lua.globals().weapon_fates)}


def test_shots_are_recorded_without_shells(lua: Any) -> None:
    lua.execute(f"""
        harm1 = make_weapon(101, "AGM_88", 2, harm_def, tor)
        fire_event(world.event.S_EVENT_SHOT, hornet, harm1)
        fire_event(world.event.S_EVENT_SHOT, hornet, make_weapon(9, "M61", 2, {SHELL}))
        """)
    [shot] = _py(lua.globals().shot_events)
    assert shot == {
        "id": 1,
        "time": 0,
        "weapon": "AGM_88",
        "category": "missile",
        "display_name": "AGM-88C",
        "missile_category": "OTHER",
        "guidance": "RADAR_PASSIVE",
        "shooter": "Hornet-1",
        "shooter_type": "FA-18C_hornet",
        "shooter_coalition": 2,
        "target": "SA15-1",
        "target_type": "Tor 9A331",
        "target_coalition": 1,
        "range_m": 50000,
    }


def test_weapon_fates(lua: Any) -> None:
    lua.execute("""
        fake_now = 10
        harm1 = make_weapon(101, "AGM_88", 2, harm_def, tor)
        harm2 = make_weapon(102, "AGM_88", 2, harm_def, tor)
        harm3 = make_weapon(103, "AGM_88", 2, harm_def, tor)
        harm4 = make_weapon(104, "AGM_88", 2, harm_def, tor)
        harm5 = make_weapon(105, "AGM_88", 2, harm_def, tor)
        for _, harm in ipairs({harm1, harm2, harm3, harm4, harm5}) do
            fire_event(world.event.S_EVENT_SHOT, hornet, harm)
        end
        fake_now = 20
        sam1 = make_weapon(201, "9M338", 1, sam_def, harm1)
        sam2 = make_weapon(202, "9M338", 1, sam_def, harm4)
        sam3 = make_weapon(203, "9M338", 1, sam_def, harm5)
        for _, sam in ipairs({sam1, sam2, sam3}) do
            fire_event(world.event.S_EVENT_SHOT, tor, sam)
        end
        tick(21)
        -- harm1 and the Tor missile fired at it vanish together: intercepted.
        harm1.alive = false
        sam1.alive = false
        tick(25)
        -- harm2 hits the radar.
        fake_now = 30
        fire_event(world.event.S_EVENT_HIT, hornet, harm2, tor)
        harm2.alive = false
        tick(31)
        -- harm3 vanishes without hitting anything.
        harm3.alive = false
        tick(32)
        -- harm4 vanishes while the Tor missile fired at it flies on: no hit.
        harm4.alive = false
        tick(40)
        tick(41)
        tick(42)
        -- The Tor missile reports hitting harm5.
        fake_now = 43
        fire_event(world.event.S_EVENT_HIT, tor, sam3, harm5)
        harm5.alive = false
        tick(44)
        """)
    shots = _py(lua.globals().shot_events)
    assert [s["id"] for s in shots] == list(range(1, 9))
    tor_shot = shots[5]
    assert tor_shot["shooter_type"] == "Tor 9A331"
    assert tor_shot["target_is_weapon"] is True
    assert tor_shot["target_shot"] == 1
    assert tor_shot["missile_category"] == "SAM"

    fates = _fates(lua)
    intercepted = fates[1]
    assert intercepted["fate"] == "intercepted"
    assert intercepted["intercepted_by"] == {
        "shot": 6,
        "shooter": "SA15-1",
        "shooter_type": "Tor 9A331",
        "weapon": "9M338",
    }
    assert intercepted["time"] == 25
    assert fates[2]["fate"] == "hit"
    assert fates[3]["fate"] == "no_hit"
    assert "engaged_by" not in fates[3]
    assert fates[4]["fate"] == "no_hit"
    assert fates[4]["engaged_by"][0]["shooter_type"] == "Tor 9A331"
    assert fates[5]["fate"] == "intercepted"
    assert fates[5]["intercepted_by"]["shot"] == 8

    hits = _py(lua.globals().hit_events)
    assert hits[0]["shot"] == 2 and hits[0]["target"] == "SA15-1"
    assert hits[1]["target_is_weapon"] is True and hits[1]["target_shot"] == 5
    assert hits[1]["shot"] == 8
    errors = _py(lua.globals().fake_errors)
    assert not [e for e in errors if "Weapon feed" in e]


def test_kills_count_as_hits_and_mission_end_flushes(lua: Any) -> None:
    lua.execute("""
        harm1 = make_weapon(101, "AGM_88", 2, harm_def, tor)
        harm2 = make_weapon(102, "AGM_88", 2, harm_def, tor)
        fire_event(world.event.S_EVENT_SHOT, hornet, harm1)
        fire_event(world.event.S_EVENT_SHOT, hornet, harm2)
        sam1 = make_weapon(201, "9M338", 1, sam_def, harm2)
        fire_event(world.event.S_EVENT_SHOT, tor, sam1)
        fake_now = 5
        fire_event(world.event.S_EVENT_KILL, hornet, harm1, tor)
        harm1.alive = false
        harm2.alive = false
        tick(6)
        -- harm2 waits for the Tor missile; the mission ends first.
        fake_now = 7
        fire_event(world.event.S_EVENT_MISSION_END)
        """)
    fates = _fates(lua)
    assert fates[1]["fate"] == "hit"
    assert fates[2]["fate"] == "no_hit"
    assert 3 not in fates  # still flying when the mission ended


def test_state_includes_the_weapon_feed(lua: Any) -> None:
    lua.execute("""
        fire_event(world.event.S_EVENT_SHOT, hornet, make_weapon(1, "AGM_88", 2, harm_def, tor))
        debriefing_file_location = "state.json"
        io = { open = function()
            return { write = function() end, close = function() end }
        end }
        write_state()
        """)
    state = _py(lua.globals().fake_encoded)
    assert len(state["shot_events"]) == 1
    assert state["hit_events"] == [] and state["weapon_fates"] == []


def test_odd_objects_do_not_break_the_feed(lua: Any) -> None:
    lua.execute("""
        local broken = {
            id_ = 7,
            getTypeName = function() error("gone") end,
            getDesc = function() error("gone") end,
        }
        fire_event(world.event.S_EVENT_SHOT, nil, broken)
        fire_event(world.event.S_EVENT_SHOT, hornet, nil)
        fire_event(world.event.S_EVENT_HIT, nil, nil, tor)
        tick(1)
        """)
    assert _py(lua.globals().shot_events) == [{"id": 1, "time": 0}]
    assert _py(lua.globals().hit_events) == []
    assert _fates(lua)[1]["fate"] == "no_hit"
