from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('logging', '0007_drop_alerts'),
    ]

    # Run SQL instead of Run Python is used to avoid OOM error
    # See also: https://docs.djangoproject.com/en/3.1/ref/models/querysets/#django.db.models.query.QuerySet.delete
    operations = [
        migrations.RunSQL(
            "DELETE FROM logging_feed WHERE event_id in (SELECT id from logging_event WHERE event_type='openstack_security_group_rule_pulled')"
        ),
        migrations.RunSQL(
            "DELETE FROM logging_event WHERE event_type='openstack_security_group_rule_pulled'"
        ),
    ]
