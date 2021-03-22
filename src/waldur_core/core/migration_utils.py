import django.db.models.deletion
from django.db import migrations, models


def build_spl_migrations(APP_NAME, SVC_NAME, SPL_NAME, AFFECTED_MODELS):
    def fill_project_and_service(apps, schema_editor):
        for model_name in AFFECTED_MODELS:
            model = apps.get_model(APP_NAME, model_name)
            for obj in model.objects.all():
                obj.project = obj.service_project_link.project
                obj.service_settings = obj.service_project_link.service.settings
                obj.save(update_fields=['project', 'service_settings'])

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
            migrations.RemoveField(model_name=model_name, name='service_project_link',),
        ]

    return (
        ADD_OPERATIONS
        + [migrations.RunPython(fill_project_and_service)]
        + ALTER_OPERATIONS
        + [
            migrations.AlterUniqueTogether(name=SPL_NAME, unique_together=None,),
            migrations.AlterUniqueTogether(name=SVC_NAME, unique_together=None,),
            migrations.DeleteModel(name=SPL_NAME),
            migrations.DeleteModel(name=SVC_NAME),
        ]
    )
