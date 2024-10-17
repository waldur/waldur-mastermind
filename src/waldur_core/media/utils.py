import base64
import hashlib
import tempfile

import magic


def dummy_image(filetype="gif"):
    """Generate empty image in temporary file for testing"""
    # 1x1px Transparent GIF
    GIF = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    tmp_file = tempfile.NamedTemporaryFile(suffix=".%s" % filetype)
    tmp_file.write(base64.b64decode(GIF))
    return open(tmp_file.name, "rb")


def guess_image_extension(content: bytes) -> str:
    mime_type = magic.from_buffer(content[:1024], mime=True)
    return {
        "image/svg": "svg",
        "image/svg+xml": "svg",
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/webp": "webp",
    }.get(mime_type)


def get_image_hash(content: str):
    return hashlib.sha256(content).hexdigest()
