#!/usr/bin/env python3
"""Self-check for the camera triage.

The point of camera_health is that each failure maps to a DIFFERENT fix, so the
only thing worth testing is that the probe output is read correctly and that the
crash-loop case is told apart from a plain stopped daemon. Run: python3 test_camera_health.py
"""
import os
os.environ.setdefault("K2_ROOT_PASS", "x")     # so printer_ssh gets past its guard
import dashboard

CASES = {
    # cam_app dead AND its config truncated: the segfault loop. Must be named
    # specifically, because "just restart it" is what does not work here.
    "app=down rtc=up ver=bad":  ("crashed", True),
    # dead but config intact: a restart really is enough
    "app=down rtc=up ver=ok":   ("not running", True),
    "app=up rtc=down ver=ok":   ("webrtc_local", True),
    # both daemons up: the printer is fine, so stop blaming it
    "app=up rtc=up ver=ok":     ("running", False),
}

def check():
    for probe, (needle, expect_fix) in CASES.items():
        dashboard.printer_ssh = lambda cmd, timeout=30, _p=probe: _p
        h = dashboard.camera_health()
        assert needle in h["cause"] or needle in h["detail"], (probe, h)
        assert h["fix"] is expect_fix, (probe, h)
        assert h["ok"] is (not expect_fix), (probe, h)

    # A probe that says nothing must NOT be read as "everything is fine" - that
    # is inferring health from silence, and it is how a real fault gets dismissed.
    for empty in ("", "garbage", "app=down"):        # empty, unparsable, partial
        dashboard.printer_ssh = lambda cmd, timeout=30, _p=empty: _p
        h = dashboard.camera_health()
        assert h["ok"] is False, (empty, h)
    print("camera triage: all cases ok")

if __name__ == "__main__":
    check()
