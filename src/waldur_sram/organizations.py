"""Map SRAM organisations to Waldur organizations (customers).

A SRAM group's URN starts with its organisation's short name
(``<organisation>:<co>[:<group>]``). The short name is kept in
``Customer.backend_id``:

1. a customer whose ``backend_id`` is the short name is used;
2. otherwise a customer named exactly like the short name and without a
   ``backend_id`` is adopted: its ``backend_id`` is set and an event recorded;
3. otherwise a customer is created.

SRAM never deletes or archives organizations.
"""

from __future__ import annotations

import logging
import zlib

from django.db import connection, transaction

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.models import Customer
from waldur_core.users.scim.server.exceptions import ScimError

logger = logging.getLogger(__name__)

# Namespace of the transaction-level advisory lock that serialises the
# lookup-or-create of one organisation.
_LOCK_NAMESPACE = zlib.crc32(b"waldur_sram.organisation") & 0x7FFFFFFF


def _lock(short_name: str) -> None:
    """``select_for_update`` cannot lock a customer that does not exist yet."""
    if connection.vendor != "postgresql":
        return
    key = zlib.crc32(short_name.encode()) & 0x7FFFFFFF
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_NAMESPACE, key])


def _single(queryset, short_name: str, what: str) -> Customer | None:
    customers = list(queryset[:2])
    if len(customers) > 1:
        logger.warning(
            "SRAM organisation %s: several organizations match by %s.", short_name, what
        )
        raise ScimError(
            409,
            f"Several organizations match SRAM organisation {short_name!r} by "
            f"{what}; set backend_id on the right one.",
            scim_type="uniqueness",
        )
    return customers[0] if customers else None


@transaction.atomic
def resolve_customer(short_name: str) -> Customer:
    short_name = short_name.strip()
    if not short_name:
        raise ScimError(
            400,
            "Group URN does not name a SRAM organisation.",
            scim_type="invalidValue",
        )
    _lock(short_name)

    customer = _single(
        Customer.objects.filter(backend_id=short_name), short_name, "backend_id"
    )
    if customer:
        return customer

    customer = _single(
        Customer.objects.filter(name=short_name, backend_id=""), short_name, "name"
    )
    if customer:
        # Bypass post_save: the generic "customer updated" event would not say
        # why the identifier changed.
        Customer.objects.filter(pk=customer.pk).update(backend_id=short_name)
        customer.backend_id = short_name
        # The short name is SRAM data: escape it for the message template.
        escaped = short_name.replace("{", "{{").replace("}", "}}")
        event_logger.emit(
            "Customer {customer_name} has been linked to SRAM organisation "
            f"{escaped}: its backend ID is now {escaped}.",
            event_type=EventType.CUSTOMER_UPDATE_SUCCEEDED,
            event_context={"customer": customer},
            scopes=[customer],
        )
        logger.info("SRAM: adopted customer %s for %s", customer.uuid.hex, short_name)
        return customer

    customer = Customer.objects.create(name=short_name, backend_id=short_name)
    logger.info("SRAM: created customer %s for %s", customer.uuid.hex, short_name)
    return customer
