from waldur_autoprovisioning import views


def register_in(router):
    router.register(
        r"autoprovisioning-rules",
        views.RuleViewSet,
        basename="autoprovisioning-rule",
    )
