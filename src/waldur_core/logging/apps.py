from django.apps import AppConfig
from django.db.models import signals
from django.utils.module_loading import autodiscover_modules


class EventsConfig(AppConfig):
    name = "waldur_core.logging"
    verbose_name = "Logging"

    def ready(self):
        import structlog
        from django.dispatch import receiver
        from django_structlog import signals as structlog_signals
        from rest_framework.authtoken.models import Token
        from rest_framework.views import APIView

        from waldur_core.logging import handlers, models
        from waldur_core.logging.event_logger import event_emitted

        def _bind_user_uuid(request, logger, **kwargs):
            """Bind user_uuid and override user_id for consistency with request_id, task_id (UUIDs)."""
            if hasattr(request, "user") and request.user.is_authenticated:
                structlog.contextvars.unbind_contextvars("user_id")
                structlog.contextvars.bind_contextvars(user_uuid=str(request.user.uuid))

        receiver(structlog_signals.bind_extra_request_metadata)(_bind_user_uuid)
        receiver(structlog_signals.bind_extra_request_finished_metadata)(
            _bind_user_uuid
        )
        receiver(structlog_signals.bind_extra_request_failed_metadata)(_bind_user_uuid)

        # Patch APIView.initial so all DRF views (incl. token-authenticated) bind user_uuid
        # after perform_authentication; middleware signals fire before that for token auth
        from waldur_core.logging.middleware import set_current_user

        _original_api_view_initial = APIView.initial

        def _patched_api_view_initial(self, request, *args, **kwargs):
            _original_api_view_initial(self, request, *args, **kwargs)
            if hasattr(request, "user") and request.user.is_authenticated:
                structlog.contextvars.unbind_contextvars("user_id")
                structlog.contextvars.bind_contextvars(user_uuid=str(request.user.uuid))
                # Also populate the event-context thread-local so audit events
                # emitted from viewsets and serializers carry the user fields.
                # For token-authenticated DRF requests, request.user isn't set
                # when CaptureEventContextMiddleware.process_request runs.
                set_current_user(request.user)

        APIView.initial = _patched_api_view_initial

        autodiscover_modules("log")
        event_emitted.connect(
            handlers.process_hook,
            dispatch_uid="waldur_core.logging.handlers.process_hook",
        )

        signals.post_delete.connect(
            handlers.delete_stale_event_subscriptions,
            sender=Token,
            dispatch_uid="waldur_core.logging.handlers.delete_stale_event_subscriptions",
        )

        signals.pre_delete.connect(
            handlers.cleanup_rabbitmq_queue_on_delete,
            sender=models.EventSubscriptionQueue,
            dispatch_uid="waldur_core.logging.handlers.cleanup_rabbitmq_queue_on_delete",
        )
