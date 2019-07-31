from waldur_core.logging.loggers import EventLogger, event_logger

from waldur_mastermind.marketplace.models import Resource


class BookingEventLogger(EventLogger):
    resource = Resource

    class Meta:
        event_types = ('device_booking_is_accepted',)

    @staticmethod
    def get_scopes(event_context):
        resource = event_context['resource']
        return {resource, resource.project, resource.project.customer}


event_logger.register('waldur_booking', BookingEventLogger)
