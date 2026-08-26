"""Media access rules for files owned by the matrix_chat app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_mastermind.matrix_chat.managers import get_accessible_room_ids
from waldur_mastermind.matrix_chat.models import MatrixHistoryExport

# Room history exports and the media extracted from them. Both fields share the
# matrix_exports/ tree -- media_file nests inside export_file's prefix -- and
# both belong to the same row, so one rule covers the whole tree.
#
# Downloads normally go through MatrixHistoryExportDownloadView, which is
# already gated; the serializer deliberately emits that URL rather than the
# storage one. This rule is defence in depth for the media route itself.
MATRIX_EXPORT_PREFIX = "matrix_exports/"

EXPORT_FILE_FIELDS = ["export_file", "media_file"]


def user_can_access_export(file, user) -> bool:
    """Mirror MatrixHistoryExportViewSet.get_queryset."""
    if not user.is_authenticated:
        return False
    queryset = MatrixHistoryExport.objects.filter(
        access.field_lookup(EXPORT_FILE_FIELDS, file.name)
    )
    if not (user.is_staff or user.is_support):
        queryset = queryset.filter(room__id__in=get_accessible_room_ids(user))
    return queryset.exists()


access.register(MATRIX_EXPORT_PREFIX, user_can_access_export)
