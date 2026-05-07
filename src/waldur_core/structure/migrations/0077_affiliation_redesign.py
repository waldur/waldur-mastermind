from django.db import migrations, models


def copy_first_affiliation_to_fk(apps, schema_editor):
    """Copy the first M2M entry into the new single-value FK.

    Logs (via print) projects that had multiple affiliations so the dropped
    extras are visible in the migration output.
    """
    Project = apps.get_model("structure", "Project")
    for project in Project.objects.filter(
        affiliated_organizations__isnull=False
    ).distinct():
        orgs = list(project.affiliated_organizations.order_by("name"))
        first = orgs[0]
        project.affiliation = first
        project.save(update_fields=["affiliation"])
        if len(orgs) > 1:
            dropped = ", ".join(o.name for o in orgs[1:])
            print(
                f"[migration 0077] project {project.uuid} kept '{first.name}', "
                f"dropped extra affiliations: {dropped}"
            )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("structure", "0076_sciencedomain_sciencesubdomain"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="affiliatedorganization",
            options={
                "ordering": ("name",),
                "verbose_name": "affiliation",
                "verbose_name_plural": "affiliations",
            },
        ),
        migrations.AddField(
            model_name="customer",
            name="default_affiliations",
            field=models.ManyToManyField(
                blank=True,
                help_text=(
                    "Affiliations offered to project creators of this organization. "
                    "Staff users can select any affiliation; non-staff are limited to this list."
                ),
                related_name="default_for_customers",
                to="structure.affiliatedorganization",
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="affiliation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="projects",
                to="structure.affiliatedorganization",
            ),
        ),
        migrations.RunPython(copy_first_affiliation_to_fk, noop_reverse),
        migrations.RemoveField(
            model_name="project",
            name="affiliated_organizations",
        ),
    ]
