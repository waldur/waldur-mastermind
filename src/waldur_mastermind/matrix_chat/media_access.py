"""Media access rules for files owned by the matrix_chat app.

See :mod:`waldur_core.media.access`.
"""

from waldur_core.media import access
from waldur_mastermind.matrix_chat.managers import filter_exports_for_request
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


access.register(
    MATRIX_EXPORT_PREFIX,
    access.queryset_rule(
        MatrixHistoryExport, EXPORT_FILE_FIELDS, filter_exports_for_request
    ),
    # A personal access token's scopes and bindings are on the request.
    with_request=True,
)
