from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0051_remove_offering_allowed_customers'),
    ]

    operations = [
        migrations.AlterField(
            model_name='offeringcomponent',
            name='billing_type',
            field=models.CharField(
                choices=[
                    ('fixed', 'Fixed-price'),
                    ('usage', 'Usage-based'),
                    ('limit', 'Limit-based'),
                    ('one', 'One-time'),
                    ('few', 'One-time on plan switch'),
                ],
                default='fixed',
                max_length=5,
            ),
        ),
        migrations.RemoveField(
            model_name='offeringcomponent',
            name='disable_quotas',
        ),
        migrations.RemoveField(
            model_name='offeringcomponent',
            name='use_limit_for_billing',
        ),
    ]
