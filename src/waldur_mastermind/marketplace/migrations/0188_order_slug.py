from django.db import migrations, models

from waldur_core.core.models import generate_slug


def fill_slug(apps, schema_editor):
    Order = apps.get_model("marketplace", "order")
    for order in Order.objects.all():
        order.slug = generate_slug(
            f"{order.project.customer.slug}-{order.offering.slug}-{order.created.date().isoformat()}",
            Order,
        )
        order.save(update_fields=["slug"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0187_alter_categorycomponent_description_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="slug",
            field=models.SlugField(blank=True, editable=False),
        ),
        migrations.RunPython(fill_slug, elidable=True),
        migrations.AlterField(
            model_name="order",
            name="slug",
            field=models.SlugField(),
        ),
    ]
