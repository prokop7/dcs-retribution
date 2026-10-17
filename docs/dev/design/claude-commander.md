# Claude Commander

Status: **draft for review**. Branch: `feature/claude-commander`, based on `origin/dev` @ 49e8067f.

The goal is fully autonomous, LLM-driven campaign planning for RED and BLUE. Claude takes on the commander role: it decides air packages (down to waypoints and loadouts), procurement, ground-war stance, squadron basing and transfers, and repairs. It keeps a persistent memory of the campaign. REDFOR should play like a human opponent.

## 1. Decisions (from the requirements Q&A)

| Topic | Decision |
|---|---|
| How Claude is invoked | Claude Code CLI in headless mode (`claude -p`). The game exposes its state and actions over MCP. It uses the user's Claude Code login. |
| Sides | Each side can be switched on separately. BLUE is fully autonomous like today's auto-planner, and the player can edit, delete or add packages afterwards. |
| Decision depth | **Everything**: objectives, targets, package composition, aircraft type and squadron, TOT and sequencing, waypoints, altitudes, speeds, loadouts, start type. |
| Other domains | Procurement (air, ground, SAM/TGO), front-line stance, squadron basing/transfers, repairs (runways, TGOs, buildings). |
| Fog of war | **Static known, dynamic hidden.** Enemy bases, TGOs and SAM sites, and their specifications, are known. The enemy ATO, flight plans and exact force counts are hidden. |
| What makes it human-like | (a) Campaign memory and intent across turns. (b) Learning from debriefings. |
| Directives | Each side has a free-text commander directive written by the player, injected into every run. |
| Sessions | One long-lived Claude session per campaign per side. It is **not** a fresh session each turn. A Markdown journal is kept as a backup in case the session is lost. |
| Rewind | Loading an older save of the same campaign starts a new session, seeded from the journal cut off at that turn. |
| Other campaign | Loading a different campaign never continues another campaign's session. |
| Isolation | The RED and BLUE runs are separate processes with separate MCP endpoints, and they run in parallel. |
| Progress | The planning run is shown live: packages and routes appear on the map while Claude keeps planning. |
| Mid-turn changes | Anything that re-runs `initialize_turn` (buying/selling TGO units, cheats, ground stance, changes to time/date/weather/conditions, air wing config) triggers a new **incremental** Claude run in the same session. Packages the player made or edited are locked. |
| Failures | Planning is blocked and an error dialog appears with **Retry**, **Use built-in planner this turn** and **Cancel**. |
| RED visibility | RED's reasoning and journal are hidden by default. A setting reveals RED's live log and journal. |
| Model | Opus by default. Model and thinking effort can be changed in settings. |
| Timeout | Configurable per side, 20 min by default. When it runs out, the error dialog appears. |
| Old HTN tweak branches | **Dropped as heuristics.** Claude instead gets the full raw specifications (SAM ranges, coverage, unit capabilities, income, …) and decides by itself. Pre-chewed heuristics could leak hidden enemy information. |
| Repair branches | Included and rebased. SAMs bought mid-campaign activate after a delay, configurable (0 = instant). |
| Delivery | A new branch on the `prokop7` fork, with a PR inside the fork. Upstreaming is up to the user later. |

## 2. Architecture

```
 Qt UI ──(signals)──┐
                    │                ┌── claude -p (RED)  ── MCP ──┐
 Game ── TurnFlow ──┼── PlanningJob ─┤                             ├─► /mcp/red   ─┐
                    │   (thread)     └── claude -p (BLUE) ── MCP ──┘   /mcp/blue  ─┤
 FastAPI server (existing, localhost:16880) ◄────────────────────────────────────┘
        └── CommanderView(side): fog-of-war filtered read model + validated actions
```

### 2.1 Runtime: `game/claudecommander/`
- **`CommanderState`** (pickled in `Coalition`, one per side):
  - `session_id`
  - `campaign_id`
  - `last_synced_turn`
  - `journal_path`
  - the latest debrief summary
  - the set of locked package ids (BLUE)

  `Game` gets a `campaign_id` (a UUID made at new game; the migrator assigns one for old saves).
- **`ClaudeProcess`** spawns the CLI:
  ```
  claude -p --resume <id> | --session-id <new uuid>
         --input-format stream-json --output-format stream-json --verbose
         --model <setting> --effort <setting>
         --mcp-config <temp json: http://127.0.0.1:16880/mcp/<side>, bearer token>
         --strict-mcp-config --setting-sources "" --tools ""
         --allowedTools "mcp__retribution__*"
         --append-system-prompt-file <commander system prompt>
  ```
  - `--tools ""` turns off every built-in tool (files, shell, web). Claude can only touch the game through its own side's MCP endpoint. This enforces fog of war: it cannot read saves, the other side's journal, or the game's code.
  - The process reads stream-json events for progress, the commander log and tool-call tracing.
  - It handles timeout and cancel by killing the process tree.
- **`SessionManager`** decides whether to resume or start a new session:
  - Different `campaign_id` → new session.
  - `game.turn < last_synced_turn` (rewind) → new session seeded with `journal.md` cut off at that turn.
  - `--resume` fails because the session was lost → new session seeded with the whole journal.
- **Journal** (Markdown, human-readable) lives at `<Saved Games>/Retribution/Commander/<campaign_id>/<side>/journal.md`. It has one `## Turn N` section per turn. Each section holds the commander's intent and objectives (written by Claude through a tool), the plan summary, the debrief, and lessons learned. The game writes the file, never Claude directly.
- **`PlanningJob`** runs both sides in parallel on worker threads. It emits Qt signals (progress, package created, finished, failed). Game mutations from tool calls go through one game lock, the same way the existing server routes mutate the game, and emit `GameUpdateEvents` so the map updates live.

### 2.2 MCP endpoint: `game/server/mcp/`
- This is a small hand-written implementation of MCP Streamable HTTP: JSON-RPC over POST, with `initialize`, `tools/list` and `tools/call`. It is mounted on the existing FastAPI server.
- Writing it by hand avoids the official `mcp` SDK, which needs `anyio>=4.5`, while upstream pins `anyio==3.7.1`.
- Tool input schemas are generated from pydantic models.
- Each side gets a random bearer token per session, and the server only accepts localhost connections. The server only exposes `CommanderView(side)`.

### 2.3 `CommanderView(side)`: fog of war
All reads go through this view. Nothing hands raw `Game` objects to the tools.
- **Own side**, everything:
  - bases, runway status, squadrons
  - aircraft and pilot availability, ranges, available loadouts
  - TGOs and SAMs with unit health
  - front-line strength, budget, income, pending orders and transfers, the current ATO
- **Enemy, static (known):**
  - control points and their type
  - runway status as of the last turn
  - TGO and SAM sites with type, specifications, detection/threat ranges and layout
  - buildings with strategic/income value
  - front-line positions
  - the faction's aircraft and unit catalogue
- **Enemy, dynamic (hidden):** ATO and flight plans, exact aircraft counts per base, exact ground-unit counts, current SAM unit health and repairs in progress. Damage to the enemy is only reported through debriefs, as "confirmed kills".
- **Specification lookups:** aircraft (role, combat radius, speed, sensors, weapons by era), weapons, ground units, and the IADS/SAM ranges known for the other side.
- **Geometry helpers:** distance and bearing; a route threat check against *known* enemy threat zones; travel time and fuel estimates for a candidate flight plan.

### 2.4 Tools (first cut)
Every write validates its input and returns either the result or a structured error, so Claude can fix it and try again.

**Read**
- `get_situation`: turn, date/time, weather, budget, victory status, directive, changes since the last run.
- `get_own_forces`
- `get_enemy_intel`
- `get_targets`: possible targets by mission type, with specifications.
- `get_front_lines`
- `get_ato`
- `get_debrief`: the last mission, from the commander's own point of view.
- `lookup_aircraft`, `lookup_unit`, `lookup_weapon`
- `check_route_threats`
- `estimate_flight`

**Air**
- `create_package`: target, flights `[{task, count, aircraft_type, squadron?, start_type?}]`, optional TOT. This builds a valid default plan with the existing `PackageBuilder`/flight-plan builders.
- `set_package_tot`
- `edit_flight_waypoints`: insert, move, delete, set altitude and speed.
- `set_flight_loadout`: a named loadout or pylon by pylon, era-validated.
- `set_flight_start_type`
- `delete_package`
- `finalize_air_plan`: runs `MissionScheduler` for packages that have no explicit TOT and returns validation warnings (fuel, range, TOT conflicts).

**Ground / logistics / economy**
- `set_front_line_stance`
- `buy_aircraft`
- `buy_ground_units`
- `buy_or_rebuild_tgo`
- `repair_runway`
- `repair_tgo`
- `repair_building`
- `transfer_units`
- `relocate_squadron`

**Memory**
- `write_journal`: intent, objectives, notes.
- `read_journal`
- `finish_turn_planning`: summary. Ends the run.

### 2.5 Turn integration
- In `Coalition.initialize_turn`, a side with Claude enabled skips the HTN commander, `ProcurementAi`, ground planner stances and transfer planning for the domains Claude owns. `Game.initialize_turn` then starts a `PlanningJob` for those sides.
- **Start of turn** (after the debriefing): the job sends the side's session a turn message. The message holds the turn number, the conditions, the fog-filtered debrief (own losses, confirmed kills, targets hit and missed), changes, the directive, and the instruction to plan.
- **Mid-turn re-inits** (TGO buy/sell, cheats, ground stance, conditions dialog, air wing config) send an *incremental* message describing what changed, plus the current ATO. Packages the player locked are marked read-only in the tools. Conditions changes (time/date/weather) notify **both** sides.
- **Turn 0** follows upstream: no missions. Claude does the initial procurement, reads the campaign briefing and writes its opening intent.
- **UI:**
  - A commander status bar or panel per side shows the state (planning / done / error), elapsed time and the package count.
  - The BLUE log is visible. The RED log is hidden unless "Reveal RED commander" is on.
  - "Take off" is disabled while planning runs.
  - Failures open the error dialog: Retry / Use built-in planner this turn / Cancel.
  - A "Re-plan with Claude" button per side.
- **Settings** (new page "Claude Commander"):
  - Enable for RED / for BLUE
  - Domains per side
  - Claude executable path
  - Model (default Opus)
  - Effort (default high)
  - Timeout per side (default 20 min)
  - Reveal RED commander
  - RED directive / BLUE directive (multi-line text)

## 3. Repair branches (included first)
- Rebase `prokop7/feature/ground-unit-purchase` (SAM repairs) and `prokop7/feature/ground-buildings-repairing` onto the new base as separate commits.
- Unify the infrastructure they duplicate (`repair_turns_remaining`, `process_ground_object_repairs`, `required_unit_classes`).
- Pass the `ProcurementAi` flags as keywords.
- Resolve `QBuildingInfo.py` against upstream #793.
- Fix the small issues found in review: map refresh when repair turns = 0, and the EWR wording.
- Add tests.
- The repair logic becomes the engine behind the `repair_*` tools. Sides without Claude keep the automated behaviour.

## 4. Open points (defaults chosen; tell me if you want them changed)
1. **Enemy order of battle.** *Default:* which squadrons and aircraft types sit at which enemy base counts as hidden. Claude only sees the enemy faction's catalogue, plus aircraft it has seen in debriefs: types encountered or shot down, and where they came from if known. The alternative is "known squadrons per base, counts hidden".
2. **Domains for BLUE.** *Default:* BLUE domains follow the existing `automate_*` settings, so Claude only buys or repairs for BLUE where automation is on.
3. **Player's own flights.** *Default:* with `auto_ato_behavior`, Claude may assign player-flown slots following the existing preference settings.
4. **Game lock while planning.** *Default:* while a side is planning, the player can still look at the UI but cannot edit, so that planning stays consistent. Editing BLUE is unlocked as soon as BLUE finishes.

## 5. Implementation phases
1. **Base:** branch, CLAUDE.md update, repair branches rebased and unified. *(One agent.)*
2. **Runtime core:** `game/claudecommander` (process, session manager, journal, planning job), the MCP endpoint, settings, and save compatibility. *(Me.)*
3. **Tools:** read/intel tools with `CommanderView` and fog-of-war tests *(agent A)*, and write tools for air, ground, logistics and economy *(agent B, after A has finished the view interfaces)*. This needs a test-game fixture built from a small campaign.
4. **UI and turn integration:** status panels, live map updates, error dialog, settings page, mid-turn re-init hooks. *(One agent.)*
5. **Prompts and live tuning:** commander system prompt, turn and incremental message templates. A few real headless runs on a test campaign to check tool coverage and plan quality.
6. Lint (black, mypy), tests, changelog, push to the fork, PR.
