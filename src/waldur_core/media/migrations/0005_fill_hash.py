import hashlib

from django.db import migrations


def calculate_file_hashes(apps, schema_editor):
    File = apps.get_model("media", "File")
    for file in File.objects.all():
        if file.content:
            file.hash = hashlib.sha256(file.content).hexdigest()
            file.save(update_fields=["hash"])
        else:
            file.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("media", "0004_add_hash"),
    ]

    operations = [
        migrations.RunPython(calculate_file_hashes),
    ]
