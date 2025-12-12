from . import views


def register_in(router):
    router.register(r"user-actions", views.UserActionViewSet, basename="useraction")
    router.register(
        r"user-action-executions",
        views.UserActionExecutionViewSet,
        basename="useractionexecution",
    )
    router.register(
        r"user-action-providers",
        views.UserActionProviderViewSet,
        basename="useractionprovider",
    )


urlpatterns = []
