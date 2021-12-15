import django.db.models.deletion
from django.db import migrations, models


def build_spl_migrations(AFFECTED_MODELS):

    ADD_OPERATIONS = []
    ALTER_OPERATIONS = []

    for model_name in AFFECTED_MODELS:
        ADD_OPERATIONS += [
            migrations.AddField(
                model_name=model_name,
                name='project',
                field=models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='+',
                    to='structure.Project',
                ),
            ),
            migrations.AddField(
                model_name=model_name,
                name='service_settings',
                field=models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='+',
                    to='structure.ServiceSettings',
                ),
            ),
        ]
        ALTER_OPERATIONS += [
            migrations.AlterField(
                model_name=model_name,
                name='project',
                field=models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='+',
                    to='structure.Project',
                ),
            ),
            migrations.AlterField(
                model_name=model_name,
                name='service_settings',
                field=models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='+',
                    to='structure.ServiceSettings',
                ),
            ),
        ]

    return ADD_OPERATIONS + ALTER_OPERATIONS
