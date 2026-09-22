from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0296_clean_noop_price_logs"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="placed_automatically",
            field=models.BooleanField(
                default=False,
                editable=False,
                help_text="The order was placed by an automated flow on behalf of the person in created_by, rather than by that person. Proposal allocation sets this: the call review authorised the spend and the accepting call manager is recorded as the consumer reviewer, while created_by only names who the order is for. Such an order is not announced as a new order, and is carried out with system authority, since the person named need hold no role on the project.",
            ),
        ),
    ]
