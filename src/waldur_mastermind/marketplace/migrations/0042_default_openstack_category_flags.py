from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0041_drop_package'),
    ]

    operations = [
        migrations.AddField(
            model_name='category',
            name='default_vm_category',
            field=models.BooleanField(
                default=False,
                help_text='Set to "true" if this category is for OpenStack VM. Only one category can have "true" value.',
            ),
        ),
        migrations.AddField(
            model_name='category',
            name='default_volume_category',
            field=models.BooleanField(
                default=False,
                help_text='Set to true if this category is for OpenStack Volume. Only one category can have "true" value.',
            ),
        ),
    ]
