from django.db import migrations


def delete_answers(apps, schema_editor):
    Answer = apps.get_model('marketplace_checklist', 'Answer')
    Answer.objects.filter(user__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace_checklist', '0007_checklist_customers'),
    ]

    operations = [
        migrations.RunPython(delete_answers),
    ]
