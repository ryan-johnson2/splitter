"""Tests for WebSocket protocol models."""

from __future__ import annotations

from velocidrone_ws.models import (
    ActivateErrorEvent,
    CountdownEvent,
    Event,
    FinishGateEvent,
    ImuEvent,
    Pilot,
    PilotListEvent,
    PlayerEvent,
    PlayerRaceData,
    RaceDataEvent,
    RaceStatusEvent,
    RaceTypeEvent,
    SessionEvent,
    SpectatorChangeEvent,
)


class TestSessionEvent:
    def test_from_json(self) -> None:
        data = {
            "playerName": "TestPilot",
            "sessionName": "MyRoom",
            "sceneryTitle": "Basketball Stadium",
            "trackName": "Test Track",
            "raceLength": 3,
            "RaceMode": "MultiGP",
            "quadType": "Five Inch",
            "quadSize": "5",
        }
        event = SessionEvent.from_json(data)
        assert event.player_name == "TestPilot"
        assert event.session_name == "MyRoom"
        assert event.scenery_title == "Basketball Stadium"
        assert event.track_name == "Test Track"
        assert event.race_length == 3
        assert event.race_mode == "MultiGP"
        assert event.quad_type == "Five Inch"
        assert event.quad_size == "5"

    def test_from_json_defaults(self) -> None:
        event = SessionEvent.from_json({})
        assert event.player_name == ""
        assert event.race_length == 0


class TestCountdownEvent:
    def test_from_json(self) -> None:
        event = CountdownEvent.from_json({"countValue": 3})
        assert event.count_value == 3

    def test_from_json_default(self) -> None:
        event = CountdownEvent.from_json({})
        assert event.count_value == 0


class TestFinishGateEvent:
    def test_from_json_true(self) -> None:
        event = FinishGateEvent.from_json({"StartFinishGate": True})
        assert event.start_finish_gate is True

    def test_from_json_false(self) -> None:
        event = FinishGateEvent.from_json({"StartFinishGate": False})
        assert event.start_finish_gate is False


class TestPlayerEvent:
    def test_from_json(self) -> None:
        data = {
            "PlayerName": "TestPilot",
            "playerColour": "#FF0000",
            "playerFlying": True,
            "raceManager": False,
        }
        event = PlayerEvent.from_json(data)
        assert event.player_name == "TestPilot"
        assert event.player_colour == "#FF0000"
        assert event.player_flying is True
        assert event.race_manager is False


class TestRaceTypeEvent:
    def test_from_json(self) -> None:
        data = {"raceMode": "MultiGP", "raceFormat": "Fastest Lap", "raceLaps": 3}
        event = RaceTypeEvent.from_json(data)
        assert event.race_mode == "MultiGP"
        assert event.race_format == "Fastest Lap"
        assert event.race_laps == 3


class TestSpectatorChangeEvent:
    def test_bare_string_payload(self) -> None:
        event = Event.from_json({"spectatorChange": "Dacus"})
        assert event.type == "spectatorChange"
        assert isinstance(event.data, SpectatorChangeEvent)
        assert event.data.player_name == "Dacus"


class TestRaceStatusEvent:
    def test_start(self) -> None:
        event = RaceStatusEvent.from_json({"raceAction": "start"})
        assert event.is_start and not event.is_abort and not event.is_finished

    def test_abort(self) -> None:
        assert RaceStatusEvent.from_json({"raceAction": "abort"}).is_abort

    def test_race_finished(self) -> None:
        assert RaceStatusEvent.from_json({"raceAction": "race finished"}).is_finished


class TestWireStrings:
    """1.17.13 stringifies every race-event scalar; parsers must accept both forms."""

    def test_stringified_scalars(self) -> None:
        prd = PlayerRaceData.from_json(
            {
                "position": "1",
                "lap": "2",
                "gate": "5",
                "time": "69.711",
                "finished": "False",
                "colour": "00FFFF",
                "uid": 12345,
            }
        )
        assert prd.position == 1 and prd.lap == 2 and prd.gate == 5
        assert prd.finished is False
        assert prd.uid == "12345"
        assert prd.time_seconds == 69.711
        assert prd.time_ms == 69711

    def test_capitalised_true(self) -> None:
        assert PlayerRaceData.from_json({"finished": "True"}).finished is True
        assert FinishGateEvent.from_json({"StartFinishGate": "True"}).start_finish_gate

    def test_countdown_string(self) -> None:
        event = CountdownEvent.from_json({"countValue": "0"})
        assert event.count_value == 0 and event.is_go

    def test_racetype_strings(self) -> None:
        event = RaceTypeEvent.from_json(
            {"raceMode": "THREE_LAP_SINGLE_CLASS", "raceFormat": "NORMAL", "raceLaps": "3"}
        )
        assert event.race_laps == 3

    def test_garbage_ints_default(self) -> None:
        assert PlayerRaceData.from_json({"lap": "x"}).lap == 0


class TestImuEvent:
    def test_from_json(self) -> None:
        msg = {
            "imu": {
                "roll": 1.23,
                "pitch": -0.5,
                "yaw": 0.01,
                "PositionX": 1.0,
                "PositionY": 2.0,
                "PositionZ": 3.0,
                "AttitudeX": 0.0,
                "AttitudeY": 0.0,
                "AttitudeZ": 0.0,
                "AttitudeW": 1.0,
                "SpeedX": 3.0,
                "SpeedY": 0.0,
                "SpeedZ": 4.0,
                "timestamp": 123456.78,
            }
        }
        event = Event.from_json(msg)
        assert event.type == "imu"
        assert isinstance(event.data, ImuEvent)
        assert event.data.position == (1.0, 2.0, 3.0)
        assert event.data.attitude == (0.0, 0.0, 0.0, 1.0)
        assert event.data.speed_magnitude == 5.0
        assert event.data.timestamp == 123456.78

    def test_comma_decimal_locale(self) -> None:
        event = ImuEvent.from_json({"SpeedX": "1,5"})
        assert event.speed == (1.5, 0.0, 0.0)

    def test_wrong_shape_is_untyped(self) -> None:
        event = Event.from_json({"imu": "nope"})
        assert event.type == "imu" and event.data is None


class TestPlayerRaceData:
    def test_from_json(self) -> None:
        data = {
            "position": 1,
            "lap": 2,
            "gate": 5,
            "time": "01:23.456",
            "finished": False,
            "colour": "#00FF00",
            "uid": "abc123",
        }
        prd = PlayerRaceData.from_json(data)
        assert prd.position == 1
        assert prd.lap == 2
        assert prd.gate == 5
        assert prd.time == "01:23.456"
        assert prd.finished is False
        assert prd.colour == "#00FF00"
        assert prd.uid == "abc123"


class TestRaceDataEvent:
    def test_from_json(self) -> None:
        data = {
            "Player1": {
                "position": 1,
                "lap": 3,
                "gate": 8,
                "time": "01:00.000",
                "finished": True,
                "colour": "#FF0000",
                "uid": "uid1",
            },
            "Player2": {
                "position": 2,
                "lap": 2,
                "gate": 6,
                "time": "01:15.500",
                "finished": False,
                "colour": "#0000FF",
                "uid": "uid2",
            },
        }
        event = RaceDataEvent.from_json(data)
        assert len(event.players) == 2
        assert event.players["Player1"].position == 1
        assert event.players["Player1"].finished is True
        assert event.players["Player2"].lap == 2

    def test_from_json_empty(self) -> None:
        event = RaceDataEvent.from_json({})
        assert len(event.players) == 0


class TestPilot:
    def test_from_json(self) -> None:
        pilot = Pilot.from_json({"name": "TestPilot", "uid": "abc123"})
        assert pilot.name == "TestPilot"
        assert pilot.uid == "abc123"


class TestPilotListEvent:
    def test_from_json(self) -> None:
        data = [
            {"name": "Pilot1", "uid": "uid1"},
            {"name": "Pilot2", "uid": "uid2"},
        ]
        event = PilotListEvent.from_json(data)
        assert len(event.pilots) == 2
        assert event.pilots[0].name == "Pilot1"
        assert event.pilots[1].uid == "uid2"

    def test_from_json_empty(self) -> None:
        event = PilotListEvent.from_json([])
        assert len(event.pilots) == 0


class TestActivateErrorEvent:
    def test_from_json(self) -> None:
        event = ActivateErrorEvent.from_json({"UIDNotFound": "bad_uid"})
        assert event.uid_not_found == "bad_uid"


class TestEventParsing:
    def test_session_event(self) -> None:
        msg = {
            "session": {
                "playerName": "Test",
                "sessionName": "Room",
                "sceneryTitle": "Scene",
                "trackName": "Track",
                "raceLength": 3,
                "RaceMode": "MultiGP",
                "quadType": "5inch",
                "quadSize": "5",
            }
        }
        event = Event.from_json(msg)
        assert event.type == "session"
        assert isinstance(event.data, SessionEvent)
        assert event.data.player_name == "Test"

    def test_countdown_event(self) -> None:
        msg = {"countdown": {"countValue": 2}}
        event = Event.from_json(msg)
        assert event.type == "countdown"
        assert isinstance(event.data, CountdownEvent)
        assert event.data.count_value == 2

    def test_racedata_event(self) -> None:
        msg = {
            "racedata": {
                "Player1": {
                    "position": 1,
                    "lap": 1,
                    "gate": 3,
                    "time": "00:30.000",
                    "finished": False,
                    "colour": "#FF0000",
                    "uid": "uid1",
                }
            }
        }
        event = Event.from_json(msg)
        assert event.type == "racedata"
        assert isinstance(event.data, RaceDataEvent)
        assert "Player1" in event.data.players

    def test_pilotlist_event(self) -> None:
        msg = {"pilotlist": [{"name": "P1", "uid": "u1"}]}
        event = Event.from_json(msg)
        assert event.type == "pilotlist"
        assert isinstance(event.data, PilotListEvent)
        assert len(event.data.pilots) == 1

    def test_finish_gate_event(self) -> None:
        msg = {"FinishGate": {"StartFinishGate": True}}
        event = Event.from_json(msg)
        assert event.type == "FinishGate"
        assert isinstance(event.data, FinishGateEvent)
        assert event.data.start_finish_gate is True

    def test_activate_error_event(self) -> None:
        msg = {"ActivateError": {"UIDNotFound": "bad"}}
        event = Event.from_json(msg)
        assert event.type == "ActivateError"
        assert isinstance(event.data, ActivateErrorEvent)

    def test_unknown_event(self) -> None:
        msg = {"unknownType": {"foo": "bar"}}
        event = Event.from_json(msg)
        assert event.type == "unknownType"
        assert event.data is None
        assert event.raw == msg

    def test_raw_preserved(self) -> None:
        msg = {"countdown": {"countValue": 1}}
        event = Event.from_json(msg)
        assert event.raw == msg
