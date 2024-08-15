from django.db import migrations, models

from waldur_core.core.models import generate_slug


def fill_slug(apps, schema_editor):
    User = apps.get_model("core", "User")
    for row in User.objects.all():
        row.slug = generate_slug(row.username, User)
        row.save(update_fields=["slug"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_user_identity_source_alter_user_registration_method"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="slug",
            field=models.SlugField(blank=True, editable=False),
        ),
        migrations.RunPython(fill_slug, elidable=True),
        migrations.AlterField(
            model_name="user",
            name="slug",
            field=models.SlugField(),
        ),
    ]
