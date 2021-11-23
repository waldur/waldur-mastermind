import unicodedata

from django.db import migrations


def normalize_unicode(data):
    return unicodedata.normalize(u'NFKD', data).encode('ascii', 'ignore').decode('utf8')


def fill_query_field(apps, schema_editor):
    User = apps.get_model('core', 'User')
    for user in User.objects.filter(query_field=''):
        user.query_field = normalize_unicode(user.first_name + ' ' + user.last_name)
        user.save(update_fields=['query_field'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_long_email'),
    ]

    operations = [
        migrations.RunPython(fill_query_field),
    ]
