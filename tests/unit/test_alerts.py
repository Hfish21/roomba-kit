from datetime import UTC, datetime

import pytest

from roomba_kit.core.events import EventType
from roomba_kit.core.models import Activity, RobotState
from roomba_kit.service.alerts import SETTLE_S, DockSearchMonitor

I5 = "0" * 31 + "1"


class Recorder:
    def __init__(self):
        self.alerts = []

    async def publish(self, type_, robot_id, data, at=None):
        assert type_ is EventType.ALERT_DOCK_SEARCH_TIMEOUT
        self.alerts.append((robot_id, data.mission_id, data.minutes))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def setup():
    bus, clock, mission = Recorder(), Clock(), {"id": "M1"}
    monitor = DockSearchMonitor(bus, lambda: 5, lambda robot_id: mission["id"], now=clock)
    return monitor, bus, clock, mission


def _state(activity):
    return RobotState(robot_id=I5, at=datetime.now(UTC), activity=activity)


async def _at(monitor, clock, t, activity=None):
    clock.t = t
    if activity is not None:
        monitor.observe(_state(activity))
    await monitor.check()


async def test_fires_once_per_mission_after_the_threshold(setup):
    monitor, bus, clock, mission = setup
    await _at(monitor, clock, 0, Activity.RETURNING)
    await _at(monitor, clock, 299)
    assert bus.alerts == []
    await _at(monitor, clock, 300)
    await _at(monitor, clock, 900)
    assert bus.alerts == [(I5, "M1", 5)]
    # Docks, then a later return in the same mission doesn't alert again.
    await _at(monitor, clock, 901, Activity.CHARGING)
    await _at(monitor, clock, 901 + SETTLE_S)
    await _at(monitor, clock, 1000, Activity.RETURNING)
    await _at(monitor, clock, 2000)
    assert len(bus.alerts) == 1
    # A new mission gets its own alert.
    mission["id"] = "M2"
    await _at(monitor, clock, 2001, Activity.RETURNING)
    await _at(monitor, clock, 2001 + 300)
    assert bus.alerts[-1] == (I5, "M2", 5)


async def test_brief_flickers_out_of_returning_keep_the_search(setup):
    monitor, bus, clock, _ = setup
    await _at(monitor, clock, 0, Activity.RETURNING)
    await _at(monitor, clock, 100, Activity.CLEANING)  # a `run` blip on dock contact
    await _at(monitor, clock, 101, Activity.RETURNING)
    await _at(monitor, clock, 300)
    assert bus.alerts == [(I5, "M1", 5)]


async def test_a_real_stop_ends_the_search(setup):
    monitor, bus, clock, _ = setup
    await _at(monitor, clock, 0, Activity.RETURNING)
    await _at(monitor, clock, 100, Activity.PAUSED)
    await _at(monitor, clock, 100 + SETTLE_S)
    await _at(monitor, clock, 200, Activity.RETURNING)
    await _at(monitor, clock, 450)
    assert bus.alerts == []  # only 250 s of the new search so far
    await _at(monitor, clock, 500)
    assert bus.alerts == [(I5, "M1", 5)]


async def test_no_alert_while_away_from_returning(setup):
    monitor, bus, clock, _ = setup
    await _at(monitor, clock, 0, Activity.RETURNING)
    await _at(monitor, clock, 295, Activity.UNKNOWN)
    await _at(monitor, clock, 302)  # left 7 s ago, within the settle window: hold off
    assert bus.alerts == []
