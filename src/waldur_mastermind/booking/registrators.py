from waldur_mastermind.marketplace import registrators as marketplace_registrators

from ..marketplace.enums import BOOKING_OFFERING


class BookingRegistrator(marketplace_registrators.MarketplaceRegistrator):
    plugin_name = BOOKING_OFFERING
