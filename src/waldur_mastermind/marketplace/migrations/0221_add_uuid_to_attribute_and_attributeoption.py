# Generated manually

import uuid

from django.db import migrations, models

import waldur_core.core.fields


def gen_attribute_uuids(apps, schema_editor):
    Attribute = apps.get_model("marketplace", "Attribute")
    for row in Attribute.objects.all():
        row.uuid = uuid.uuid4()
        row.save(update_fields=["uuid"])


def gen_attributeoption_uuids(apps, schema_editor):
    AttributeOption = apps.get_model("marketplace", "AttributeOption")
    for row in AttributeOption.objects.all():
        row.uuid = uuid.uuid4()
        row.save(update_fields=["uuid"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0220_offering_helpdesk_url_documentation_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="attribute",
            name="uuid",
            field=models.UUIDField(null=True, unique=False),
        ),
        migrations.RunPython(
            gen_attribute_uuids, reverse_code=migrations.RunPython.noop
        ),
        migrations.AlterField(
            model_name="attribute",
            name="uuid",
            field=waldur_core.core.fields.UUIDField(),
        ),
        migrations.AddField(
            model_name="attributeoption",
            name="uuid",
            field=models.UUIDField(null=True, unique=False),
        ),
        migrations.RunPython(
            gen_attributeoption_uuids, reverse_code=migrations.RunPython.noop
        ),
        migrations.AlterField(
            model_name="attributeoption",
            name="uuid",
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
