import django_fsm
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0218_backfill_gpu_architectures"),
    ]

    operations = [
        migrations.AlterField(
            model_name="courseaccount",
            name="state",
            field=django_fsm.FSMIntegerField(
                choices=[
                    (1, "OK"),
                    (2, "Closed"),
                    (3, "Erred"),
                    (4, "Pending"),
                ],
                default=1,
            ),
        ),
    ]
