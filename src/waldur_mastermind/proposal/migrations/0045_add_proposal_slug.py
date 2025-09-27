# Generated manually for adding slug field to Proposal model

from django.db import migrations, models


def generate_slugs_for_existing_proposals(apps, schema_editor):
    """Generate slugs for existing Proposal objects."""
    Proposal = apps.get_model("proposal", "Proposal")

    # Group proposals by round
    rounds_proposals = {}
    for proposal in Proposal.objects.all():
        round_id = proposal.round_id
        if round_id not in rounds_proposals:
            rounds_proposals[round_id] = []
        rounds_proposals[round_id].append(proposal)

    # Generate slugs for each round's proposals
    for round_id, proposals in rounds_proposals.items():
        # Get the round's slug
        Round = apps.get_model("proposal", "Round")
        try:
            round_obj = Round.objects.get(pk=round_id)
            round_slug = round_obj.slug if round_obj.slug else "proposal"
        except Round.DoesNotExist:
            round_slug = "proposal"

        # Assign sequential slugs
        for counter, proposal in enumerate(proposals, start=1):
            if not proposal.slug:
                proposal.slug = f"{round_slug}-{counter}"
                proposal.save(update_fields=["slug"])


def reverse_slug_generation(apps, schema_editor):
    """Reverse operation - does nothing as we're removing the field."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0044_add_round_slug"),
    ]

    operations = [
        # Add slug field as nullable first
        migrations.AddField(
            model_name="proposal",
            name="slug",
            field=models.SlugField(null=True, blank=True),
        ),
        # Generate slugs for existing proposals
        migrations.RunPython(
            generate_slugs_for_existing_proposals, reverse_slug_generation
        ),
        # Make slug field non-nullable
        migrations.AlterField(
            model_name="proposal",
            name="slug",
            field=models.SlugField(),
        ),
    ]
