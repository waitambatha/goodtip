"""How long a video runs, read from the file itself.

The story editor's slideshows take short videos, and the client set the limit:
"a short video, less than 45 seconds". The editor checks that in the browser
before it uploads, which is a convenience — a limit only the browser knows is
not a rule. This is the rule.

There is no ffprobe on the server and nothing in the venv that reads video, so
this reads the one number it needs by hand. MP4 and MOV — which is everything a
phone camera produces — are ISO base media files: a sequence of length-prefixed
boxes, and the `moov` box holds an `mvhd` ("movie header") whose timescale and
duration give the running time. Walking the top-level boxes to find it is a
couple of seeks, however large the file, because each box header says how far
to jump to the next one — including phone videos, which put `moov` after the
media data at the very end.

WebM is a different container (EBML) and is not read here: `video_seconds`
returns None for it, and the browser's check is the only one. That is the rare
case — WebM comes out of a desktop screen recorder, not a camera.
"""
import struct
from pathlib import Path


def video_seconds(upload) -> float | None:
    """Running time of an uploaded video in seconds, or None if unknown."""
    if Path(upload.name or "").suffix.lower() not in {".mp4", ".mov", ".m4v"}:
        return None
    try:
        return _mp4_seconds(upload)
    except (OSError, ValueError, struct.error):
        return None
    finally:
        try:
            upload.seek(0)
        except (OSError, ValueError):
            pass


def _mp4_seconds(f) -> float | None:
    f.seek(0, 2)
    end = f.tell()
    moov = _find_box(f, 0, end, b"moov")
    if not moov:
        return None
    mvhd = _find_box(f, moov[0], moov[1], b"mvhd")
    if not mvhd:
        return None
    f.seek(mvhd[0])
    version = f.read(4)[0]          # version byte, then three bytes of flags
    if version == 1:
        f.read(16)                  # creation and modification times, 64-bit
        timescale, duration = struct.unpack(">IQ", f.read(12))
    else:
        f.read(8)                   # the same two times, 32-bit
        timescale, duration = struct.unpack(">II", f.read(8))
    return duration / timescale if timescale else None


def _find_box(f, start: int, end: int, kind: bytes):
    """(payload start, payload end) of the first `kind` box in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return None
        size, name = struct.unpack(">I4s", header)
        head = 8
        if size == 1:               # a 64-bit size follows the name
            size = struct.unpack(">Q", f.read(8))[0]
            head = 16
        elif size == 0:             # "runs to the end of the file"
            size = end - pos
        if size < head:
            return None             # corrupt; stop rather than loop forever
        if name == kind:
            return pos + head, pos + size
        pos += size
    return None
