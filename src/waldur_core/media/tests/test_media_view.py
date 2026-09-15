"""Uploaded files must never run as a page on the portal's own origin.

Homeport keeps the API token in localStorage, so a script in an uploaded file
that is opened from ``/api/media/`` could read it. These tests pin the response
headers that stop that, whatever the upload-time checks let through.
"""

from ddt import data, ddt, unpack
from django.urls import reverse
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.media.utils import get_image_hash

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg">'
    b'<animate attributeName="x" dur="1s" onbegin="alert(1)"/></svg>'
)
PNG = b"\x89PNG\r\n"
PDF = b"%PDF-1.4\n"


def make_file(name, content, mime_type):
    return media_models.File.objects.create(
        name=name,
        content=content,
        size=len(content),
        mime_type=mime_type,
        hash=get_image_hash(content),
    )


def get_media(client, file):
    return client.get(reverse("media", kwargs={"uuid": file.uuid.hex}))


@ddt
class MediaResponseHeadersTest(test.APITestCase):
    @data(
        ("markdown_images/logo.svg", SVG, "image/svg+xml"),
        ("customer/logo.png", PNG, "image/png"),
        ("call_documents/guide.pdf", PDF, "application/pdf"),
    )
    @unpack
    def test_file_cannot_run_as_a_page(self, name, content, mime_type):
        response = get_media(self.client, make_file(name, content, mime_type))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        policy = response["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertIn("sandbox", policy)
        self.assertNotIn("script-src", policy)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")


@ddt
class MediaContentDispositionTest(test.APITestCase):
    @data(
        ("customer/logo.svg", "image/svg+xml"),
        ("customer/logo.svg", "image/svg"),
        # Markdown images are otherwise always inline.
        ("markdown_images/diagram.svg", "image/svg+xml"),
        # The type is sniffed from the content, so the extension proves nothing.
        ("markdown_images/diagram.png", "image/svg+xml"),
    )
    @unpack
    def test_svg_is_downloaded_rather_than_opened(self, name, mime_type):
        response = get_media(self.client, make_file(name, SVG, mime_type))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        self.assertEqual(response.content, SVG)

    @data("customer/logo.png", "markdown_images/diagram.png")
    def test_raster_image_is_served_inline(self, name):
        response = get_media(self.client, make_file(name, PNG, "image/png"))

        self.assertTrue(response["Content-Disposition"].startswith("inline"))

    def test_document_is_served_as_attachment(self):
        file = make_file("call_documents/guide.pdf", PDF, "application/pdf")

        response = get_media(self.client, file)

        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
