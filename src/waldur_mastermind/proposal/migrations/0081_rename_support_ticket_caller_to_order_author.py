import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Rename the call's ticket-caller setting to what it actually configures.

    0080 shipped this as ``support_ticket_caller``, which named an offering
    integration detail on a proposal-domain model. The setting decides whose
    name the orders placed when the call grants resources carry; a helpdesk
    ticket, for the offering types that raise one, follows from that. The
    values are unchanged, so a rename carries the data across.
    """

    dependencies = [
        ("proposal", "0080_call_support_ticket_caller"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name="call",
            old_name="support_ticket_caller",
            new_name="order_author",
        ),
        migrations.RenameField(
            model_name="call",
            old_name="support_ticket_caller_user",
            new_name="order_author_user",
        ),
        migrations.AlterField(
            model_name="call",
            name="order_author",
            field=models.CharField(
                choices=[
                    ("applicant", "Proposal applicant"),
                    ("project_manager", "Project manager"),
                    ("call_manager", "Call manager"),
                    ("specific_user", "Named contact"),
                ],
                default="applicant",
                help_text="Whose name the orders placed when this call grants resources carry. That person is who a helpdesk ticket is raised for and who Waldur's order mail is addressed to; reading the ticket in Waldur also needs a role on the project. The call review still authorises the spend, and the orders are still carried out with system authority.",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="call",
            name="order_author_user",
            field=models.ForeignKey(
                blank=True,
                help_text="The person orders are attributed to when the author is set to a named contact. Useful for routing a whole call to a shared mailbox. Must hold a role on this call or on the organisation managing it.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
