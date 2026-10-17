"""Dialog window for editing flights."""

from typing import Any

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
)

from game.ato.flight import Flight
from game.server import EventStream
from game.sim import GameUpdateEvents
from qt_ui.models import GameModel, PackageModel
from qt_ui.uiconstants import EVENT_ICONS
from qt_ui.windows.mission.flight.QFlightPlanner import QFlightPlanner


def flight_fingerprint(flight: Flight) -> tuple[Any, ...]:
    """What the player can change about a flight in the edit dialog.

    Compared before and after the dialog to tell whether the player edited the
    flight (which locks its package against Claude commanders) or only looked.
    """
    waypoints = tuple(
        (
            w.waypoint_type.name,
            round(w.position.x),
            round(w.position.y),
            round(w.alt.meters),
            w.alt_type,
            w.display_name,
        )
        for w in flight.flight_plan.waypoints
    )
    members = tuple(
        (
            id(m.pilot),
            m.is_player,
            m.loadout.name,
            tuple(
                sorted(
                    (n, w.clsid if w is not None else None)
                    for n, w in m.loadout.pylons.items()
                )
            ),
            repr(sorted(m.loadout.pylon_settings.items())),
            m.livery,
            repr(m.properties),
            str(m.tgp_laser_code),
            str(m.weapon_laser_code),
        )
        for m in flight.iter_members()
    )
    return (
        id(flight.squadron),
        flight.flight_type,
        flight.start_type,
        flight.custom_name,
        flight.divert.name if flight.divert is not None else None,
        round(flight.fuel),
        str(flight.frequency),
        str(getattr(flight, "tacan", None)),
        repr(flight.props),
        flight.flight_plan.tot_offset,
        type(flight.flight_plan).__name__,
        waypoints,
        members,
    )


class QEditFlightDialog(QDialog):
    """Dialog window for editing flight plans and loadouts."""

    def __init__(
        self,
        game_model: GameModel,
        package_model: PackageModel,
        flight: Flight,
        parent=None,
    ) -> None:
        super().__init__(parent=parent)

        self.game_model = game_model
        self.flight = flight
        self.package_model = package_model
        self.events = GameUpdateEvents()
        self.initial_fingerprint = flight_fingerprint(flight)

        self.setWindowTitle("Edit flight")
        self.setWindowIcon(EVENT_ICONS["strike"])
        self.setModal(True)

        layout = QVBoxLayout()

        self.flight_planner = QFlightPlanner(package_model, flight, game_model)
        self.flight_planner.squadron_changed.connect(self.on_squadron_change)
        layout.addWidget(self.flight_planner)

        self.setLayout(layout)
        self.finished.connect(self.on_close)

    def on_squadron_change(self, flight: Flight):
        self.package_model.lock()
        self.events = GameUpdateEvents().delete_flight(self.flight)
        self.events = self.events.new_flight(flight)
        self.game_model.ato_model.client_slots_changed.emit()
        self.flight = flight
        self.reject()
        new_dialog = QEditFlightDialog(
            self.game_model, self.package_model, flight, self.parent()
        )
        new_dialog.show()

    def on_close(self, _result) -> None:
        if flight_fingerprint(self.flight) != self.initial_fingerprint:
            self.package_model.lock()
        self.events = self.events.update_flight(self.flight)
        EventStream.put_nowait(self.events)
        self.game_model.ato_model.client_slots_changed.emit()
