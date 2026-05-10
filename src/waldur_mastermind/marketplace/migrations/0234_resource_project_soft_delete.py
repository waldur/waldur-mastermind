from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("marketplace", "0233_repair_limit_period_after_0226"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="resourceproject",
            options={
                "base_manager_name": "objects",
                "ordering": ["created"],
            },
        ),
        migrations.AddField(
            model_name="resourceproject",
            name="is_removed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="resourceproject",
            name="removed_date",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="resourceproject",
            name="removed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="removed_resource_projects",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="resourceproject",
            name="termination_metadata",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterUniqueTogether(
            name="resourceproject",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="resourceproject",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_removed", False)),
                fields=("resource", "name"),
                name="uniq_active_resource_project_name_per_resource",
            ),
        ),
    ]
