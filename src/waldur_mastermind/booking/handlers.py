from waldur_core.core.enums import CoreStates
from waldur_mastermind.marketplace.models import Offering

from .executors import GoogleCalendarRenameExecutor


def update_google_calendar_name_if_offering_name_has_been_changed(
    sender, instance: Offering, created=False, **kwargs
):
    if created:
        return

    offering = instance

    if (
        hasattr(offering, "googlecalendar")
        and offering.googlecalendar.backend_id
        and offering.googlecalendar.state in [CoreStates.OK, CoreStates.ERRED]
        and offering.tracker.has_changed("name")
    ):
        GoogleCalendarRenameExecutor.execute(offering.googlecalendar)
