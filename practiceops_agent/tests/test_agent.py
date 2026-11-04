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
