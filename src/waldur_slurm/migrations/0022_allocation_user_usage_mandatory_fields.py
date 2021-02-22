import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_slurm', '0021_delete_allocation_usage'),
    ]

    operations = [
        migrations.AlterField(
            model_name='allocationuserusage',
            name='allocation',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to='waldur_slurm.Allocation',
            ),
        ),
        migrations.AlterField(
            model_name='allocationuserusage',
            name='month',
            field=models.PositiveSmallIntegerField(
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(12),
                ]
            ),
        ),
        migrations.AlterField(
            model_name='allocationuserusage',
            name='year',
            field=models.PositiveSmallIntegerField(),
        ),
    ]
