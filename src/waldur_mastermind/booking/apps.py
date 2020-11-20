from django.apps import AppConfig


class BookingConfig(AppConfig):
    name = 'waldur_mastermind.booking'
    verbose_name = 'Booking system'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import manager

        from . import PLUGIN_NAME, processors, utils

        manager.register(
            offering_type=PLUGIN_NAME,
            create_resource_processor=processors.BookingCreateProcessor,
            delete_resource_processor=processors.BookingDeleteProcessor,
            change_attributes_for_view=utils.change_attributes_for_view,
        )
