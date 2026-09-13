from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0289_resource_member_sync_report"),
    ]

    operations = [
        migrations.AddField(
            model_name="serviceprovider",
            name="account_username_anonymized_prefix",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Provider-level default prefix for anonymized usernames, which "
                    "are the prefix followed by the account's POSIX UID. Blank means "
                    "each offering decides for itself."
                ),
                max_length=100,
            ),
        ),
        migrations.AddConstraint(
            model_name="serviceprovideraccount",
            constraint=models.UniqueConstraint(
                condition=models.Q(("username__isnull", False))
                & ~models.Q(("username", "")),
                fields=("service_provider", "username"),
                name="marketplace_spaccount_unique_username",
            ),
        ),
    ]
