"""A stored profile photo is always upright, square and 640px (client, 29 Oct
2026: "make sure it does not get distorted")."""
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image

from .forms import AvatarForm


def _upload(w, h, fmt="PNG"):
    buf = BytesIO()
    Image.new("RGB", (w, h), (20, 120, 60)).save(buf, fmt)
    return SimpleUploadedFile(f"wide.{fmt.lower()}", buf.getvalue(), content_type=f"image/{fmt.lower()}")


class AvatarSquareTests(TestCase):
    def test_a_wide_screenshot_is_stored_as_a_square_jpeg(self):
        out = AvatarForm._square(_upload(1600, 400))
        out.seek(0)
        im = Image.open(out)
        self.assertEqual(im.size, (640, 640))
        self.assertEqual(im.format, "JPEG")
        self.assertTrue(out.name.endswith(".jpg"))

    def test_something_that_is_not_a_picture_is_refused(self):
        from django import forms
        bad = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
        with self.assertRaises(forms.ValidationError):
            AvatarForm._square(bad)
