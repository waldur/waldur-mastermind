from django.db import migrations

from waldur_core.core.clean_html import unescape_html

HTML_FIELDS = (
    'terms_of_service',
    'description',
    'full_description',
    'vendor_details',
)


def unescape_html_fields(apps, schema_editor):
    Offering = apps.get_model('marketplace', 'Offering')
    for offering in Offering.objects.all():
        changed_fields = set()
        for field in HTML_FIELDS:
            old_value = getattr(offering, field)
            new_value = unescape_html(old_value)
            if old_value != new_value:
                setattr(offering, field, new_value)
                changed_fields.add(field)
        if changed_fields:
            offering.save(update_fields=changed_fields)


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0044_remove_order_file_field'),
    ]

    operations = [
        migrations.RunPython(unescape_html_fields),
    ]
