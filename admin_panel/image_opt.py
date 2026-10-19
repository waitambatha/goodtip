"""Shrinking a story's pictures before they ever reach disk.

Every image the story editor had ever taken — featured pictures, body
pictures, OG cards — went straight from whatever the browser uploaded onto
disk unchanged (news_upload_image), which for an AI-generated or
screenshotted PNG is routinely 1-2MB at a resolution nobody's screen shows
it at. Nine of those loading at once (NEWS_PAGE_ROWS x ROW_CARDS) is the
real cost behind "the blog is loading too long" (client, 19 Oct 2026) —
more than any query on the page.

A story picture is a graphic, not a photograph that needs losslessness, and
nothing here needs to keep transparency either — alpha gets flattened onto
white rather than carried through, because every place a story's picture
appears already sits on a plain card background.
"""

import io

from django.core.files.base import ContentFile
from PIL import Image

#: Wide enough for the detail page's full-bleed hero gallery, generous for
#: a 240px-tall deck card. Nothing on this site shows a story picture wider
#: than this, so there is nothing a bigger original buys except bytes.
MAX_DIMENSION = 1600
JPEG_QUALITY = 82


def optimize_image(django_file, *, name: str | None = None):
    """Re-encode an uploaded image as a resized, compressed JPEG.

    Returns a new ContentFile ready for storage, or None if the upload
    isn't something Pillow can open (caller keeps the original rather than
    losing the upload over a format it doesn't need to touch).
    """
    try:
        django_file.seek(0)
        img = Image.open(django_file)
        img.load()
    except Exception:
        return None

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[-1])
        img = flat
    else:
        img = img.convert("RGB")

    if img.width > MAX_DIMENSION or img.height > MAX_DIMENSION:
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    buf.seek(0)

    out_name = name or getattr(django_file, "name", "image")
    stem = out_name.rsplit(".", 1)[0] if "." in out_name else out_name
    return ContentFile(buf.read(), name=f"{stem}.jpg")
