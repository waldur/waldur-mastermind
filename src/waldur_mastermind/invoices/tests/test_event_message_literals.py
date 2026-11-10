"""Invoice item names show in event messages as written."""

from rest_framework import test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event

from . import factories

TEXT = "AA{{BB}}CC {nosuchkey} {"


def last_message(event_type):
    return Event.objects.filter(event_type=event_type).latest("created").message


class InvoiceItemMessagesTest(test.APITestCase):
    def test_name_shows_as_written(self):
        item = factories.InvoiceItemFactory(name="item")
        item.name = TEXT
        item.save()

        message = last_message(EventType.INVOICE_ITEM_UPDATED)
        self.assertTrue(message.startswith(f"Invoice item {TEXT} has been updated."))
        self.assertIn(f"name: item -> {TEXT}", message)

        item.delete()

        self.assertEqual(
            last_message(EventType.INVOICE_ITEM_DELETED),
            f"Invoice item {TEXT} has been deleted.",
        )
