"""optimize_image — the resize/recompress step behind news_upload_image and
optimize_news_images, which between them are what "the blog is loading too
long... even the images" (client, 19 Oct 2026) actually fixes."""

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image

from .image_opt import MAX_DIMENSION, optimize_image


def _png(width, height, mode="RGB", color=(10, 20, 30)):
    buf = io.BytesIO()
    Image.new(mode, (width, height), color).save(buf, format="PNG")
    buf.seek(0)
    return SimpleUploadedFile("pic.png", buf.read(), content_type="image/png")


class OptimizeImageTests(SimpleTestCase):
    def test_a_large_file_shrinks_a_lot(self):
        f = _png(2000, 1500)
        out = optimize_image(f, name="news/pic.png")
        self.assertLess(out.size, f.size)
        img = Image.open(out)
        self.assertEqual(img.format, "JPEG")

    def test_nothing_gets_wider_than_the_cap(self):
        f = _png(3000, 2000)
        out = optimize_image(f, name="news/pic.png")
        img = Image.open(out)
        self.assertLessEqual(max(img.size), MAX_DIMENSION)

    def test_a_small_image_is_not_upscaled(self):
        f = _png(400, 300)
        out = optimize_image(f, name="news/pic.png")
        img = Image.open(out)
        self.assertEqual(img.size, (400, 300))

    def test_transparency_is_flattened_onto_white_not_carried_through(self):
        f = _png(200, 200, mode="RGBA", color=(10, 20, 30, 0))
        out = optimize_image(f, name="news/pic.png")
        img = Image.open(out).convert("RGB")
        self.assertEqual(img.mode, "RGB")
        self.assertEqual(img.getpixel((5, 5)), (255, 255, 255))

    def test_the_path_keeps_its_folder_and_becomes_jpg(self):
        f = _png(100, 100)
        out = optimize_image(f, name="news/featured/pic.png")
        self.assertEqual(out.name, "news/featured/pic.jpg")

    def test_something_pillow_cannot_open_is_left_to_the_caller(self):
        garbage = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
        self.assertIsNone(optimize_image(garbage))
