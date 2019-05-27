from django.apps import AppConfig


class BookingConfig(AppConfig):
    name = 'waldur_mastermind.booking'
    verbose_name = 'Booking system'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import manager

        from . import BOOKING_TYPE, processors

        manager.register(offering_type=BOOKING_TYPE,
                         create_resource_processor=processors.BookingCreateProcessor,
                         delete_resource_processor=processors.BookingDeleteProcessor)
