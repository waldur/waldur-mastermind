from . import rest_views


def register_in(router):
    router.register(
        r"sram-project-rules",
        rest_views.SramProjectRuleViewSet,
        basename="sram-project-rule",
    )
    router.register(
        r"sram-groups",
        rest_views.SramGroupViewSet,
        basename="sram-group",
    )
