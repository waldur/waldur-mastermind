from django.core.checks import Error, register

from waldur_core.logging.availability import get_undeclared_groups


@register()
def event_groups_declare_availability(app_configs, **kwargs):
    """Every event group must say when a deployment can emit it.

    Without a declaration the group falls back to being advertised everywhere,
    which is how the catalogue accumulated OpenStack groups on deployments that
    run no OpenStack. Failing here keeps a newly added group from quietly
    re-entering that state.
    """
    undeclared = get_undeclared_groups()
    if not undeclared:
        return []
    return [
        Error(
            "Event groups without an availability declaration: %s."
            % ", ".join(group.value for group in undeclared),
            hint=(
                "Add an entry to EVENT_GROUP_AVAILABILITY in "
                "waldur_core/logging/availability.py. Use Always() only for "
                "behaviour every Waldur deployment has."
            ),
            id="waldur.logging.E001",
        )
    ]
