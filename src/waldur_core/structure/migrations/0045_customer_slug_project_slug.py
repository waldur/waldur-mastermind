# Generated by Django 4.2.10 on 2024-07-23 19:06

from django.db import migrations, models
from django.template.defaultfilters import slugify


def fill_slug(apps, schema_editor):
    Customer = apps.get_model("structure", "Customer")
    Project = apps.get_model("structure", "Project")
    for model in (Customer, Project):
        for row in model.objects.all():
            base_slug = slugify(row.name)[:8]
            existing_slugs = model.objects.filter(
                slug__startswith=base_slug
            ).values_list("slug", flat=True)

            # Find maximum suffix
            max_num = 0
            for slug in existing_slugs:
                try:
                    num = int(slug.split("-")[-1])
                    if num > max_num:
                        max_num = num
                except ValueError:
                    pass

            row.slug = f"{base_slug}-{max_num + 1}"
            row.save(update_fields=["slug"])


class Migration(migrations.Migration):
    dependencies = [
        ("structure", "0044_remove_customer_inet_accesssubnet"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="slug",
            field=models.SlugField(blank=True, editable=False),
        ),
        migrations.AddField(
            model_name="project",
            name="slug",
            field=models.SlugField(blank=True, editable=False),
        ),
        migrations.RunPython(fill_slug, elidable=True),
        migrations.AlterField(
            model_name="customer",
            name="slug",
            field=models.SlugField(editable=False),
        ),
        migrations.AlterField(
            model_name="project",
            name="slug",
            field=models.SlugField(editable=False),
        ),
    ]