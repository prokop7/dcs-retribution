import pytest
import re
from types import SimpleNamespace
from datetime import date
from pathlib import Path

from dcs.planes import F_16C_50

from game.ato.loadouts import Loadout
from game.data.weapons import (
    DEFAULT_RANGE_NM_BY_KIND,
    Weapon,
    WeaponGroup,
    WeaponKind,
    WeaponType,
)


def _bare_weapon(clsid: str) -> Weapon:
    group = WeaponGroup(
        name=f"test-{clsid}",
        type=WeaponType.UNKNOWN,
        introduction_year=None,
        fallback_name=None,
    )
    return Weapon(clsid=clsid, weapon_group=group)


@pytest.mark.parametrize(
    "clsid",
    [
        "{LAU-131 - 7 AGR-20A}",
        "{LAU-131 - 7 AGR-20 M282}",
        "{BRU-32 GBU-12}",
        "DIS_GBU_12",
    ],
)
def test_accepts_laser_code_true_for_laser_guided_weapons(clsid: str) -> None:
    assert _bare_weapon(clsid).accepts_laser_code() is True


@pytest.mark.parametrize(
    "clsid",
    [
        "<CLEAN>",
        "{AUF2_MK82}",
        "definitely-not-a-real-clsid",
    ],
)
def test_accepts_laser_code_false_for_non_laser_or_unknown(clsid: str) -> None:
    assert _bare_weapon(clsid).accepts_laser_code() is False


def test_aaq_33_has_era_data_and_degrades_on_pre_intro_f16() -> None:
    weapon = Weapon.with_clsid("{AN_AAQ_33}")

    assert weapon is not None
    assert weapon.weapon_group.name == "AN/AAQ-33 - Advanced Targeting Pod"
    assert weapon.weapon_group.introduction_year == 2005

    faction = SimpleNamespace(weapons_introduction_year_overrides={})
    assert weapon.available_on(date(2004, 1, 1), faction) is False  # type: ignore[arg-type]
    assert weapon.available_on(date(2005, 1, 1), faction) is True  # type: ignore[arg-type]

    loadout = Loadout("Test", {11: weapon}, date=None)
    degraded = loadout.degrade_for_date(
        SimpleNamespace(dcs_unit_type=F_16C_50), date(2004, 1, 1), faction  # type: ignore[arg-type]
    )

    degraded_weapon = degraded.pylons.get(11)
    assert degraded_weapon is not None
    assert degraded_weapon.weapon_group.name == "AN/AAQ-28 LITENING"


@pytest.mark.parametrize(
    ("clsid", "group_name", "introduction_year"),
    [
        ("{F-15E_AAQ-33_XR_ATP-SE}", "AN/AAQ-33 - Advanced Targeting Pod", 2005),
        ("{F-15E_AAQ-28_LITENING}", "AN/AAQ-28 LITENING", 1999),
        ("{SUPERHORNET_PYLON_05_TP_ASQ228}", "AN/ASQ-228 ATFLIR", 2003),
        ("{SUPERHORNET_PYLON_06_CN_TP_AAQ28}", "AN/AAQ-28 LITENING", 1999),
        ("_NiteHawk_FLIR", "AN/AAS-38 Nite Hawk", 1984),
        ("{HB_PAVE_SPIKE_FAST_TRACK}", "AN/AVQ-23 Pave Spike", 1974),
        ("{HB_PAVE_SPIKE_FAST_ON_ADAPTER_IN_AERO7}", "AN/AVQ-23 Pave Spike", 1974),
        ("{JAS39_Litening}", "AN/AAQ-28 LITENING", 1999),
        ("{JAS39_FLIR}", "AN/AAQ-28 LITENING", 1999),
        ("{LITENING_POD}", "AN/AAQ-28 LITENING", 1999),
        ("{DAMOCLES}", "DAMOCLES", 2009),
        ("{F111C_FLIR}", "AN/AVQ-26 Pave Tack", 1982),
        ("DIS_WMD7", "AVIC WMD-7", 2007),
    ],
)
def test_targeting_pod_variants_have_era_data(
    clsid: str, group_name: str, introduction_year: int
) -> None:
    weapon = Weapon.with_clsid(clsid)

    assert weapon is not None
    assert weapon.weapon_group.type is WeaponType.TGP
    assert weapon.weapon_group.name == group_name
    assert weapon.weapon_group.introduction_year == introduction_year


def test_custom_payload_targeting_pods_do_not_fall_back_to_unknown() -> None:
    payload_clsids: set[str] = set()
    for path in Path("resources/customized_payloads").glob("*.lua"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        payload_clsids.update(re.findall(r'\["CLSID"\]\s*=\s*"([^"]+)"', text))

    def is_targeting_pod(weapon: Weapon) -> bool:
        name = weapon.name.upper()
        return "NAV POD" not in name and any(
            token in name
            for token in [
                "TARGETING POD",
                "ATFLIR",
                "LITENING",
                "LANTIRN",
                "SNIPER",
                "DAMOCLES",
                "WMD7",
                "FLIR/LDT POD",
                "TARGETING POD FLIR",
            ]
        )

    targeting_pods = [
        w
        for clsid in sorted(payload_clsids)
        if (w := Weapon.with_clsid(clsid)) is not None and is_targeting_pod(w)
    ]

    assert targeting_pods
    assert all(weapon.weapon_group.type is WeaponType.TGP for weapon in targeting_pods)
    assert all(
        weapon.weapon_group.introduction_year is not None for weapon in targeting_pods
    )


def _yaml_group_names(folder: str) -> list[str]:
    import yaml

    names = []
    for path in sorted(Path("resources/weapons", folder).glob("*.yaml")):
        with path.open(encoding="utf8") as group_file:
            names.append(yaml.safe_load(group_file)["name"])
    return names


def test_every_standoff_weapon_has_reach_data() -> None:
    names = _yaml_group_names("standoff")
    assert len(names) > 100
    missing = [
        name
        for name in names
        if WeaponGroup.named(name).range_nm is None
        or WeaponGroup.named(name).kind is None
    ]
    assert not missing, f"standoff weapons without kind/range_nm: {missing}"


def test_guided_bombs_have_reach_and_unguided_bombs_default_by_kind() -> None:
    jdam = WeaponGroup.named("GBU-31(V)3/B")
    assert jdam.kind is WeaponKind.GUIDED_BOMB
    assert jdam.range_nm is not None and 10 <= jdam.range_nm <= 15
    for name in ("GBU-12", "GBU-38", "KAB-500Kr", "KAB-1500L"):
        assert WeaponGroup.named(name).range_nm is not None, name
    mk82 = WeaponGroup.named("Mk 82")
    assert mk82.kind is WeaponKind.UNGUIDED_BOMB
    assert mk82.range_nm is None
    assert mk82.reach_nm == DEFAULT_RANGE_NM_BY_KIND[WeaponKind.UNGUIDED_BOMB]
    assert all(
        WeaponGroup.named(n).kind is not None for n in _yaml_group_names("bombs")
    )


def test_reach_data_ranks_standoff_weapons() -> None:
    def reach(name: str) -> float:
        value = WeaponGroup.named(name).reach_nm
        assert value is not None
        return value

    assert reach("AGM-154C JSOW") > reach("GBU-31(V)3/B")
    assert reach("AGM-86D") > reach("AGM-158B JASSM-ER") > reach("AGM-84H SLAM-ER")
    assert reach("Kh-555") > reach("Kh-22 (AS-4 Kitchen)") > reach("Kh-59M")
    assert reach("Kh-29L") > reach("Kh-25ML")
    assert WeaponGroup.named("Kh-22 (AS-4 Kitchen)").kind is (
        WeaponKind.ANTI_SHIP_MISSILE
    )
    assert WeaponGroup.named("AGM-88C HARM").kind is (WeaponKind.ANTI_RADIATION_MISSILE)
    # Groups without data have no reach.
    assert WeaponGroup.named("AIM-120C").reach_nm is None


def test_weapon_groups_refresh_reach_data_on_load() -> None:
    import pickle

    group = WeaponGroup.named("AGM-154C JSOW")
    state = dict(group.__dict__)
    # A save from before reach data existed.
    del state["kind"]
    del state["range_nm"]
    old = WeaponGroup.__new__(WeaponGroup)
    old.__setstate__(state)
    assert old.kind is WeaponKind.GLIDE_WEAPON
    assert old.range_nm == group.range_nm
    assert pickle.loads(pickle.dumps(group)).range_nm == group.range_nm
