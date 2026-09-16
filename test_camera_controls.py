#!/usr/bin/env python3
"""Self-check for the v4l2-ctl control parser.

Fixture is verbatim output from the K2's camera on 2026-09-16. The parts worth
pinning are the ones that are easy to get wrong and silently wrong: bool
controls carry no min/max, menu entries belong to the control above them, and
`flags=inactive` decides whether a slider is usable.
"""
import os
os.environ.setdefault("K2_ROOT_PASS", "x")
import dashboard

SAMPLE = """\
                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=128
 white_balance_temperature_auto 0x0098090c (bool)   : default=1 value=1
                           gain 0x00980913 (int)    : min=0 max=100 step=1 default=5 value=5
           power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=1 value=1
\t\t\t\t0: Disabled
\t\t\t\t1: 50 Hz
\t\t\t\t2: 60 Hz
      white_balance_temperature 0x0098091a (int)    : min=0 max=255 step=1 default=128 value=128 flags=inactive
                  exposure_auto 0x009a0901 (menu)   : min=0 max=3 default=0 value=0
\t\t\t\t0: Auto Mode
\t\t\t\t1: Manual Mode
              exposure_absolute 0x009a0902 (int)    : min=0 max=6500 step=1 default=100 value=100 flags=inactive
"""


def check():
    got = {c["name"]: c for c in dashboard.parse_controls(SAMPLE)}
    assert len(got) == 7, sorted(got)

    b = got["brightness"]
    assert (b["type"], b["min"], b["max"], b["value"]) == ("int", 0, 255, 128), b
    assert b["inactive"] is False

    # bool lines carry no min/max at all - a naive parser gives them max=0 and
    # the checkbox can then never be turned on.
    wb = got["white_balance_temperature_auto"]
    assert (wb["type"], wb["min"], wb["max"], wb["value"]) == ("bool", 0, 1, 1), wb

    # menu entries attach to the control above them, and only to menus
    assert got["power_line_frequency"]["menu"] == {"0": "Disabled", "1": "50 Hz", "2": "60 Hz"}
    assert got["exposure_auto"]["menu"] == {"0": "Auto Mode", "1": "Manual Mode"}
    assert "menu" not in got["brightness"]

    # the flag that decides whether the UI lets you touch it
    assert got["exposure_absolute"]["inactive"] is True
    assert got["white_balance_temperature"]["inactive"] is True
    assert got["gain"]["inactive"] is False

    # a wide range must not be truncated - exposure goes to 6500, not 650
    assert got["exposure_absolute"]["max"] == 6500

    assert dashboard.parse_controls("") == []
    assert dashboard.parse_controls("total garbage\nno controls here") == []
    print("v4l2 control parser: all cases ok")


if __name__ == "__main__":
    check()
