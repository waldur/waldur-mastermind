import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('waldur_slurm', '0017_association'),
    ]

    operations = [
        migrations.AddField(
            model_name='allocationuserusage',
            name='allocation',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to='waldur_slurm.Allocation',
            ),
        ),
        migrations.AddField(
            model_name='allocationuserusage',
            name='month',
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(12),
                ],
            ),
        ),
        migrations.AddField(
            model_name='allocationuserusage',
            name='year',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
