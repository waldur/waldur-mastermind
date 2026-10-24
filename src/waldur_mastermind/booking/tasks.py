from celery import shared_task
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.callbacks import resource_creation_canceled
from waldur_mastermind.marketplace.enums import BOOKING_OFFERING, ResourceStates

from . import utils


@shared_task(
    name="waldur_mastermind.booking.send_notifications_about_upcoming_bookings"
)
def send_notifications_about_upcoming_bookings():
    """Send email notifications to users about their upcoming bookings."""
    for info in utils.get_info_about_upcoming_bookings():
        user = info["user"]
        if not user.email or not user.notifications_enabled:
            return
        core_utils.broadcast_mail("booking", "notification", info, [user.email])


@shared_task(name="waldur_mastermind.booking.reject_past_bookings")
def reject_past_bookings():
    """Reject booking resources that have start times in the past."""
    resources = marketplace_models.Resource.objects.filter(
        offering__type=BOOKING_OFFERING,
        state=ResourceStates.CREATING,
    )
    for resource in resources:
        if resource.attributes["schedules"][-1]["start"] < str(timezone.now()):
            resource_creation_canceled(resource, validate=True)
