"""Test helpers that cannot destroy real data.

WHY THIS FILE EXISTS
--------------------
On 4 Sep 2026 the client reported blog and profile images 404ing. The uploads
were not misconfigured and nothing on the server was deleting them. This was:

    class MessageVideoTests(TestCase):        # no @override_settings
        @classmethod
        def tearDownClass(cls):
            shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)

With no MEDIA_ROOT override, `settings.MEDIA_ROOT` is the REAL uploads
directory of whatever checkout the suite is run in — and `rmtree` removed all
of it. Staging's deploy gate runs the full suite on every deploy, so every
deploy silently deleted every file anybody had uploaded since the last one.
Stories written on 1–3 Sep lost their pictures; staging's `media/` directory
was born again from nothing at 06:12 the next morning, when the first upload
after a deploy recreated it. The production checkout lost its directory the
same way and never got one back, because nothing has been uploaded there
since.

`ignore_errors=True` is what made it silent: no test failed, no log line, and
the damage was two directories away from anything anybody was looking at.

WHAT THIS FIXES, AND WHAT IT DOES NOT
-------------------------------------
`drop_temp_media` refuses to delete anything that is not under the system
temporary directory, and it raises rather than warning — a test that has
forgotten its override should fail loudly on the machine of whoever wrote it,
not quietly delete a member's avatar on a server three weeks later.

It does not bring the files back. They were removed, not archived.
"""
import shutil
import tempfile
from pathlib import Path

from django.conf import settings


def temp_media(prefix: str = "gt-test-media-") -> str:
    """A throwaway MEDIA_ROOT, for @override_settings(MEDIA_ROOT=temp_media()).

    Paired with drop_temp_media in tearDownClass. Using this rather than a bare
    tempfile.mkdtemp is what makes the pair greppable: one name to search for
    when asking "which tests write files, and where".
    """
    return tempfile.mkdtemp(prefix=prefix)


def drop_temp_media() -> None:
    """Remove the test's MEDIA_ROOT. Refuses to touch a real one.

    RAISES rather than skipping. A missing @override_settings is a bug in the
    test, and the person who can fix it cheaply is the one running the suite
    right now — not whoever eventually notices that a production directory has
    gone. The cost of getting this wrong is unrecoverable and the cost of a
    loud failure is thirty seconds.
    """
    root = Path(settings.MEDIA_ROOT).resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    if not root.is_relative_to(tmp):
        raise RuntimeError(
            f"Refusing to delete {root} — it is not a temporary directory.\n"
            "This test class deletes MEDIA_ROOT in tearDownClass but has no "
            "@override_settings(MEDIA_ROOT=temp_media()), so it was about to "
            "remove the real uploads directory of this checkout. Add the "
            "decorator. See goodtip/testing.py for what happened last time."
        )
    shutil.rmtree(root, ignore_errors=True)
