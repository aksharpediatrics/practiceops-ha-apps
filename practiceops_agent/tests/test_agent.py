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
