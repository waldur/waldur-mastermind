import magic
from django.db import migrations


def fill_attachment_mime_type(apps, schema_editor):
    Attachment = apps.get_model('support', 'Attachment')

    for attachment in Attachment.objects.all():
        if not attachment.file:
            continue
        if not attachment.file_size:
            attachment.file_size = attachment.file.size
            attachment.save()
        if not attachment.mime_type:
            try:
                content = attachment.file.open().read(1024)
                attachment.mime_type = magic.from_buffer(content, mime=True)
                attachment.save()
            except Exception as e:
                print(
                    f'Unable to detect mime type for attachment {attachment}. Error is: {e}'
                )
                continue


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0014_unique_issue_attachment'),
    ]

    operations = [
        migrations.RunPython(fill_attachment_mime_type),
    ]
