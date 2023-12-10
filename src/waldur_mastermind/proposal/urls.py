from django.urls import re_path

from waldur_mastermind.proposal import views


def register_in(router):
    router.register(
        r'call-managing-organisations',
        views.CallManagingOrganisationViewSet,
        basename='call-managing-organisation',
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
    router.register(
        r'proposal-proposals',
        views.ProposalViewSet,
        basename='proposal-proposal',
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
    re_path(
        r'^api/proposal-protected-calls/(?P<uuid>[a-f0-9]+)/rounds/(?P<round_uuid>[a-f0-9]+)/$',
        views.ProtectedCallViewSet.as_view(
            {
                'get': 'round_detail',
                'delete': 'round_detail',
                'patch': 'round_detail',
                'put': 'round_detail',
            }
        ),
        name='proposal-call-round-detail',
    ),
]
