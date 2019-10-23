from . import views


def register_in(router):
    router.register(r'resource-sla-state-transition', views.ResourceSlaStateTransitionViewSet,
                    basename='resource-sla-state-transition')
