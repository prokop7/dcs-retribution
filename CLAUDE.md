# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

DCS Retribution is a fork of DCS Liberation: an external Python/Qt program that runs a persistent, turn-based dynamic campaign for DCS World. Each turn it generates a `.miz` mission file (via [pydcs](https://github.com/dcs-retribution/pydcs), pinned to a commit in `requirements.txt`) and then reads the result back from DCS. The project targets Windows and Python 3.11. CI runs only on `windows-latest`, except for black.

PRs target the **`dev`** branch, which keeps a linear history (PRs get rebased or squashed). Keep each PR to one task. Add user-visible features and fixes to `changelog.md` under the upcoming release.

## Commands

Run everything from the repo root. Many code paths load data through relative paths such as `Path("resources/units/aircraft")`.

```powershell
python -m venv .venv; ./.venv/scripts/activate; pip install -r requirements.txt   # CI uses Python 3.11 and ./venv

$env:PYTHONPATH="."                  # required: qt_ui/main.py imports `game` from the repo root
python qt_ui/main.py                 # run the app
python qt_ui/main.py --dev           # dev mode; set CORS_ALLOW_DEBUG_SERVER=true to use the React dev server
python qt_ui/main.py new-game <campaign.yaml> --blue "USA 2005" --red "Russia 1990"   # skip the new-game wizard
python qt_ui/main.py lint-weapons <aircraft variant>
python qt_ui/main.py dump-task-priorities

pytest tests                                           # fuzz tests are excluded by default (pytest.ini)
pytest tests/test_factions.py::TestFactionLoader::test_load_valid_faction   # single test
pytest -m fuzztest tests                               # run the randomized fuzz tests explicitly
pytest --cov --cov-report=xml tests                    # what CI runs

black .            # CI runs `black --check .`
mypy game          # CI type-checks only `game` and `tests`, not `qt_ui`
mypy tests
```

mypy runs in strict-ish mode (`mypy.ini`): `disallow_untyped_defs` and `disallow_any_generics` are on, and so is the pydantic plugin. New code in `game/` must be fully annotated.

Front-end (`client/`, a React + TypeScript + Redux Toolkit + Leaflet map shown inside the Qt UI's web view):

```powershell
cd client; npm ci
npm start                       # dev server on :3000 (run the Python app with CORS_ALLOW_DEBUG_SERVER=true)
npm test -- --watchAll=false    # CI: npm test -- --coverage
npm run lint
npm run regenerate-api          # with the Python app running; regenerates src/api/liberationApi.ts from FastAPI /openapi.json
```

Release build (what CI does): `npm run build` in `client/`, then `pyinstaller pyinstaller.spec` from the root.

## Architecture

### Process layout
`qt_ui/main.py` is the entry point. It starts a FastAPI/uvicorn server (`game/server/`, default port 16880) in a background thread and then runs the PySide6 Qt UI. The map is the React client running in a Qt web view. It talks to the server through REST routers (one sub-package per domain: `flights`, `waypoints`, `controlpoints`, `tgos`, …) plus a websocket event stream (`game/server/eventstream`). `GameContext` (`game/server/dependencies.py`) gives the server access to the currently loaded `Game`. Changes made in the model reach the UI as `GameUpdateEvents` (`game/sim/gameupdateevents.py`), which the event stream pushes to the client. If you change the API surface, regenerate the TS stubs.

### Core model and the turn cycle
`game/game.py::Game` is the root object. It holds the theater (`game/theater`: control points, TGOs, front lines, conflict theater per DCS terrain) and two `Coalition`s (blue/red). Each coalition owns its faction, air wing and squadrons, its ATO (`game/ato`: packages → flights → flight plans/waypoints), transfers and budget.

- `Game.initialize_turn` checks win/loss, computes threat zones, calls each `Coalition.initialize_turn` (procurement plus AI air planning), then plans the ground war and culling. This can run **more than once per turn** (after a cheat capture, or after buying or selling TGO units), so turn-init code must be safe to re-run.
- AI planning uses a generic HTN planner (`game/htn.py`). The theater commander (`game/commander/`) plans over `TheaterState` with compound and primitive tasks in `game/commander/tasks/`. Mission proposals are turned into real packages by `PackageFulfiller`/`PackageBuilder`. Flight-plan geometry comes from `game/flightplan` and `game/ato/flightplans`.
- `game/sim/` (`GameLoop`, `MissionSimulation`, `AircraftSimulation`) fast-forwards the campaign clock in-app before a mission starts, handling flight states and combat freezes. See `docs/dev/design/turnless.md` for the longer-term direction.
- `game/missiongenerator/MissionGenerator.generate_miz` writes the `.miz` file through pydcs. Each sub-generator handles one concern: aircraft, TGOs, front line, convoys, triggers, kneeboards, briefing, and Lua.
- Lua plugins (`resources/plugins/<name>/`, registered in `plugins.json`, managed by `game/plugins`) are injected into the mission. While DCS runs, the base plugin writes `state.json`. `PollDebriefingFileThread` picks it up and turns it into a `Debriefing`, and `MissionResultsProcessor` applies the losses and captures. `Game.pass_turn`/`finish_turn` then advance to the next turn.

### Save files and compatibility
Saves are pickled `Game` objects (`game/persistency.py`). Retribution promises forward compatibility for saves, so any change to a pickled class needs migration handling:
- Classes that moved or were renamed: add a mapping to `MigrationUnpickler` in `game/persistency.py`.
- New or changed attributes on loaded objects: patch them in `game/migrator.py::Migrator`, or in the class's `__setstate__`.
- `Settings` fields get save compat automatically, because `Settings.__setstate__` starts from a fresh `Settings()` and overlays the saved state.
- `@has_save_compat_for(major)` (`game/savecompat.py`) marks compat code tied to a major version. It raises at startup once `MAJOR_VERSION` (`game/version.py`) moves on.

### Data-driven content
Most game content is data in `resources/`, loaded by thin Python wrappers:
- `resources/units/{aircraft,ground_units,ships}/*.yaml`, one file per pydcs unit id → `game/dcs/{aircraft,groundunit,shipunit}type.py`
- `resources/factions/*.json` → `game/factions`. Weapons and introduction years: `resources/weapons` → `game/data/weapons.py`
- `resources/campaigns` (a `.miz` plus a `.yaml` per campaign) → `game/campaignloader`. Force-group layouts: `resources/layouts` → `game/layout`. Squadron definitions: `resources/squadrons`. Lua payloads: `resources/customized_payloads`.
- `pydcs_extensions/` defines pydcs unit types for third-party aircraft and unit mods that stock pydcs doesn't include. `game/modsupport.py` ties them in.
- `resources/tools/` holds maintenance scripts (terrain/landmap import, `datalint.py`, `mkrelease.py`, …).

### Settings
`game/settings/settings.py::Settings` is one large dataclass. Each field is declared with an option helper (`boolean_option`, `bounded_int_option`, `bounded_float_option`, `choices_option`, `minutes_option`, `skill_option`) that carries its `page`/`section` metadata. The Qt settings window and the new-game wizard build their widgets from that metadata. Adding a field with a default is usually all a new setting needs.

### Claude Commander (branch `feature/claude-commander`, in progress)
Design: `docs/dev/design/claude-commander.md`. Claude (headless Claude Code CLI) plans RED and/or BLUE: air packages down to waypoints and loadouts, procurement, front-line stance, transfers and repairs. Code lives in `game/claudecommander/`:
- `coordinator.py` decides when a side plans. It is driven by `Coalition.initialize_turn` and `Game.notify_commander`.
- `runner.py`, `process.py` and `session.py` launch `claude -p` and resume one long-lived session per campaign and side.
- `tools/` holds the MCP tools. Every read must go through `view.py` (`CommanderView`), which enforces fog of war ("static known, dynamic hidden").
- `state.py` (`CommanderState`) is saved on each `Coalition`.

The MCP route is `game/server/mcp/routes.py` on the existing FastAPI server, and a per-run token binds each connection to one side. Tests are in `tests/claudecommander/`; `conftest.py` generates a real campaign (`test_game`, about 10 s once per session). The older `game/llm` / `game/mcp` code on `feature/mcp-server` is superseded; don't port it.
