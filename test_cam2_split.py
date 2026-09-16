#!/usr/bin/env python3
"""Self-check for the MJPEG frame splitter.

Framing is where a fan-out goes quietly wrong: hand a viewer half a JPEG and
every frame after it is corrupt, which looks like a camera fault rather than a
parser bug. These cases are the ones that actually happen on a byte stream -
frames split across reads, several frames in one read, and preamble that is not
part of any frame.
"""
import os
os.environ.setdefault("K2_ROOT_PASS", "x")
import dashboard

SOI, EOI = b"\xff\xd8", b"\xff\xd9"
def frame(n):  return SOI + b"body%d" % n + EOI
def part(n):   return b"--b\r\nContent-Type: image/jpeg\r\n\r\n" + frame(n) + b"\r\n"


def check():
    got = []
    # whole frames arriving together
    left = dashboard.cam2_split(part(1) + part(2), got.append)
    assert got == [frame(1), frame(2)], got
    assert left == b"\r\n", left

    # a frame split across two reads must not be emitted twice or truncated
    got.clear()
    buf = dashboard.cam2_split(part(3)[:20], got.append)
    assert got == [], got
    buf = dashboard.cam2_split(buf + part(3)[20:], got.append)
    assert got == [frame(3)], got

    # preamble before the first SOI is dropped, not prepended to the frame
    got.clear()
    dashboard.cam2_split(b"HTTP junk preamble" + part(4), got.append)
    assert got == [frame(4)], got

    # no complete frame yet -> hold, do not emit
    got.clear()
    held = dashboard.cam2_split(SOI + b"incomplete", got.append)
    assert got == [] and held.startswith(SOI), held

    # a source that never completes a frame must not grow the buffer forever
    assert dashboard.cam2_split(SOI + b"x" * 5_000_000, got.append) == b""

    print("mjpeg splitter: all cases ok")


if __name__ == "__main__":
    check()
