import magic
from django.db import migrations


def detect_mime_type(apps, schema_editor):
    File = apps.get_model("media", "File")
    for row in File.objects.all():
        if row.content:
            row.mime_type = magic.from_buffer(row.content[:1024].tobytes(), mime=True)
            row.save(update_fields=["mime_type"])


class Migration(migrations.Migration):
    dependencies = [
        ("media", "0002_data"),
    ]

    operations = [migrations.RunPython(detect_mime_type)]
