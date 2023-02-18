from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('invoices', '0060_alter_paymentprofile_is_active'),
    ]

    operations = [
        migrations.RenameField(
            model_name='invoice',
            old_name='current_cost',
            new_name='total_cost',
        ),
        migrations.AlterField(
            model_name='invoice',
            name='total_cost',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                editable=False,
                help_text='Cached value for total cost.',
                max_digits=10,
            ),
        ),
    ]
