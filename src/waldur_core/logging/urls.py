from django.conf.urls import url

from waldur_core.logging import views


def register_in(router):
    router.register(r'events', views.EventViewSet, basename='event')
    router.register(r'hooks-web', views.WebHookViewSet, basename='webhook')
    router.register(r'hooks-push', views.PushHookViewSet, basename='pushhook')
    router.register(r'hooks-email', views.EmailHookViewSet, basename='emailhook')
    router.register(r'hooks', views.HookSummary, basename='hooks')


events_count_history = views.EventViewSet.as_view({'get': 'count_history'})

urlpatterns = [
    # Separate history URL for consistency with other history endpoints
    url(r'^events/count/history/', events_count_history, name='event-count-history'),
]
