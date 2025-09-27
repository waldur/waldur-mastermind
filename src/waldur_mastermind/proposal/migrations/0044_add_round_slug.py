# Generated manually for adding slug field to Round model

from django.db import migrations, models


def generate_slugs_for_existing_rounds(apps, schema_editor):
    """Generate slugs for existing Round objects."""
    Round = apps.get_model("proposal", "Round")

    for round_obj in Round.objects.all():
        if not round_obj.slug:
            # Generate slug from organization, call, and round start date
            org_slug = round_obj.call.manager.customer.slug or "org"
            call_slug = round_obj.call.slug or "call"
            round_date = round_obj.start_time.strftime("%Y%m%d")
            base_slug = f"{org_slug}-{call_slug}-{round_date}"

            # Ensure uniqueness
            slug = base_slug
            counter = 1
            while Round.objects.filter(slug=slug).exclude(pk=round_obj.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1

            round_obj.slug = slug
            round_obj.save(update_fields=["slug"])


def reverse_slug_generation(apps, schema_editor):
    """Reverse operation - does nothing as we're removing the field."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0043_alter_call_state_alter_proposal_state_and_more"),
    ]

    operations = [
        # Add slug field as nullable first
        migrations.AddField(
            model_name="round",
            name="slug",
            field=models.SlugField(null=True, blank=True),
        ),
        # Generate slugs for existing rounds
        migrations.RunPython(
            generate_slugs_for_existing_rounds, reverse_slug_generation
        ),
        # Make slug field non-nullable
        migrations.AlterField(
            model_name="round",
            name="slug",
            field=models.SlugField(),
        ),
    ]
