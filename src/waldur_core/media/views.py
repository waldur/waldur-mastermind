import os

from django.http import Http404, HttpResponse
from django.utils.http import content_disposition_header
from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView

from waldur_core.core.models import User

from . import access, models
from .utils import MARKDOWN_IMAGE_PREFIX

# Uploads are untrusted content served from the portal's own origin, where
# homeport keeps the API token in localStorage. Opened directly, a file must not
# run as a page: no scripts or plugins, an opaque origin, and no MIME sniffing.
# The policy only governs opening the URL itself; embedding with <img src> is
# unaffected.
CONTENT_SECURITY_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"

SVG_MIME_TYPES = ("image/svg", "image/svg+xml")


def check_file_permissions(file: models.File, user: User, request=None):
    """Deny access unless an app has registered a rule that allows it.

    Rules live in each app's ``media_access`` module; see
    :mod:`waldur_core.media.access` for the registry and the default-deny
    contract.
    """
    if not access.user_can_access_file(file, user, request=request):
        raise Http404


# A same-origin file served as script would run under any policy that allows
# 'self' scripts, such as the admin's: an upload is never served as one.
SCRIPT_MIME_SUFFIXES = ("javascript", "ecmascript", "jscript")


def _served_type(file: models.File) -> str:
    mime_type = file.mime_type or "application/octet-stream"
    if mime_type.split(";")[0].strip().lower().endswith(SCRIPT_MIME_SUFFIXES):
        return "application/octet-stream"
    return mime_type


def _serve_inline(file: models.File) -> bool:
    # SVG is a document format that can carry script, so it is downloaded
    # rather than opened. <img src> ignores Content-Disposition, so SVG logos
    # and markdown images still render where they are embedded.
    if file.mime_type in SVG_MIME_TYPES:
        return False
    if file.name.startswith(MARKDOWN_IMAGE_PREFIX):
        return True
    return bool(file.mime_type and file.mime_type.startswith("image/"))


class MediaView(GenericAPIView):
    permission_classes = ()
    filter_backends = ()
    access_declarations = {"get": [access.SERVES_FILE_TO]}

    @extend_schema(
        description="Get media file",
        responses={200: bytes},
    )
    def get(self, request, uuid):
        try:
            file = models.File.objects.get(uuid=uuid)
        except models.File.DoesNotExist:
            raise Http404
        check_file_permissions(file, request.user, request=request)
        filename = os.path.split(file.name)[-1]
        response = HttpResponse(file.content)
        response.headers["Content-Length"] = file.size
        response.headers["Content-Type"] = _served_type(file)
        response.headers["Content-Disposition"] = content_disposition_header(
            as_attachment=not _serve_inline(file),
            filename=filename,
        )
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
