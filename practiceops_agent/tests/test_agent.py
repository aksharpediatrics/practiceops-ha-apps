import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402

NY = agent.OFFICE


def test_office_hours():
    assert agent.in_office_hours(datetime(2026, 11, 2, 7, 45, tzinfo=NY))  # Monday open
    assert not agent.in_office_hours(datetime(2026, 11, 2, 7, 44, tzinfo=NY))
    assert not agent.in_office_hours(datetime(2026, 11, 2, 17, 15, tzinfo=NY))
    assert agent.in_office_hours(datetime(2026, 11, 7, 12, 14, tzinfo=NY))  # Saturday morning
    assert not agent.in_office_hours(datetime(2026, 11, 7, 12, 15, tzinfo=NY))
    assert not agent.in_office_hours(datetime(2026, 11, 8, 10, 0, tzinfo=NY))  # Sunday


def test_secrets_and_macs_never_leave():
    s = agent.Sanitizer("salt")
    out = s.clean({"data": [{"name": "Akshar", "x_passphrase": "wifi-secret", "mac": "AA-BB-CC-DD-EE-01",
                             "nested": {"ap_mac": "aa:bb:cc:dd:ee:01", "x_authkey": "k"}}]})
    row = out["data"][0]
    assert "x_passphrase" not in row and "x_authkey" not in row["nested"]
    assert row["mac"] == row["nested"]["ap_mac"] and row["mac"].startswith("mac-") and len(row["mac"]) == 16
    assert agent.Sanitizer("other").mac_token("aa:bb:cc:dd:ee:01") != row["mac"]  # salted
    assert "wifi-secret" not in str(out)


def test_temperature_averages():
    from datetime import timedelta, timezone
    t0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    m = lambda minutes: t0 + timedelta(minutes=minutes)  # noqa: E731
    points = [(m(-30), 4.0), (m(2), 6.0), (m(5), None), (m(12), 5.0)]  # 4.0 holds from before the start
    out = agent.averages(points, t0, m(15))
    assert out == [(m(2.5), 5.2),   # 2 min at 4 and 3 min at 6
                   (m(12.5), 5.0)]  # 5-10 is unavailable (skipped); 10-15 has 3 minutes at 5, enough
    assert agent.averages([(m(0), None), (m(3), 5.0)], t0, m(5)) == []  # only 2 of 5 minutes: skipped


def test_home_assistant_snapshot_leaves_people_and_places_out():
    states = [
        {"entity_id": "sensor.fridge", "state": "4.1", "attributes": {"friendly_name": "Fridge", "unit_of_measurement": "°C",
                                                                       "device_class": "temperature", "token": "x"}},
        {"entity_id": "person.someone", "state": "home", "attributes": {"latitude": 1}},
        {"entity_id": "device_tracker.phone", "state": "home"},
        {"entity_id": "zone.home", "state": "1"},
        {"entity_id": "update.core", "state": "on", "attributes": {"installed_version": "1", "latest_version": "2"}},
    ]
    out = agent.ha_snapshot(states, {"version": "2026.10.4", "location_name": "Office", "latitude": 40.1})
    assert out["config"] == {"version": "2026.10.4", "location_name": "Office"}
    assert [e["entity_id"] for e in out["entities"]] == ["sensor.fridge", "update.core"]
    assert out["entities"][0]["attributes"] == {} and out["entities"][0]["unit"] == "°C"
    assert "latitude" not in str(out)


def test_card_ids_without_tokens():
    a = agent.Access("https://console", "key")
    a.users = lambda: iter([{"id": "u1", "nfc_cards": [{"id": "100001", "token": "secret"}]}, {"id": "u2", "nfc_cards": []}])
    assert a.all_cards() == {"u1": ["100001"]}


def test_snapshot_carries_devices_and_never_camera_pictures():
    states = [{"entity_id": "camera.front", "state": "idle",
               "attributes": {"friendly_name": "Front", "entity_picture": "/api/camera_proxy/camera.front?token=t",
                              "access_token": "t"}},
              {"entity_id": "sensor.printer_black_toner", "state": "40", "attributes": {"unit_of_measurement": "%"}}]
    devices = {"camera.front": ["Front doorbell", "Entrance", "Ring", "Video Doorbell 4"],
               "sensor.printer_black_toner": ["Office printer", "Front desk", "Brother", "MFC-L2750DW"]}
    out = agent.ha_snapshot(states, {}, devices)
    cam, toner = out["entities"]
    assert cam["device"] == "Front doorbell" and cam["manufacturer"] == "Ring" and cam["attributes"] == {}
    assert "token" not in str(out) and toner["area"] == "Front desk" and toner["model"] == "MFC-L2750DW"
    assert "{% for s in states" in agent.DEVICES_TEMPLATE and "'camera'" in agent.DEVICES_TEMPLATE


def test_camera_sensors_are_found_by_device_or_name():
    states = [
        {"entity_id": "camera.front_door", "attributes": {"friendly_name": "Front Door"}},
        {"entity_id": "camera.back", "attributes": {"friendly_name": "Back"}},
        {"entity_id": "binary_sensor.front_door_motion", "attributes": {"device_class": "motion"}},
        {"entity_id": "event.front_door_ding", "attributes": {"friendly_name": "Front Door Ding"}},
        {"entity_id": "binary_sensor.back_sound", "attributes": {"friendly_name": "Back Sound", "device_class": "sound"}},
        {"entity_id": "sensor.front_door_last_motion", "attributes": {}},  # not a trigger (a timestamp sensor)
        {"entity_id": "binary_sensor.office_door", "attributes": {"device_class": "door"}},
    ]
    devices = {"camera.front_door": ["Front Door"], "binary_sensor.front_door_motion": ["Front Door"],
               "event.front_door_ding": ["Front Door"]}
    cameras, triggers = agent.camera_triggers(states, devices)
    assert cameras == {"camera.front_door": "Front Door", "camera.back": "Back"}
    assert sorted(triggers["camera.front_door"]) == [("binary_sensor.front_door_motion", "motion"),
                                                     ("event.front_door_ding", "ding")]
    assert triggers["camera.back"] == [("binary_sensor.back_sound", "sound")]
    assert agent.went_off({"entity_id": "binary_sensor.x", "state": "on"})
    assert not agent.went_off({"entity_id": "binary_sensor.x", "state": "off"})
    assert agent.went_off({"entity_id": "event.x", "state": "2026-11-04T15:00:00+00:00"})
    assert not agent.went_off({"entity_id": "event.x", "state": "unavailable"})


def test_readers_are_offered_even_when_unifi_labels_them_differently():
    a = agent.Access("https://console", "key")
    hub = {"id": "h1", "name": "UA-HUB-1", "type": "UAH"}
    g2 = {"id": "r1", "alias": "Front door", "type": "UA-G2-PRO"}
    a.call = lambda method, path, body=None, allow_failure=False: [[hub, g2]]
    assert [r["name"] for r in a.readers()] == ["Front door"]  # no capabilities listed: the hub is left out
    g3 = {"id": "r2", "alias": "Back", "type": "UA-G3", "capabilities": ["nfc_card", "pin_code"]}
    a.call = lambda method, path, body=None, allow_failure=False: [[hub, g3]]
    assert [r["name"] for r in a.readers()] == ["Back"]
    a.call = lambda method, path, body=None, allow_failure=False: [[hub]]
    assert [r["name"] for r in a.readers()] == ["UA-HUB-1"]  # better to offer something than nothing
