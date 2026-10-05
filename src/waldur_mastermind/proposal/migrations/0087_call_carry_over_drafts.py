from django.db import migrations, models

HELP_TEXT = (
    "Whether a draft still open when its round's cut-off passes moves on to "
    "the call's next round instead of being cancelled. Without a later round "
    "the draft is cancelled either way."
)


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0086_proposal_admin_role_and_notified_roles"),
    ]

    operations = [
        # Calls that already exist keep cancelling their drafts at the cut-off,
        # as they did when they were set up; a manager opts them in. Calls
        # created from now on carry drafts over.
        migrations.AddField(
            model_name="call",
            name="carry_over_drafts",
            field=models.BooleanField(default=False, help_text=HELP_TEXT),
        ),
        migrations.AlterField(
            model_name="call",
            name="carry_over_drafts",
            field=models.BooleanField(default=True, help_text=HELP_TEXT),
        ),
    ]
