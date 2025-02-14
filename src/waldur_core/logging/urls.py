from django.urls import re_path

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
    router.register(r"email-logs", views.EmailLogView, basename="email-log")


urlpatterns = [
    re_path(r"^rabbitmq-vhost-stats/", views.RabbitMQVhostStats.as_view()),
    re_path(r"^rabbitmq-user-stats/", views.RabbitMQUserStats.as_view()),
]
