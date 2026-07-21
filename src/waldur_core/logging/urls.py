from django.urls import include, re_path
from rest_framework.routers import DefaultRouter

from waldur_core.logging import views


def register_in(router):
    router.register(r"events", views.EventViewSet, basename="event")
    router.register(r"hooks-web", views.WebHookViewSet, basename="webhook")
    router.register(r"hooks-email", views.EmailHookViewSet, basename="emailhook")
    router.register(r"hooks", views.HookSummary, basename="hooks")
    router.register(r"events-stats", views.EventsStatsViewSet, basename="events-stats")
    router.register(
        r"event-subscriptions",
        views.EventSubscriptionViewSet,
        basename="event-subscription",
    )
    router.register(
        r"event-subscription-queues",
        views.EventSubscriptionQueueViewSet,
        basename="event-subscription-queue",
    )
    router.register(
        r"event-consumers",
        views.EventConsumerViewSet,
        basename="event-consumer",
    )
    router.register(r"email-logs", views.EmailLogView, basename="email-log")
    router.register(r"system-logs", views.SystemLogViewSet, basename="system-log")
    router.register(
        r"data-access-logs",
        views.UserDataAccessLogViewSet,
        basename="data-access-log",
    )


# Debug router for staff-only debugging endpoints under /api/debug/
debug_router = DefaultRouter()
debug_router.register(r"pubsub", views.PubsubDebugViewSet, basename="pubsub-debug")


urlpatterns = [
    re_path(r"^rabbitmq-vhost-stats/", views.RabbitMQVhostStats.as_view()),
    re_path(r"^rabbitmq-user-stats/", views.RabbitMQUserStats.as_view()),
    re_path(r"^rabbitmq-stats/", views.RabbitMQStatsViewSet.as_view()),
    re_path(r"^rabbitmq-overview/", views.RabbitMQOverviewStats.as_view()),
    re_path(r"^debug/", include(debug_router.urls)),
]
