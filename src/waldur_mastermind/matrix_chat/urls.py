from django.urls import path

from . import views


def register_in(router):
    router.register(
        r"matrix/rooms",
        views.MatrixRoomViewSet,
        basename="matrix-room",
    )
    router.register(
        r"matrix/exports",
        views.MatrixHistoryExportViewSet,
        basename="matrix-export",
    )


urlpatterns = [
    path(
        "api/matrix/credentials/",
        views.MatrixCredentialsView.as_view(),
        name="matrix-credentials",
    ),
    path(
        "_matrix/app/v1/transactions/<str:txn_id>",
        views.MatrixAppserviceWebhookView.as_view(),
        name="matrix-appservice-transactions",
    ),
    path(
        "api/admin/matrix-appservice/setup/",
        views.MatrixAppserviceSetupView.as_view(),
        name="matrix-appservice-setup",
    ),
    path(
        "api/admin/matrix-appservice/status/",
        views.MatrixAppserviceStatusView.as_view(),
        name="matrix-appservice-status",
    ),
    path(
        "api/admin/matrix/reprovision/",
        views.MatrixReprovisionView.as_view(),
        name="matrix-reprovision",
    ),
    path(
        "api/admin/matrix/diagnostics/",
        views.MatrixDiagnosticsView.as_view(),
        name="matrix-diagnostics",
    ),
    path(
        "api/admin/matrix/livekit/overview/",
        views.LiveKitOverviewView.as_view(),
        name="matrix-livekit-overview",
    ),
    path(
        "api/admin/matrix/livekit/participants/",
        views.LiveKitRoomParticipantsView.as_view(),
        name="matrix-livekit-room-participants",
    ),
    # `<str:uuid>` not `<uuid:uuid>`: Waldur's StringUUID.__str__ returns the
    # 32-char hex form (no hyphens), but Django's uuid converter requires the
    # canonical dashed form. The view's MatrixHistoryExport.objects.get()
    # raises DoesNotExist if `uuid` isn't a valid identifier.
    path(
        "api/matrix/exports/<str:uuid>/download/<str:kind>/",
        views.MatrixHistoryExportDownloadView.as_view(),
        name="matrix-export-download",
    ),
]
