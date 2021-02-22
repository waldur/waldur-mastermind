from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_extend_description_limits'),
    ]

    operations = [
        migrations.RunSQL('DROP TABLE IF EXISTS slurm_invoices_slurmpackage'),
    ]
