from waldur_autoprovisioning import views


def register_in(router):
    router.register(
        r"autoprovisioning-rules",
        views.RuleViewSet,
        basename="autoprovisioning-rule",
    )
    router.register(
        r"autoprovisioning-rule-plans",
        views.RulePlansViewSet,
        basename="autoprovisioning-rule-plan",
    )
