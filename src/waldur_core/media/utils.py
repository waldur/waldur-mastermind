import base64
import hashlib
import os
import tempfile
import uuid as uuid_module

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from waldur_core.media import models as media_models

MARKDOWN_IMAGE_PREFIX = "markdown_images/"


def dummy_image(filetype="gif"):
    """Generate empty image in temporary file for testing"""
    # 1x1px Transparent GIF
    GIF = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    tmp_file = tempfile.NamedTemporaryFile(suffix=".%s" % filetype)
    tmp_file.write(base64.b64decode(GIF))
    return open(tmp_file.name, "rb")


def dummy_svg():
    """Generate a simple SVG file for testing"""
    svg_content = """<?xml version="1.0" encoding="UTF-8"?>
<svg width="100" height="100" xmlns="http://www.w3.org/2000/svg">
  <circle cx="50" cy="50" r="40" fill="red"/>
</svg>"""
    tmp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".svg", encoding="utf-8")
    tmp_file.write(svg_content)
    return open(tmp_file.name, "rb")


def guess_image_extension(content: bytes | str) -> str | None:
    import magic

    mime_type = magic.from_buffer(content[:1024], mime=True)
    if not mime_type:
        return
    return {
        "image/svg": "svg",
        "image/svg+xml": "svg",
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/webp": "webp",
    }.get(mime_type)


def get_image_hash(content: bytes):
    return hashlib.sha256(content).hexdigest()


def store_markdown_image(uploaded_file) -> media_models.File:
    """Store an uploaded image for embedding in offering markdown descriptions."""
    content = uploaded_file.read()
    filename = os.path.basename(uploaded_file.name or "image")
    storage_name = f"{MARKDOWN_IMAGE_PREFIX}{uuid_module.uuid4().hex}_{filename}"
    saved_name = default_storage.save(storage_name, ContentFile(content))
    return media_models.File.objects.get(name=saved_name)
