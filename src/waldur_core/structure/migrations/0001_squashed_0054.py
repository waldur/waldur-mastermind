# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from uuid import uuid4

import django.core.validators
import django.utils.timezone
import django_fsm
import model_utils.fields
import taggit.managers
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.validators
import waldur_core.structure.images
import waldur_core.structure.models


def create_quotas(apps, schema_editor):
    Project = apps.get_model('structure', 'Project')
    Customer = apps.get_model('structure', 'Customer')
    Quota = apps.get_model('quotas', 'Quota')

    # We can not use model constants in migrations because they can be changed in future
    quota_name_map = {
        Project: 'nc_global_project_count',
        Customer: 'nc_global_customer_count',
    }

    for model in [Project, Customer]:
        name = quota_name_map[model]
        usage = model.objects.count()
        if not Quota.objects.filter(name=name, object_id__isnull=True).exists():
            Quota.objects.create(uuid=uuid4().hex, name=name, usage=usage)
        else:
            Quota.objects.filter(name=name, object_id__isnull=True).update(usage=usage)


class Migration(migrations.Migration):
    replaces = [('structure', '0001_squashed_0021_balancehistory'),
                ('structure', '0001_initial'),
                ('structure', '0002_customer_native_name'),
                ('structure', '0003_protect_non_empty_customers'), ('structure', '0004_init_new_quotas'),
                ('structure', '0005_init_customers_quotas'), ('structure', '0006_inherit_namemixin'),
                ('structure', '0007_add_service_model'), ('structure', '0008_add_customer_billing_fields'),
                ('structure', '0009_update_service_models'), ('structure', '0010_add_oracle_service_type'),
                ('structure', '0011_customer_registration_code'), ('structure', '0012_customer_image'),
                ('structure', '0013_servicesettings_customer'), ('structure', '0014_servicesettings_options'),
                ('structure', '0015_drop_service_polymorphic'), ('structure', '0016_init_nc_resource_count_quotas'),
                ('structure', '0017_add_azure_service_type'), ('structure', '0018_service_settings_plural_form'),
                ('structure', '0019_rename_nc_service_count_to_nc_service_project_link_count'),
                ('structure', '0020_servicesettings_certificate'),
                ('structure', '0021_balancehistory'),
                ('structure', '0022_init_global_count_quotas'),
                ('structure', '0023_add_creation_state'), ('structure', '0024_add_sugarcrm_to_settings'),
                ('structure', '0025_add_zabbix_to_settings'), ('structure', '0026_add_error_message'),
                ('structure', '0027_servicesettings_service_type'), ('structure', '0028_servicesettings_service_type2'),
                ('structure', '0031_add_options_default'), ('structure', '0032_make_options_optional'),
                ('structure', '0033_remove_servicesettings_dummy'),
                ('structure', '0034_change_service_settings_state_field'),
                ('structure', '0035_settings_tags_and_scope'), ('structure', '0036_add_vat_fields'),
                ('structure', '0037_remove_customer_billing_backend_id'),
                ('structure', '0038_add_project_and_customer_permissions'),
                ('structure', '0039_remove_permission_groups'), ('structure', '0040_make_is_active_nullable'),
                ('structure', '0041_servicesettings_domain'),
                ('structure', '0042_add_service_certification_homepage_and_terms'),
                ('structure', '0043_servicesettings_geolocations'), ('structure', '0044_terms_of_services_url'),
                ('structure', '0045_project_services_certifications'),
                ('structure', '0046_shared_service_settings_customer'),
                ('structure', '0047_privateservicesettings_sharedservicesettings'),
                ('structure', '0048_remove_balance'), ('structure', '0049_extend_abbreviation'),
                ('structure', '0050_reset_cloud_spl_quota_limits'),
                ('structure', '0051_add_customer_email_phone_agreement_number'), ('structure', '0052_customer_subnets'),
                ('structure', '0053_add_project_type'), ('structure', '0054_payment_details')]

    initial = True

    dependencies = [
        ('quotas', '0004_quota_threshold'),
        ('taggit', '0002_auto_20150616_2121'),
        ('contenttypes', '0002_remove_content_type_name'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Customer',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('image', models.ImageField(blank=True, null=True, upload_to=waldur_core.structure.images.get_upload_path)),
                ('vat_code', models.CharField(blank=True, help_text='VAT number', max_length=20)),
                ('vat_name', models.CharField(blank=True, help_text='Optional business name retrieved for the VAT number.', max_length=255)),
                ('vat_address', models.CharField(blank=True, help_text='Optional business address retrieved for the VAT number.', max_length=255)),
                ('is_company', models.BooleanField(default=False, help_text='Is company or private person')),
                ('country', waldur_core.core.fields.CountryField(blank=True, choices=[('AF', 'Afghanistan'), ('AX', '\xc5land Islands'), ('AL', 'Albania'), ('DZ', 'Algeria'), ('AS', 'American Samoa'), ('AD', 'Andorra'), ('AO', 'Angola'), ('AI', 'Anguilla'), ('AQ', 'Antarctica'), ('AG', 'Antigua and Barbuda'), ('AR', 'Argentina'), ('AM', 'Armenia'), ('AW', 'Aruba'), ('AU', 'Australia'), ('AT', 'Austria'), ('AZ', 'Azerbaijan'), ('BS', 'Bahamas'), ('BH', 'Bahrain'), ('BD', 'Bangladesh'), ('BB', 'Barbados'), ('BY', 'Belarus'), ('BE', 'Belgium'), ('BZ', 'Belize'), ('BJ', 'Benin'), ('BM', 'Bermuda'), ('BT', 'Bhutan'), ('BO', 'Bolivia, Plurinational State of'), ('BQ', 'Bonaire, Sint Eustatius and Saba'), ('BA', 'Bosnia and Herzegovina'), ('BW', 'Botswana'), ('BV', 'Bouvet Island'), ('BR', 'Brazil'), ('IO', 'British Indian Ocean Territory'), ('BN', 'Brunei Darussalam'), ('BG', 'Bulgaria'), ('BF', 'Burkina Faso'), ('BI', 'Burundi'), ('KH', 'Cambodia'), ('CM', 'Cameroon'), ('CA', 'Canada'), ('CV', 'Cape Verde'), ('KY', 'Cayman Islands'), ('CF', 'Central African Republic'), ('TD', 'Chad'), ('CL', 'Chile'), ('CN', 'China'), ('CX', 'Christmas Island'), ('CC', 'Cocos (Keeling) Islands'), ('CO', 'Colombia'), ('KM', 'Comoros'), ('CG', 'Congo'), ('CD', 'Congo, The Democratic Republic of the'), ('CK', 'Cook Islands'), ('CR', 'Costa Rica'), ('CI', "C\xf4te d'Ivoire"), ('HR', 'Croatia'), ('CU', 'Cuba'), ('CW', 'Cura\xe7ao'), ('CY', 'Cyprus'), ('CZ', 'Czech Republic'), ('DK', 'Denmark'), ('DJ', 'Djibouti'), ('DM', 'Dominica'), ('DO', 'Dominican Republic'), ('EC', 'Ecuador'), ('EG', 'Egypt'), ('SV', 'El Salvador'), ('GQ', 'Equatorial Guinea'), ('ER', 'Eritrea'), ('EE', 'Estonia'), ('ET', 'Ethiopia'), ('FK', 'Falkland Islands (Malvinas)'), ('FO', 'Faroe Islands'), ('FJ', 'Fiji'), ('FI', 'Finland'), ('FR', 'France'), ('GF', 'French Guiana'), ('PF', 'French Polynesia'), ('TF', 'French Southern Territories'), ('GA', 'Gabon'), ('GM', 'Gambia'), ('GE', 'Georgia'), ('DE', 'Germany'), ('GH', 'Ghana'), ('GI', 'Gibraltar'), ('GR', 'Greece'), ('GL', 'Greenland'), ('GD', 'Grenada'), ('GP', 'Guadeloupe'), ('GU', 'Guam'), ('GT', 'Guatemala'), ('GG', 'Guernsey'), ('GN', 'Guinea'), ('GW', 'Guinea-Bissau'), ('GY', 'Guyana'), ('HT', 'Haiti'), ('HM', 'Heard Island and McDonald Islands'), ('VA', 'Holy See (Vatican City State)'), ('HN', 'Honduras'), ('HK', 'Hong Kong'), ('HU', 'Hungary'), ('IS', 'Iceland'), ('IN', 'India'), ('ID', 'Indonesia'), ('IR', 'Iran, Islamic Republic of'), ('IQ', 'Iraq'), ('IE', 'Ireland'), ('IM', 'Isle of Man'), ('IL', 'Israel'), ('IT', 'Italy'), ('JM', 'Jamaica'), ('JP', 'Japan'), ('JE', 'Jersey'), ('JO', 'Jordan'), ('KZ', 'Kazakhstan'), ('KE', 'Kenya'), ('KI', 'Kiribati'), ('KP', "Korea, Democratic People's Republic of"), ('KR', 'Korea, Republic of'), ('KW', 'Kuwait'), ('KG', 'Kyrgyzstan'), ('LA', "Lao People's Democratic Republic"), ('LV', 'Latvia'), ('LB', 'Lebanon'), ('LS', 'Lesotho'), ('LR', 'Liberia'), ('LY', 'Libya'), ('LI', 'Liechtenstein'), ('LT', 'Lithuania'), ('LU', 'Luxembourg'), ('MO', 'Macao'), ('MK', 'Macedonia, Republic of'), ('MG', 'Madagascar'), ('MW', 'Malawi'), ('MY', 'Malaysia'), ('MV', 'Maldives'), ('ML', 'Mali'), ('MT', 'Malta'), ('MH', 'Marshall Islands'), ('MQ', 'Martinique'), ('MR', 'Mauritania'), ('MU', 'Mauritius'), ('YT', 'Mayotte'), ('MX', 'Mexico'), ('FM', 'Micronesia, Federated States of'), ('MD', 'Moldova, Republic of'), ('MC', 'Monaco'), ('MN', 'Mongolia'), ('ME', 'Montenegro'), ('MS', 'Montserrat'), ('MA', 'Morocco'), ('MZ', 'Mozambique'), ('MM', 'Myanmar'), ('NA', 'Namibia'), ('NR', 'Nauru'), ('NP', 'Nepal'), ('NL', 'Netherlands'), ('NC', 'New Caledonia'), ('NZ', 'New Zealand'), ('NI', 'Nicaragua'), ('NE', 'Niger'), ('NG', 'Nigeria'), ('NU', 'Niue'), ('NF', 'Norfolk Island'), ('MP', 'Northern Mariana Islands'), ('NO', 'Norway'), ('OM', 'Oman'), ('PK', 'Pakistan'), ('PW', 'Palau'), ('PS', 'Palestine, State of'), ('PA', 'Panama'), ('PG', 'Papua New Guinea'), ('PY', 'Paraguay'), ('PE', 'Peru'), ('PH', 'Philippines'), ('PN', 'Pitcairn'), ('PL', 'Poland'), ('PT', 'Portugal'), ('PR', 'Puerto Rico'), ('QA', 'Qatar'), ('RE', 'R\xe9union'), ('RO', 'Romania'), ('RU', 'Russian Federation'), ('RW', 'Rwanda'), ('BL', 'Saint Barth\xe9lemy'), ('SH', 'Saint Helena, Ascension and Tristan da Cunha'), ('KN', 'Saint Kitts and Nevis'), ('LC', 'Saint Lucia'), ('MF', 'Saint Martin (French part)'), ('PM', 'Saint Pierre and Miquelon'), ('VC', 'Saint Vincent and the Grenadines'), ('WS', 'Samoa'), ('SM', 'San Marino'), ('ST', 'Sao Tome and Principe'), ('SA', 'Saudi Arabia'), ('SN', 'Senegal'), ('RS', 'Serbia'), ('SC', 'Seychelles'), ('SL', 'Sierra Leone'), ('SG', 'Singapore'), ('SX', 'Sint Maarten (Dutch part)'), ('SK', 'Slovakia'), ('SI', 'Slovenia'), ('SB', 'Solomon Islands'), ('SO', 'Somalia'), ('ZA', 'South Africa'), ('GS', 'South Georgia and the South Sandwich Islands'), ('ES', 'Spain'), ('LK', 'Sri Lanka'), ('SD', 'Sudan'), ('SR', 'Suriname'), ('SS', 'South Sudan'), ('SJ', 'Svalbard and Jan Mayen'), ('SZ', 'Swaziland'), ('SE', 'Sweden'), ('CH', 'Switzerland'), ('SY', 'Syrian Arab Republic'), ('TW', 'Taiwan, Province of China'), ('TJ', 'Tajikistan'), ('TZ', 'Tanzania, United Republic of'), ('TH', 'Thailand'), ('TL', 'Timor-Leste'), ('TG', 'Togo'), ('TK', 'Tokelau'), ('TO', 'Tonga'), ('TT', 'Trinidad and Tobago'), ('TN', 'Tunisia'), ('TR', 'Turkey'), ('TM', 'Turkmenistan'), ('TC', 'Turks and Caicos Islands'), ('TV', 'Tuvalu'), ('UG', 'Uganda'), ('UA', 'Ukraine'), ('AE', 'United Arab Emirates'), ('GB', 'United Kingdom'), ('US', 'United States'), ('UM', 'United States Minor Outlying Islands'), ('UY', 'Uruguay'), ('UZ', 'Uzbekistan'), ('VU', 'Vanuatu'), ('VE', 'Venezuela, Bolivarian Republic of'), ('VN', 'Viet Nam'), ('VG', 'Virgin Islands, British'), ('VI', 'Virgin Islands, U.S.'), ('WF', 'Wallis and Futuna'), ('EH', 'Western Sahara'), ('YE', 'Yemen'), ('ZM', 'Zambia'), ('ZW', 'Zimbabwe')], max_length=2)),
                ('native_name', models.CharField(blank=True, default='', max_length=160)),
                ('abbreviation', models.CharField(blank=True, max_length=12)),
                ('contact_details', models.TextField(blank=True, validators=[django.core.validators.MaxLengthValidator(500)])),
                ('agreement_number', models.PositiveIntegerField(blank=True, null=True, unique=True)),
                ('email', models.EmailField(blank=True, max_length=75, verbose_name='email address')),
                ('phone_number', models.CharField(blank=True, max_length=255, verbose_name='phone number')),
                ('access_subnets', models.TextField(blank=True, default='', help_text='Enter a comma separated list of IPv4 or IPv6 CIDR addresses from where connection to self-service is allowed.', validators=[waldur_core.core.validators.validate_cidr_list])),
                ('registration_code', models.CharField(blank=True, default='', max_length=160)),
                ('type', models.CharField(blank=True, max_length=150)),
                ('address', models.CharField(blank=True, max_length=300)),
                ('postal', models.CharField(blank=True, max_length=20)),
                ('bank_name', models.CharField(blank=True, max_length=150)),
                ('bank_account', models.CharField(blank=True, max_length=50)),
                ('accounting_start_date', models.DateTimeField(default=django.utils.timezone.now, verbose_name='Start date of accounting')),
                ('default_tax_percent', models.DecimalField(decimal_places=2, default=0, max_digits=4, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
            ],
            options={
                'verbose_name': 'organization',
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.structure.models.PermissionMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='CustomerPermission',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False)),
                ('expiration_time', models.DateTimeField(blank=True, null=True)),
                ('is_active', models.NullBooleanField(db_index=True, default=True)),
                ('role', waldur_core.structure.models.CustomerRole(choices=[('owner', 'Owner'), ('support', 'Support')], db_index=True, max_length=30)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('customer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='permissions', to='structure.Customer', verbose_name='organization')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='Project',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False, verbose_name='created')),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, editable=False, verbose_name='modified')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('uuid', waldur_core.core.fields.UUIDField()),
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.structure.models.PermissionMixin, waldur_core.structure.models.StructureLoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='ProjectPermission',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, editable=False)),
                ('expiration_time', models.DateTimeField(blank=True, null=True)),
                ('is_active', models.NullBooleanField(db_index=True, default=True)),
                ('role', waldur_core.structure.models.ProjectRole(choices=[('admin', 'Administrator'), ('manager', 'Manager'), ('support', 'Support')], db_index=True, max_length=30)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='permissions', to='structure.Project')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='ProjectType',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('uuid', waldur_core.core.fields.UUIDField()),
            ],
            options={
                'ordering': ['name'],
                'verbose_name': 'Project type',
                'verbose_name_plural': 'Project types',
            },
        ),
        migrations.CreateModel(
            name='ServiceCertification',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.CharField(blank=True, max_length=500, verbose_name='description')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('link', models.URLField(blank=True, max_length=255)),
                ('name', models.CharField(max_length=150, unique=True, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
            ],
            options={
                'ordering': ['-name'],
                'verbose_name': 'Service Certification',
                'verbose_name_plural': 'Service Certifications',
            },
        ),
        migrations.CreateModel(
            name='ServiceSettings',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150, validators=[waldur_core.core.validators.validate_name], verbose_name='name')),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('state', django_fsm.FSMIntegerField(choices=[(5, 'Creation Scheduled'), (6, 'Creating'), (1, 'Update Scheduled'), (2, 'Updating'), (7, 'Deletion Scheduled'), (8, 'Deleting'), (3, 'OK'), (4, 'Erred')], default=5)),
                ('backend_url', waldur_core.core.fields.BackendURLField(blank=True, null=True)),
                ('username', models.CharField(blank=True, max_length=100, null=True)),
                ('password', models.CharField(blank=True, max_length=100, null=True)),
                ('domain', models.CharField(blank=True, max_length=200, null=True)),
                ('token', models.CharField(blank=True, max_length=255, null=True)),
                ('certificate', models.FileField(blank=True, null=True, upload_to='certs', validators=[waldur_core.core.validators.FileTypeValidator(allowed_extensions=['pem'], allowed_types=['application/x-pem-file', 'application/x-x509-ca-cert', 'text/plain'])])),
                ('type', models.CharField(db_index=True, max_length=255, validators=[waldur_core.structure.models.validate_service_type])),
                ('options', waldur_core.core.fields.JSONField(blank=True, default={}, help_text='Extra options')),
                ('geolocations', waldur_core.core.fields.JSONField(blank=True, default=[], help_text='List of latitudes and longitudes. For example: [{"latitude": 123, "longitude": 345}, {"latitude": 456, "longitude": 678}]')),
                ('shared', models.BooleanField(default=False, help_text='Anybody can use it')),
                ('homepage', models.URLField(blank=True, max_length=255)),
                ('terms_of_services', models.URLField(blank=True, max_length=255)),
                ('object_id', models.PositiveIntegerField(null=True)),
                ('certifications', models.ManyToManyField(blank=True, related_name='service_settings', to='structure.ServiceCertification')),
                ('content_type', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='contenttypes.ContentType')),
                ('customer', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='service_settings', to='structure.Customer', verbose_name='organization')),
                ('tags', taggit.managers.TaggableManager(blank=True, help_text='A comma-separated list of tags.', through='taggit.TaggedItem', to='taggit.Tag', verbose_name='Tags')),
            ],
            options={
                'verbose_name': 'Service settings',
                'verbose_name_plural': 'Service settings',
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.AddField(
            model_name='project',
            name='certifications',
            field=models.ManyToManyField(blank=True, related_name='projects', to='structure.ServiceCertification'),
        ),
        migrations.AddField(
            model_name='project',
            name='customer',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='projects', to='structure.Customer', verbose_name='organization'),
        ),
        migrations.AddField(
            model_name='project',
            name='type',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='structure.ProjectType', verbose_name='project type'),
        ),
        migrations.CreateModel(
            name='PrivateServiceSettings',
            fields=[
            ],
            options={
                'proxy': True,
                'verbose_name_plural': 'Private provider settings',
                'indexes': [],
            },
            bases=('structure.servicesettings',),
        ),
        migrations.CreateModel(
            name='SharedServiceSettings',
            fields=[
            ],
            options={
                'proxy': True,
                'verbose_name_plural': 'Shared provider settings',
                'indexes': [],
            },
            bases=('structure.servicesettings',),
        ),
        migrations.AlterUniqueTogether(
            name='projectpermission',
            unique_together=set([('project', 'role', 'user', 'is_active')]),
        ),
        migrations.AlterUniqueTogether(
            name='customerpermission',
            unique_together=set([('customer', 'role', 'user', 'is_active')]),
        ),
        migrations.RunPython(create_quotas),
    ]
