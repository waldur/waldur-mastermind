from django.urls import re_path

from waldur_mastermind.proposal import views


def register_in(router):
    router.register(
        r'proposal-managers',
        views.ManagerViewSet,
        basename='proposal-manager',
    )
    router.register(
        r'proposal-public-calls',
        views.PublicCallViewSet,
        basename='proposal-public-call',
    )
    router.register(
        r'proposal-protected-calls',
        views.ProtectedCallViewSet,
        basename='proposal-protected-call',
    )


urlpatterns = [
    re_path(
        r'^api/proposal-protected-calls/(?P<uuid>[a-f0-9]+)/offerings/(?P<requested_offering_uuid>[a-f0-9]+)/$',
        views.ProtectedCallViewSet.as_view(
            {
                'get': 'offering_detail',
                'delete': 'offering_detail',
                'patch': 'offering_detail',
                'put': 'offering_detail',
            }
        ),
        name='proposal-call-offering-detail',
    ),
]
