from waldur_core.core import models as core_models

from .executors import GoogleCalendarRenameExecutor


def update_google_calendar_name_if_offering_name_has_been_changed(
    sender, instance, created=False, **kwargs
):
    if created:
        return

    offering = instance

    if (
        hasattr(offering, "googlecalendar")
        and offering.googlecalendar.backend_id
        and offering.googlecalendar.state
        in [core_models.StateMixin.States.OK, core_models.StateMixin.States.ERRED]
        and offering.tracker.has_changed("name")
    ):
        GoogleCalendarRenameExecutor.execute(offering.googlecalendar)
