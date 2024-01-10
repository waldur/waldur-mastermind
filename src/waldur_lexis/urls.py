from . import views


def register_in(router):
    router.register(
        r"lexis-links",
        views.LexisLinkViewSet,
        basename="lexis-link",
    )
