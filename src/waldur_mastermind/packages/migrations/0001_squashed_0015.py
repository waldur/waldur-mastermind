# -*- coding: utf-8 -*-
from decimal import Decimal
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
import waldur_core.core.fields
import waldur_core.core.validators


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('openstack', '0001_squashed_0042'),
        ('structure', '0001_squashed_0054'),
    ]

    operations = [
        migrations.CreateModel(
            name='PackageComponent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('type', models.CharField(choices=[('ram', 'RAM, MB'), ('cores', 'Cores'), ('storage', 'Storage, MB')], max_length=50)),
                ('amount', models.PositiveIntegerField(default=0)),
                ('price', models.DecimalField(decimal_places=7, default=0, help_text='The price per unit of amount', max_digits=13, validators=[django.core.validators.MinValueValidator(Decimal('0'))], verbose_name='Price per hour')),
            ],
        ),
        migrations.CreateModel(
            name='PackageTemplate',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('icon_url', models.URLField(blank=True, verbose_name='icon url')),
                ('uuid', waldur_core.core.fields.UUIDField()),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='packagecomponent',
            name='template',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='components', to='packages.PackageTemplate'),
        ),
        migrations.AlterField(
            model_name='packagecomponent',
            name='price',
            field=models.DecimalField(decimal_places=10, default=0, max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0'))], verbose_name='Price per unit per day'),
        ),
        migrations.AlterUniqueTogether(
            name='packagecomponent',
            unique_together=set([('type', 'template')]),
        ),
        migrations.CreateModel(
            name='OpenStackPackage',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('service_settings', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='structure.ServiceSettings')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='packagetemplate',
            name='service_settings',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='structure.ServiceSettings'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='openstackpackage',
            name='template',
            field=models.ForeignKey(help_text='Tenant will be created based on this template.', on_delete=django.db.models.deletion.CASCADE, related_name='openstack_packages', to='packages.PackageTemplate'),
        ),
        migrations.AddField(
            model_name='openstackpackage',
            name='tenant',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='openstack.Tenant'),
        ),
        migrations.AlterModelOptions(
            name='openstackpackage',
            options={'verbose_name': 'OpenStack VPC package', 'verbose_name_plural': 'OpenStack VPC packages'},
        ),
        migrations.AlterModelOptions(
            name='packagetemplate',
            options={'verbose_name': 'VPC package template', 'verbose_name_plural': 'VPC package templates'},
        ),
        migrations.AddField(
            model_name='packagetemplate',
            name='category',
            field=models.CharField(choices=[('small', 'Small'), ('medium', 'Medium'), ('large', 'Large'), ('trial', 'Trial')], default='small', max_length=10),
        ),
        migrations.AlterField(
            model_name='openstackpackage',
            name='service_settings',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='structure.ServiceSettings'),
        ),
        migrations.AlterField(
            model_name='packagecomponent',
            name='type',
            field=models.CharField(choices=[('ram', 'RAM'), ('cores', 'Cores'), ('storage', 'Storage')], max_length=50),
        ),
        migrations.AddField(
            model_name='packagetemplate',
            name='archived',
            field=models.BooleanField(default=False, help_text='Forbids creation of new packages.'),
        ),
        migrations.AlterField(
            model_name='openstackpackage',
            name='template',
            field=models.ForeignKey(help_text='Tenant will be created based on this template.', on_delete=django.db.models.deletion.PROTECT, related_name='openstack_packages', to='packages.PackageTemplate'),
        ),
        migrations.AddField(
            model_name='packagetemplate',
            name='product_code',
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AddField(
            model_name='packagetemplate',
            name='article_code',
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AlterField(
            model_name='openstackpackage',
            name='tenant',
            field=models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='openstack.Tenant'),
        ),
        migrations.AddField(
            model_name='packagetemplate',
            name='unit',
            field=models.CharField(choices=[('month', 'Per month'), ('half_month', 'Per half month'), ('day', 'Per day'), ('hour', 'Per hour'), ('quantity', 'Quantity')], default='day', max_length=30),
        ),
        migrations.AlterField(
            model_name='packagecomponent',
            name='price',
            field=models.DecimalField(decimal_places=10, default=0, max_digits=14, validators=[django.core.validators.MinValueValidator(Decimal('0'))], verbose_name='Price per unit'),
        ),
    ]
