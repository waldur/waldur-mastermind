import logging

from django.db import migrations

from waldur_core.core.models import generate_slug
from waldur_core.core.utils import chunked_queryset

logger = logging.getLogger(__name__)


def fill_slug(apps, schema_editor):
    Order = apps.get_model("marketplace", "order")

    orders_without_slug = Order.objects.filter(slug="").count()
    logger.info(
        f"Starting slug generation for {orders_without_slug} orders without slugs"
    )

    if orders_without_slug == 0:
        logger.info("No orders need slug generation")
        return

    batch_size = 5000
    orders_to_update = []
    processed_count = 0

    # Process only orders without slugs in batches to avoid memory issues.
    # Client-side chunks rather than a server-side cursor: this migration is
    # atomic = False and bulk_updates between fetches, so a cursor would be
    # declared and fetched across transaction boundaries — which a
    # transaction-mode pooler does not keep on one backend. The keyset walk
    # only moves forward by pk, so rows this loop stamps cannot be revisited.
    order_queryset = chunked_queryset(
        Order.objects.filter(slug="").select_related("project__customer", "offering"),
        chunk_size=batch_size,
    )

    for order in order_queryset:
        order.slug = generate_slug(
            f"{order.project.customer.slug}-{order.offering.slug}-{order.created.date().isoformat()}",
            Order,
        )
        orders_to_update.append(order)

        # Bulk update when batch is full
        if len(orders_to_update) >= batch_size:
            Order.objects.bulk_update(orders_to_update, ["slug"])
            processed_count += len(orders_to_update)
            logger.info(
                f"Processed {processed_count}/{orders_without_slug} orders ({processed_count / orders_without_slug * 100:.1f}%)"
            )
            orders_to_update = []

    # Update remaining orders
    if orders_to_update:
        Order.objects.bulk_update(orders_to_update, ["slug"])
        processed_count += len(orders_to_update)
        logger.info(
            f"Processed {processed_count}/{orders_without_slug} orders (100.0%)"
        )

    logger.info("Order slug generation completed successfully")


def clear_slug(apps, schema_editor):
    Order = apps.get_model("marketplace", "order")
    Order.objects.update(slug="")
    logger.info("Order slugs cleared")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("marketplace", "0188_order_slug"),
    ]

    operations = [
        migrations.RunPython(fill_slug, clear_slug, elidable=True),
    ]
