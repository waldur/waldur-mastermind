import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_aws", "0004_error_traceback"),
        ("structure", "0001_squashed_0036"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="structure.Project",
            ),
        ),
        migrations.AddField(
            model_name="instance",
            name="service_settings",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="structure.ServiceSettings",
            ),
        ),
        migrations.AddField(
            model_name="volume",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="structure.Project",
            ),
        ),
        migrations.AddField(
            model_name="volume",
            name="service_settings",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="structure.ServiceSettings",
            ),
        ),
    ]
