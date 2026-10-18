-- the state.json file will be updated according to this schedule and on mission end
local WRITESTATE_SCHEDULE_IN_SECONDS = 15

logger = mist.Logger:new("DCSRetribution", "info")
logger:info("Check that json.lua is loaded : json = "..tostring(json))

crash_events = {} -- killed aircraft will be added via S_EVENT_CRASH event
dead_events = {} -- killed units will be added via S_EVENT_DEAD event
unit_lost_events = {} -- killed units will be added via S_EVENT_UNIT_LOST
kill_events = {} -- killed units will be added via S_EVENT_KILL 
kill_details = {} -- who killed what, with which weapon, where and when (S_EVENT_KILL)
shot_events = {} -- weapons fired, except gun rounds and shells (S_EVENT_SHOT)
hit_events = {} -- weapon hits (S_EVENT_HIT), except gun rounds and shells
weapon_fates = {} -- what became of each tracked weapon: hit, intercepted or no_hit
base_capture_events = {}
destroyed_objects_positions = {} -- will be added via S_EVENT_DEAD event
mission_ended = false
dirty_state = false -- Track if state has changed and needs writing

local function ends_with(str, ending)
   return ending == "" or str:sub(-#ending) == ending
end

local function messageAll(message)
    local msg = {}
    msg.text = message
    msg.displayTime = 25
    msg.msgFor = {coa = {'all'}}
    mist.message.add(msg)
end

function write_state()
    local _debriefing_file_location = debriefing_file_location
    if not debriefing_file_location or debriefing_file_location == "" then
        error("Unable to save DCS Retribution state: debriefing file path is unavailable")
    end

    if not json then
        error("Unable to save DCS Retribution state, JSON library is not loaded")
    end

    local fp, open_error = io.open(_debriefing_file_location, 'w')
    if not fp then
        error("Unable to open state file for writing: "..tostring(_debriefing_file_location).." ("..tostring(open_error)..")")
    end
    local game_state = {
        ["crash_events"] = crash_events,
        ["dead_events"] = dead_events,
        ["base_capture_events"] = base_capture_events,
		["unit_lost_events"] = unit_lost_events,
		["kill_events"] = kill_events,
		["kill_details"] = kill_details,
        ["shot_events"] = shot_events,
        ["hit_events"] = hit_events,
        ["weapon_fates"] = weapon_fates,
        ["mission_ended"] = mission_ended,
        ["destroyed_objects_positions"] = destroyed_objects_positions,
    }
    local ok, write_error = pcall(function()
        fp:write(json:encode(game_state))
    end)
    fp:close()
    if not ok then
        error(write_error)
    end
end

local function canWrite(name)
    local f = io.open(name, "a")
    if f then
        f:close()
        return true
    end
    return false
end

local function testDebriefingFilePath(folderPath, folderName, useCurrentStamping)
    if folderPath then
        local filePath = nil
        if not ends_with(folderPath, "\\") then
            folderPath = folderPath .. "\\"
        end
        if useCurrentStamping then
            filePath = string.format("%sstate-%s.json",folderPath, tostring(os.time()))
        else 
            filePath = string.format("%sstate.json",folderPath)
        end
        local isOk = canWrite(filePath)
        if isOk then 
            logger:info(string.format("The state.json file will be created in %s : (%s)",folderName, filePath))
            return filePath
        end
    end
    return nil
end

local function discoverDebriefingFilePath()   
    -- establish a search pattern into the following modes
    -- 1. Environment variable RETRIBUTION_EXPORT_DIR, to support dedicated server hosting
    -- 2. Embedded DCS Retribution dcsRetribution.installPath (set by the app to its install path), to support locally hosted single player
    -- 3. System temporary folder, as set in the TEMP environment variable
    -- 4. Working directory.
    
    local useCurrentStamping = nil
    if os then  
        useCurrentStamping = os.getenv("RETRIBUTION_EXPORT_STAMPED_STATE")
    end

    local installPath = nil
    if dcsRetribution then
        installPath = dcsRetribution.installPath
    end
    
    if os then
        local result = nil
        -- try using the RETRIBUTION_EXPORT_DIR environment variable
        result = testDebriefingFilePath(os.getenv("RETRIBUTION_EXPORT_DIR"), "RETRIBUTION_EXPORT_DIR", useCurrentStamping)
        if result then
            return result
        end
        -- no joy ? maybe there is a valid path in the mission ?
        result = testDebriefingFilePath(installPath, "the DCS Retribution install folder", useCurrentStamping)
        if result then
            return result
        end
        -- there's always the possibility of using the system temporary folder
        result = testDebriefingFilePath(os.getenv("TEMP"), "TEMP", useCurrentStamping)
        if result then
            return result
        end
    end

    -- nothing worked, let's try the last resort folder : current directory.
    if lfs then
        return testDebriefingFilePath(lfs.writedir().."Missions\\", "the working directory", useCurrentStamping)
    end
    
    return nil
end

debriefing_file_location = discoverDebriefingFilePath()
local error_message_shown = false

write_state_error_handling = function()
    local _debriefing_file_location = debriefing_file_location
    if not debriefing_file_location then 
        _debriefing_file_location = "[nil]"
        logger:error("Unable to find where to write DCS Retribution state")
    end

    -- Only write if state has changed since last write
    if dirty_state then
        if pcall(write_state) then
            dirty_state = false -- Reset dirty flag after successful write
            error_message_shown = false
        else
            if not error_message_shown then
                messageAll("Unable to write DCS Retribution state to ".._debriefing_file_location..
                        "\nYou can abort the mission in DCS Retribution.\n"..
                        "\n\nPlease fix your setup in DCS Retribution, make sure you are pointing to the right installation directory from the File/Preferences menu. Then after fixing the path restart DCS Retribution, and then restart DCS."..
                        "\n\nYou can also try to fix the issue manually by replacing the file <dcs_installation_directory>/Scripts/MissionScripting.lua by the one provided there : <dcs_retribution_folder>/resources/scripts/MissionScripting.lua. And then restart DCS. (This will also have to be done again after each DCS update)"..
                        "\n\nIt's not worth playing, the state of the mission will not be recorded.")
                error_message_shown = true
            end
        end
    end

    -- Reschedule quickly if mission is over and we still have unsaved changes,
    -- otherwise use the normal cadence.
    local next_schedule_in_seconds = WRITESTATE_SCHEDULE_IN_SECONDS
    if mission_ended and dirty_state then
        next_schedule_in_seconds = 1
    end
    mist.scheduleFunction(write_state_error_handling, {}, timer.getTime() + next_schedule_in_seconds)
end

-- Describes an S_EVENT_KILL for the debriefing. Every field is read defensively:
-- the initiator may be a static, scenery or already gone.
local function kill_detail(event)
    local detail = { time = timer.getTime() }
    local target = event.target
    if target.getName then detail.target = target:getName() end
    if target.getTypeName then detail.target_type = target:getTypeName() end
    if target.getPoint then
        local point = target:getPoint()
        detail.x = point.x
        detail.z = point.z
        detail.alt = point.y
    end
    local killer = event.initiator
    if killer then
        if killer.getName then detail.killer = killer:getName() end
        if killer.getTypeName then detail.killer_type = killer:getTypeName() end
        if killer.getCoalition then detail.killer_coalition = killer:getCoalition() end
    end
    if event.weapon_name then
        detail.weapon = event.weapon_name
    elseif event.weapon and event.weapon.getTypeName then
        detail.weapon = event.weapon:getTypeName()
    end
    return detail
end

-- Weapon feed: what was fired (S_EVENT_SHOT), what hit (S_EVENT_HIT) and what
-- became of each weapon. Gun rounds (S_EVENT_SHOOTING_START/END) and shells are
-- left out. Each launched weapon is tracked until it no longer exists, then gets
-- a fate:
--   "hit"         an S_EVENT_HIT (or S_EVENT_KILL) named this weapon;
--   "intercepted" a defending weapon was fired at it (its getTarget() was this
--                 weapon) and either hit it, or ended within INTERCEPT_WINDOW
--                 seconds of it without this weapon hitting anything;
--   "no_hit"      it vanished without a hit event.
-- The intercept inference is approximate: weapons are polled every
-- WEAPON_CHECK_INTERVAL seconds, a defender that ends next to a weapon that then
-- misses counts as an intercept, and blast damage DCS doesn't report as a hit
-- counts as "no_hit". Weapons still in flight when the mission ends get no fate.
local MAX_SHOT_EVENTS = 4000
local MAX_HIT_EVENTS = 4000
local MAX_WEAPON_FATES = 4000
local MAX_TRACKED_WEAPONS = 600
local MAX_ENGAGEMENTS_PER_WEAPON = 8
local WEAPON_CHECK_INTERVAL = 1
local INTERCEPT_WINDOW = 2
local ENDED_WEAPON_MEMORY = 60

local WEAPON_CATEGORY_NAMES = { [0] = "shell", [1] = "missile", [2] = "rocket", [3] = "bomb", [4] = "torpedo" }
local MISSILE_CATEGORY_NAMES = { [1] = "AAM", [2] = "SAM", [3] = "BM", [4] = "ANTI_SHIP", [5] = "CRUISE", [6] = "OTHER" }
local GUIDANCE_NAMES = {
    [1] = "INS", [2] = "IR", [3] = "RADAR_ACTIVE", [4] = "RADAR_SEMI_ACTIVE",
    [5] = "RADAR_PASSIVE", [6] = "TV", [7] = "LASER", [8] = "TELE",
}

local tracked_weapons = {} -- weapon key -> track, while the weapon exists
local tracked_weapon_count = 0
local pending_fates = {} -- weapon key -> track, gone but waiting for its defenders
local ended_weapons = {} -- weapon key -> mission time it was found gone
local last_shot_id = 0
local weapon_feed_error_logged = false

local function shell_category()
    if Weapon and Weapon.Category and Weapon.Category.SHELL then
        return Weapon.Category.SHELL
    end
    return 0
end

local function weapon_object_category()
    if Object and Object.Category and Object.Category.WEAPON then
        return Object.Category.WEAPON
    end
    return 2
end

-- Calls obj:method() and returns its first result, or nil if the object lacks
-- the method or DCS raises (the object may already be gone).
local function try_call(obj, method)
    if obj == nil then return nil end
    local ok, fn = pcall(function() return obj[method] end)
    if not ok or type(fn) ~= "function" then return nil end
    local called, value = pcall(fn, obj)
    if called then return value end
    return nil
end

local function weapon_key(weapon)
    if weapon == nil then return nil end
    if type(weapon) == "table" and weapon.id_ ~= nil then
        return tostring(weapon.id_)
    end
    return tostring(weapon)
end

local function point_of(obj)
    local point = try_call(obj, "getPoint")
    if type(point) == "table" and type(point.x) == "number" and type(point.z) == "number" then
        return point
    end
    return nil
end

local function describe_object(obj, prefix, record)
    if obj == nil then return end
    record[prefix] = try_call(obj, "getName")
    record[prefix .. "_type"] = try_call(obj, "getTypeName")
    record[prefix .. "_coalition"] = try_call(obj, "getCoalition")
end

local function is_weapon_object(obj)
    return obj ~= nil and try_call(obj, "getCategory") == weapon_object_category()
end

-- A tracked weapon, in flight or waiting for its fate.
local function find_track(weapon)
    local key = weapon_key(weapon)
    if key == nil then return nil end
    return tracked_weapons[key] or pending_fates[key]
end

local function weapon_category(weapon)
    local desc = try_call(weapon, "getDesc")
    if type(desc) ~= "table" then return nil, nil end
    return desc.category, desc
end

local function shot_detail(event)
    local weapon = event.weapon
    if weapon == nil or #shot_events >= MAX_SHOT_EVENTS then return nil end
    local category, desc = weapon_category(weapon)
    if category == shell_category() then return nil end

    last_shot_id = last_shot_id + 1
    local shot = { id = last_shot_id, time = timer.getTime() }
    shot.weapon = try_call(weapon, "getTypeName") or event.weapon_name
    if desc then
        shot.category = WEAPON_CATEGORY_NAMES[category] or category
        if type(desc.displayName) == "string" and desc.displayName ~= "" then
            shot.display_name = desc.displayName
        end
        if desc.missileCategory then
            shot.missile_category = MISSILE_CATEGORY_NAMES[desc.missileCategory] or desc.missileCategory
        end
        if desc.guidance then
            shot.guidance = GUIDANCE_NAMES[desc.guidance] or desc.guidance
        end
    end
    local shooter = event.initiator
    describe_object(shooter, "shooter", shot)

    local target = try_call(weapon, "getTarget")
    local target_track = nil
    if target ~= nil then
        describe_object(target, "target", shot)
        if is_weapon_object(target) then
            shot.target_is_weapon = true
            target_track = find_track(target)
            if target_track then shot.target_shot = target_track.shot end
        end
        local from = point_of(shooter)
        local to = point_of(target)
        if from and to then
            local dx, dz = to.x - from.x, to.z - from.z
            shot.range_m = math.floor(math.sqrt(dx * dx + dz * dz))
        end
    end
    shot_events[#shot_events + 1] = shot

    local key = weapon_key(weapon)
    if tracked_weapon_count < MAX_TRACKED_WEAPONS and key and not tracked_weapons[key] then
        local track = {
            key = key,
            shot = shot.id,
            weapon = weapon,
            weapon_type = shot.weapon,
            shooter = shot.shooter,
            shooter_type = shot.shooter_type,
        }
        local point = point_of(weapon) or point_of(shooter)
        if point then
            track.x, track.z, track.alt = point.x, point.z, point.y
        end
        tracked_weapons[key] = track
        tracked_weapon_count = tracked_weapon_count + 1
    end

    if target_track then
        target_track.engaged_by = target_track.engaged_by or {}
        if #target_track.engaged_by < MAX_ENGAGEMENTS_PER_WEAPON then
            target_track.engaged_by[#target_track.engaged_by + 1] = {
                key = key,
                shot = shot.id,
                shooter = shot.shooter,
                shooter_type = shot.shooter_type,
                weapon = shot.weapon,
            }
        end
    end
    return shot
end

local function defender_summary(track)
    return {
        shot = track.shot,
        shooter = track.shooter,
        shooter_type = track.shooter_type,
        weapon = track.weapon_type,
    }
end

local function hit_detail(event)
    local weapon = event.weapon
    local track = find_track(weapon)
    if track == nil then
        if weapon == nil and not event.weapon_name then return nil end
        if weapon ~= nil and weapon_category(weapon) == shell_category() then return nil end
    end
    if #hit_events >= MAX_HIT_EVENTS then
        if track then track.hit = true end
        return nil
    end

    local hit = { time = timer.getTime() }
    if track then
        track.hit = true
        hit.shot = track.shot
        hit.weapon = track.weapon_type
    else
        hit.weapon = try_call(weapon, "getTypeName") or event.weapon_name
    end
    describe_object(event.initiator, "shooter", hit)
    local target = event.target
    if target ~= nil then
        describe_object(target, "target", hit)
        if is_weapon_object(target) then
            hit.target_is_weapon = true
            local target_track = find_track(target)
            if target_track then
                hit.target_shot = target_track.shot
                if track then target_track.intercepted_by = defender_summary(track) end
            end
        end
    end
    hit_events[#hit_events + 1] = hit
    return hit
end

-- A weapon that killed something hit it, even without an S_EVENT_HIT.
local function note_kill_weapon(event)
    local track = find_track(event.weapon)
    if track then track.hit = true end
end

local function resolve_weapon_fate(track)
    local fate = {
        shot = track.shot,
        weapon = track.weapon_type,
        time = track.ended,
        x = track.x,
        z = track.z,
        alt = track.alt,
    }
    if track.hit then
        fate.fate = "hit"
    else
        local by = track.intercepted_by
        if by == nil and track.engaged_by then
            local best_gap = nil
            for _, defender in ipairs(track.engaged_by) do
                local ended = defender.key and ended_weapons[defender.key]
                if ended and track.ended then
                    local gap = math.abs(ended - track.ended)
                    if gap <= INTERCEPT_WINDOW and (best_gap == nil or gap < best_gap) then
                        by, best_gap = defender, gap
                    end
                end
            end
        end
        if by then
            fate.fate = "intercepted"
            fate.intercepted_by = {
                shot = by.shot,
                shooter = by.shooter,
                shooter_type = by.shooter_type,
                weapon = by.weapon,
            }
        else
            fate.fate = "no_hit"
        end
    end
    if track.engaged_by then
        fate.engaged_by = {}
        for _, defender in ipairs(track.engaged_by) do
            fate.engaged_by[#fate.engaged_by + 1] = {
                shot = defender.shot,
                shooter = defender.shooter,
                shooter_type = defender.shooter_type,
                weapon = defender.weapon,
            }
        end
    end
    if #weapon_fates < MAX_WEAPON_FATES then
        weapon_fates[#weapon_fates + 1] = fate
        dirty_state = true
    end
    return fate
end

-- Waits for the defenders engaging a gone weapon to end too, so an intercept
-- where both vanish a poll apart is still matched.
local function fate_ready(track, now, flush)
    if flush or track.hit or track.intercepted_by or not track.engaged_by then
        return true
    end
    if now - track.ended >= INTERCEPT_WINDOW then return true end
    for _, defender in ipairs(track.engaged_by) do
        if defender.key and tracked_weapons[defender.key] then return false end
    end
    return true
end

-- Polls tracked weapons: keeps their last position while they exist, and gives
-- each one a fate once it is gone. flush resolves every pending fate now (at
-- mission end).
local function check_tracked_weapons(now, flush)
    for key, track in pairs(tracked_weapons) do
        if try_call(track.weapon, "isExist") then
            local point = point_of(track.weapon)
            if point then
                track.x, track.z, track.alt = point.x, point.z, point.y
            end
        else
            -- Assigning nil to an existing field while iterating with pairs is allowed.
            tracked_weapons[key] = nil
            tracked_weapon_count = tracked_weapon_count - 1
            track.ended = now
            track.weapon = nil
            ended_weapons[key] = now
            pending_fates[key] = track
        end
    end
    for key, track in pairs(pending_fates) do
        if fate_ready(track, now, flush) then
            pending_fates[key] = nil
            resolve_weapon_fate(track)
        end
    end
    for key, ended in pairs(ended_weapons) do
        if now - ended > ENDED_WEAPON_MEMORY then ended_weapons[key] = nil end
    end
end

local function weapon_feed_tick()
    local now = timer.getTime()
    local ok, check_error = pcall(check_tracked_weapons, now, false)
    if not ok and not weapon_feed_error_logged then
        weapon_feed_error_logged = true
        logger:error("Weapon feed check failed: " .. tostring(check_error))
    end
    return now + WEAPON_CHECK_INTERVAL
end

activeWeapons = {}
local function onEvent(event)
    if event.id == world.event.S_EVENT_CRASH and event.initiator then
        crash_events[#crash_events + 1] = event.initiator.getName(event.initiator)
        dirty_state = true
    end
   
    if event.id == world.event.S_EVENT_UNIT_LOST and event.initiator then
        unit_lost_events[#unit_lost_events + 1] = event.initiator.getName(event.initiator)
        dirty_state = true
    end
	
	if event.id == world.event.S_EVENT_KILL and event.target then
        kill_events[#kill_events + 1] = event.target.getName(event.target)
        local ok, detail = pcall(kill_detail, event)
        if ok and detail then
            kill_details[#kill_details + 1] = detail
        end
        pcall(note_kill_weapon, event)
        dirty_state = true
    end

    if event.id == world.event.S_EVENT_SHOT then
        local ok, shot = pcall(shot_detail, event)
        if ok and shot then
            dirty_state = true
        end
    end

    if event.id == world.event.S_EVENT_HIT then
        local ok, hit = pcall(hit_detail, event)
        if ok and hit then
            dirty_state = true
        end
    end

    if event.id == world.event.S_EVENT_DEAD and event.initiator and event.initiator.getName then
        dead_events[#dead_events + 1] = event.initiator.getName(event.initiator)
        local position = event.initiator.getPosition(event.initiator)
        local destruction = {}
        destruction.x = position.p.x
        destruction.y = position.p.y
        destruction.z = position.p.z
        destruction.type = event.initiator:getTypeName()
        destruction.orientation = mist.getHeading(event.initiator) * 57.3
        -- Only track actual units/buildings, not debris/crash models
        if destruction.type ~= nil and 
           string.find(destruction.type, "GENERIC_CRASH_MODEL") == nil and
           string.find(destruction.type, "_CRASH") == nil then
            destroyed_objects_positions[#destroyed_objects_positions + 1] = destruction
        end
        dirty_state = true
    end

    if event.id == world.event.S_EVENT_MISSION_END then
        pcall(check_tracked_weapons, timer.getTime(), true)
        mission_ended = true
        dirty_state = true
        if pcall(write_state) then
            dirty_state = false
        end
    end

end

mist.addEventHandler(onEvent)
timer.scheduleFunction(weapon_feed_tick, nil, timer.getTime() + WEAPON_CHECK_INTERVAL)

dirty_state = true
write_state_error_handling()

-- Escort leash
-- Escorts are kept within their engagement range relative to the escorted group.
-- This is driven by the mission-injected dcsRetribution.Escorts table.

local function escort_leash_get_group(id)
    local group_id = tonumber(id)
    if not group_id or group_id <= 0 then
        return nil
    end
    -- DCS has no Group.getByID; resolve the mission group id to a name via mist.
    local data = mist.DBs.groupsById and mist.DBs.groupsById[group_id]
    return data and Group.getByName(data.groupName) or nil
end

local function escort_leash_set_roe(group, roe)
    if not group then
        return
    end
    local controller = group:getController()
    if controller then
        controller:setOption(AI.Option.Air.id.ROE, roe)
    end
end

local function escort_leash_update()
    -- Keep running even if dcsRetribution data isn't available yet (trigger ordering)
    if not dcsRetribution or type(dcsRetribution.Escorts) ~= "table" then
        return timer.getTime() + 10
    end

    for _, pair in pairs(dcsRetribution.Escorts) do
        local escort_group = escort_leash_get_group(pair.escortGroupId)
        local escorted_group = escort_leash_get_group(pair.escortedGroupId)

        -- If the escorted group no longer exists (dead/despawned), ensure escort isn't stuck.
        if escort_group and not escorted_group then
            escort_leash_set_roe(escort_group, AI.Option.Air.val.ROE.OPEN_FIRE)
        elseif escort_group and escorted_group then
            local escort_unit = escort_group:getUnit(1)
            local escorted_unit = escorted_group:getUnit(1)
            if escort_unit and escorted_unit then
                local escort_pos = escort_unit:getPoint()
                local escorted_pos = escorted_unit:getPoint()
                local dx = escort_pos.x - escorted_pos.x
                local dz = escort_pos.z - escorted_pos.z
                local distance = math.sqrt(dx * dx + dz * dz)

                local max_dist = tonumber(pair.engagementRangeMeters) or 0
                if max_dist > 0 and distance > max_dist then
                    escort_leash_set_roe(escort_group, AI.Option.Air.val.ROE.RETURN_FIRE)
                else
                    escort_leash_set_roe(escort_group, AI.Option.Air.val.ROE.OPEN_FIRE)
                end
            end
        end
    end

    return timer.getTime() + 10
end

timer.scheduleFunction(escort_leash_update, nil, timer.getTime() + 1)
