from __future__ import unicode_literals

from collections import defaultdict
import json
import logging

from django.conf import settings
from django.contrib import auth
from django.core import exceptions as django_exceptions
from django.core.validators import RegexValidator, MaxLengthValidator
from django.db import models as django_models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
import pyvat
from rest_framework import exceptions, serializers
from rest_framework.reverse import reverse
import six

from waldur_core.core import (models as core_models, fields as core_fields, serializers as core_serializers,
                              utils as core_utils)
from waldur_core.core.fields import MappedChoiceField
from waldur_core.monitoring.serializers import MonitoringSerializerMixin
from waldur_core.quotas import serializers as quotas_serializers
from waldur_core.structure import (models, SupportedServices, ServiceBackendError, ServiceBackendNotImplemented,
                                   executors)
from waldur_core.structure.managers import filter_queryset_for_user

User = auth.get_user_model()
logger = logging.getLogger(__name__)


class IpCountValidator(MaxLengthValidator):
    message = _('Only %(limit_value)s ip address is supported.')


class PermissionFieldFilteringMixin(object):
    """
    Mixin allowing to filter related fields.

    In order to constrain the list of entities that can be used
    as a value for the field:

    1. Make sure that the entity in question has corresponding
       Permission class defined.

    2. Implement `get_filtered_field_names()` method
       in the class that this mixin is mixed into and return
       the field in question from that method.
    """

    def get_fields(self):
        fields = super(PermissionFieldFilteringMixin, self).get_fields()

        try:
            request = self.context['request']
            user = request.user
        except (KeyError, AttributeError):
            return fields

        for field_name in self.get_filtered_field_names():
            if field_name not in fields:  # field could be not required by user
                continue
            field = fields[field_name]
            field.queryset = filter_queryset_for_user(field.queryset, user)

        return fields

    def get_filtered_field_names(self):
        raise NotImplementedError(
            'Implement get_filtered_field_names() '
            'to return list of filtered fields')


class PermissionListSerializer(serializers.ListSerializer):
    """
    Allows to filter related queryset by user.
    Counterpart of PermissionFieldFilteringMixin.

    In order to use it set Meta.list_serializer_class. Example:

    >>> class PermissionProjectSerializer(BasicProjectSerializer):
    >>>     class Meta(BasicProjectSerializer.Meta):
    >>>         list_serializer_class = PermissionListSerializer
    >>>
    >>> class CustomerSerializer(serializers.HyperlinkedModelSerializer):
    >>>     projects = PermissionProjectSerializer(many=True, read_only=True)
    """

    def to_representation(self, data):
        try:
            request = self.context['request']
            user = request.user
        except (KeyError, AttributeError):
            pass
        else:
            if isinstance(data, (django_models.Manager, django_models.query.QuerySet)):
                data = filter_queryset_for_user(data.all(), user)

        return super(PermissionListSerializer, self).to_representation(data)


class BasicUserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = User
        fields = ('url', 'uuid', 'username', 'full_name', 'native_name',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class BasicProjectSerializer(core_serializers.BasicInfoSerializer):
    class Meta(core_serializers.BasicInfoSerializer.Meta):
        model = models.Project


class PermissionProjectSerializer(BasicProjectSerializer):
    class Meta(BasicProjectSerializer.Meta):
        list_serializer_class = PermissionListSerializer


class NestedServiceProjectLinkSerializer(serializers.Serializer):
    uuid = serializers.ReadOnlyField(source='service.uuid')
    url = serializers.SerializerMethodField()
    service_project_link_url = serializers.SerializerMethodField()
    name = serializers.ReadOnlyField(source='service.settings.name')
    type = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    shared = serializers.SerializerMethodField()
    settings_uuid = serializers.ReadOnlyField(source='service.settings.uuid')
    settings = serializers.SerializerMethodField()
    validation_state = serializers.ChoiceField(
        choices=models.ServiceProjectLink.States.CHOICES,
        read_only=True,
        help_text=_('A state of service compliance with project requirements.'))
    validation_message = serializers.ReadOnlyField(
        help_text=_('An error message for a service that is non-compliant with project requirements.'))

    def get_settings(self, link):
        """
        URL of service settings
        """
        return reverse(
            'servicesettings-detail', kwargs={'uuid': link.service.settings.uuid}, request=self.context['request'])

    def get_url(self, link):
        """
        URL of service
        """
        view_name = SupportedServices.get_detail_view_for_model(link.service)
        return reverse(view_name, kwargs={'uuid': link.service.uuid.hex}, request=self.context['request'])

    def get_service_project_link_url(self, link):
        view_name = SupportedServices.get_detail_view_for_model(link)
        return reverse(view_name, kwargs={'pk': link.id}, request=self.context['request'])

    def get_type(self, link):
        return SupportedServices.get_name_for_model(link.service)

    # XXX: SPL is intended to become stateless. For backward compatiblity we are returning here state from connected
    # service settings. To be removed once SPL becomes stateless.
    def get_state(self, link):
        return link.service.settings.get_state_display()

    def get_resources_count(self, link):
        """
        Count total number of all resources connected to link
        """
        total = 0
        for model in SupportedServices.get_service_resources(link.service):
            # Format query path from resource to service project link
            query = {model.Permissions.project_path.split('__')[0]: link}
            total += model.objects.filter(**query).count()
        return total

    def get_shared(self, link):
        return link.service.settings.shared


class NestedServiceCertificationSerializer(core_serializers.AugmentedSerializerMixin,
                                           core_serializers.HyperlinkedRelatedModelSerializer):
    class Meta(object):
        model = models.ServiceCertification
        fields = ('uuid', 'url', 'name', 'description', 'link')
        read_only_fields = ('name', 'description', 'link')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class ProjectTypeSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.ProjectType
        fields = ('uuid', 'url', 'name', 'description')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'project_type-detail'},
        }


class ProjectSerializer(core_serializers.RestrictedSerializerMixin,
                        PermissionFieldFilteringMixin,
                        core_serializers.AugmentedSerializerMixin,
                        serializers.HyperlinkedModelSerializer):
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)
    services = serializers.SerializerMethodField()
    certifications = NestedServiceCertificationSerializer(
        queryset=models.ServiceCertification.objects.all(),
        many=True, required=False)

    class Meta(object):
        model = models.Project
        fields = (
            'url', 'uuid',
            'name',
            'customer', 'customer_uuid', 'customer_name', 'customer_native_name', 'customer_abbreviation',
            'description',
            'quotas',
            'services',
            'created',
            'certifications',
            'type', 'type_name',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
            'certifications': {'lookup_field': 'uuid'},
            'type': {'lookup_field': 'uuid', 'view_name': 'project_type-detail'},
        }
        related_paths = {
            'customer': ('uuid', 'name', 'native_name', 'abbreviation'),
            'type': ('name',),
        }
        protected_fields = ('certifications',)

    @staticmethod
    def eager_load(queryset, request=None):
        related_fields = (
            'uuid',
            'name',
            'created',
            'description',
            'customer__uuid',
            'customer__name',
            'customer__native_name',
            'customer__abbreviation',
        )
        return queryset.select_related('customer').only(*related_fields) \
            .prefetch_related('quotas', 'certifications')

    def create(self, validated_data):
        certifications = validated_data.pop('certifications', [])
        project = super(ProjectSerializer, self).create(validated_data)
        project.certifications.add(*certifications)

        return project

    def get_filtered_field_names(self):
        return 'customer',

    def get_services(self, project):
        if 'services' not in self.context:
            self.context['services'] = self.get_services_map()
        services = self.context['services'][project.pk]

        serializer = NestedServiceProjectLinkSerializer(
            services,
            many=True,
            read_only=True,
            context={'request': self.context['request']})
        return serializer.data

    def get_services_map(self):
        services = defaultdict(list)
        related_fields = (
            'id',
            'service__settings__state',
            'project_id',
            'service__uuid',
            'service__settings__uuid',
            'service__settings__shared',
            'service__settings__name',
        )
        for link_model in models.ServiceProjectLink.get_all_models():
            links = (link_model.objects.all()
                     .select_related('service', 'service__settings')
                     .only(*related_fields)
                     .prefetch_related('service__settings__certifications'))
            if isinstance(self.instance, list):
                links = links.filter(project__in=self.instance)
            else:
                links = links.filter(project=self.instance)
            for link in links:
                services[link.project_id].append(link)
        return services


class CustomerSerializer(core_serializers.RestrictedSerializerMixin,
                         core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer, ):
    projects = PermissionProjectSerializer(many=True, read_only=True)
    owners = BasicUserSerializer(source='get_owners', many=True, read_only=True)
    support_users = BasicUserSerializer(source='get_support_users', many=True, read_only=True)
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)

    COUNTRIES = core_fields.CountryField.COUNTRIES
    if settings.WALDUR_CORE.get('COUNTRIES'):
        COUNTRIES = [item for item in COUNTRIES if item[0] in settings.WALDUR_CORE['COUNTRIES']]
    country = serializers.ChoiceField(required=False, choices=COUNTRIES, allow_blank=True)
    country_name = serializers.ReadOnlyField(source='get_country_display')
    display_name = serializers.ReadOnlyField(source='get_display_name')

    class Meta(object):
        model = models.Customer
        fields = (
            'url',
            'uuid',
            'created',
            'name', 'native_name', 'abbreviation', 'contact_details',
            'domain', 'display_name',
            'agreement_number', 'email', 'phone_number', 'access_subnets',
            'projects',
            'owners', 'support_users',
            'registration_code', 'homepage',
            'quotas',
            'image',
            'country', 'country_name', 'vat_code', 'is_company',
            'type', 'postal', 'address', 'bank_name', 'bank_account',
            'default_tax_percent', 'accounting_start_date',
        )
        protected_fields = ('agreement_number',)
        read_only_fields = ('access_subnets', 'accounting_start_date', 'default_tax_percent')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_fields(self):
        fields = super(CustomerSerializer, self).get_fields()

        try:
            request = self.context['view'].request
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if not user.is_staff:
            if 'domain' in fields:
                fields['domain'].read_only = True

        return fields

    def create(self, validated_data):
        user = self.context['request'].user
        if 'domain' not in validated_data:
            # Staff can specify domain name on organization creation
            validated_data['domain'] = user.organization
        return super(CustomerSerializer, self).create(validated_data)

    @staticmethod
    def eager_load(queryset, request=None):
        return queryset.prefetch_related('quotas', 'projects')

    def validate(self, attrs):
        country = attrs.get('country')
        vat_code = attrs.get('vat_code')
        is_company = attrs.get('is_company')

        if vat_code:
            if not is_company:
                raise serializers.ValidationError({
                    'vat_code': _('VAT number is not supported for private persons.')})

            # Check VAT format
            if not pyvat.is_vat_number_format_valid(vat_code, country):
                raise serializers.ValidationError({'vat_code': _('VAT number has invalid format.')})

            # Check VAT number in EU VAT Information Exchange System
            # if customer is new or either VAT number or country of the customer has changed
            if not self.instance or self.instance.vat_code != vat_code or self.instance.country != country:
                check_result = pyvat.check_vat_number(vat_code, country)
                if check_result.is_valid:
                    attrs['vat_name'] = check_result.business_name
                    attrs['vat_address'] = check_result.business_address
                    if not attrs.get('contact_details'):
                        attrs['contact_details'] = attrs['vat_address']
                elif check_result.is_valid is False:
                    raise serializers.ValidationError({'vat_code': _('VAT number is invalid.')})
                else:
                    logger.debug('Unable to check VAT number %s for country %s. Error message: %s',
                                 vat_code, country, check_result.log_lines)
                    raise serializers.ValidationError({'vat_code': _('Unable to check VAT number.')})
        return attrs


class NestedProjectPermissionSerializer(serializers.ModelSerializer):
    url = serializers.HyperlinkedRelatedField(
        source='project',
        lookup_field='uuid',
        view_name='project-detail',
        queryset=models.Project.objects.all(),
    )
    uuid = serializers.ReadOnlyField(source='project.uuid')
    name = serializers.ReadOnlyField(source='project.name')
    permission = serializers.HyperlinkedRelatedField(
        source='pk',
        view_name='project_permission-detail',
        queryset=models.ProjectPermission.objects.all(),
    )

    class Meta:
        model = models.ProjectPermission
        fields = ['url', 'uuid', 'name', 'role', 'permission', 'expiration_time']


class CustomerUserSerializer(serializers.ModelSerializer):
    role = serializers.ReadOnlyField()
    expiration_time = serializers.ReadOnlyField(source='perm.expiration_time')
    permission = serializers.HyperlinkedRelatedField(
        source='perm.pk',
        view_name='customer_permission-detail',
        queryset=models.CustomerPermission.objects.all(),
    )
    projects = NestedProjectPermissionSerializer(many=True, read_only=True)

    class Meta:
        model = User
        fields = ['url', 'uuid', 'username', 'full_name', 'email', 'role', 'permission', 'projects',
                  'expiration_time']
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def to_representation(self, user):
        customer = self.context['customer']
        permission = models.CustomerPermission.objects.filter(
            customer=customer, user=user, is_active=True).first()
        projects = models.ProjectPermission.objects.filter(
            project__customer=customer, user=user, is_active=True)
        setattr(user, 'perm', permission)
        setattr(user, 'role', permission and permission.role)
        setattr(user, 'projects', projects)
        return super(CustomerUserSerializer, self).to_representation(user)


class ProjectUserSerializer(serializers.ModelSerializer):
    role = serializers.ReadOnlyField()
    expiration_time = serializers.ReadOnlyField(source='perm.expiration_time')
    permission = serializers.HyperlinkedRelatedField(
        source='perm.pk',
        view_name='project_permission-detail',
        queryset=models.ProjectPermission.objects.all(),
    )

    class Meta:
        model = User
        fields = ['url', 'uuid', 'username', 'full_name', 'email', 'role', 'permission',
                  'expiration_time']
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def to_representation(self, user):
        project = self.context['project']
        permission = models.ProjectPermission.objects.filter(
            project=project, user=user, is_active=True).first()
        setattr(user, 'perm', permission)
        setattr(user, 'role', permission and permission.role)
        return super(ProjectUserSerializer, self).to_representation(user)


class BasePermissionSerializer(core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer):
    class Meta(object):
        fields = ('user', 'user_full_name', 'user_native_name', 'user_username', 'user_uuid', 'user_email')
        related_paths = {
            'user': ('username', 'full_name', 'native_name', 'uuid', 'email'),
        }


class BasicCustomerPermissionSerializer(BasePermissionSerializer):
    class Meta(BasePermissionSerializer.Meta):
        model = models.CustomerPermission
        fields = (
            'url', 'pk', 'role', 'customer_uuid', 'customer_name', 'customer_native_name', 'customer_abbreviation',
        )
        related_paths = dict(
            customer=('name', 'native_name', 'abbreviation', 'uuid'),
            **BasePermissionSerializer.Meta.related_paths
        )
        extra_kwargs = {
            'customer': {
                'view_name': 'customer-detail',
                'lookup_field': 'uuid',
                'queryset': models.Customer.objects.all(),
            }
        }


class CustomerPermissionSerializer(PermissionFieldFilteringMixin, BasePermissionSerializer):
    class Meta(BasePermissionSerializer.Meta):
        model = models.CustomerPermission
        fields = (
            'url', 'pk', 'role', 'created', 'expiration_time', 'created_by',
            'customer', 'customer_uuid', 'customer_name', 'customer_native_name', 'customer_abbreviation',
        ) + BasePermissionSerializer.Meta.fields
        related_paths = dict(
            customer=('name', 'native_name', 'abbreviation', 'uuid'),
            **BasePermissionSerializer.Meta.related_paths
        )
        protected_fields = (
            'customer', 'role', 'user', 'created_by', 'created'
        )
        extra_kwargs = {
            'user': {
                'view_name': 'user-detail',
                'lookup_field': 'uuid',
                'queryset': User.objects.all(),
            },
            'created_by': {
                'view_name': 'user-detail',
                'lookup_field': 'uuid',
                'read_only': True,
            },
            'customer': {
                'view_name': 'customer-detail',
                'lookup_field': 'uuid',
                'queryset': models.Customer.objects.all(),
            }
        }

    def validate(self, data):
        if not self.instance:
            customer = data['customer']
            user = data['user']

            if customer.has_user(user):
                raise serializers.ValidationError(_('The fields customer and user must make a unique set.'))

        return data

    def create(self, validated_data):
        customer = validated_data['customer']
        user = validated_data['user']
        role = validated_data['role']
        expiration_time = validated_data.get('expiration_time')

        created_by = self.context['request'].user
        permission, _ = customer.add_user(user, role, created_by, expiration_time)

        return permission

    def validate_expiration_time(self, value):
        if value is not None and value < timezone.now():
            raise serializers.ValidationError(_('Expiration time should be greater than current time.'))
        return value

    def get_filtered_field_names(self):
        return ('customer',)


class CustomerPermissionLogSerializer(CustomerPermissionSerializer):
    class Meta(CustomerPermissionSerializer.Meta):
        view_name = 'customer_permission_log-detail'


class ProjectPermissionSerializer(PermissionFieldFilteringMixin, BasePermissionSerializer):
    customer_name = serializers.ReadOnlyField(source='project.customer.name')

    class Meta(BasePermissionSerializer.Meta):
        model = models.ProjectPermission
        fields = (
            'url', 'pk', 'role', 'created', 'expiration_time', 'created_by',
            'project', 'project_uuid', 'project_name', 'customer_name'
        ) + BasePermissionSerializer.Meta.fields
        related_paths = dict(
            project=('name', 'uuid'),
            **BasePermissionSerializer.Meta.related_paths
        )
        protected_fields = (
            'project', 'role', 'user', 'created_by', 'created'
        )
        extra_kwargs = {
            'user': {
                'view_name': 'user-detail',
                'lookup_field': 'uuid',
                'queryset': User.objects.all(),
            },
            'created_by': {
                'view_name': 'user-detail',
                'lookup_field': 'uuid',
                'read_only': True,
            },
            'project': {
                'view_name': 'project-detail',
                'lookup_field': 'uuid',
                'queryset': models.Project.objects.all(),
            }
        }

    def validate(self, data):
        if not self.instance:
            project = data['project']
            user = data['user']

            if project.has_user(user):
                raise serializers.ValidationError(_('The fields project and user must make a unique set.'))

        return data

    def create(self, validated_data):
        project = validated_data['project']
        user = validated_data['user']
        role = validated_data['role']
        expiration_time = validated_data.get('expiration_time')

        created_by = self.context['request'].user
        permission, _ = project.add_user(user, role, created_by, expiration_time)

        return permission

    def validate_expiration_time(self, value):
        if value is not None and value < timezone.now():
            raise serializers.ValidationError(_('Expiration time should be greater than current time.'))
        return value

    def get_filtered_field_names(self):
        return ('project',)


class BasicProjectPermissionSerializer(BasePermissionSerializer):
    class Meta(BasePermissionSerializer.Meta):
        model = models.ProjectPermission
        fields = (
            'url', 'pk', 'role', 'project_uuid', 'project_name',
        )
        related_paths = dict(
            project=('name', 'uuid'),
            **BasePermissionSerializer.Meta.related_paths
        )
        extra_kwargs = {
            'project': {
                'view_name': 'project-detail',
                'lookup_field': 'uuid',
                'queryset': models.Project.objects.all(),
            }
        }


class ProjectPermissionLogSerializer(ProjectPermissionSerializer):
    class Meta(ProjectPermissionSerializer.Meta):
        view_name = 'project_permission_log-detail'


class UserSerializer(serializers.HyperlinkedModelSerializer):
    email = serializers.EmailField()
    agree_with_policy = serializers.BooleanField(write_only=True, required=False,
                                                 help_text=_('User must agree with the policy to register.'))
    preferred_language = serializers.ChoiceField(choices=settings.LANGUAGES, allow_blank=True, required=False)
    competence = serializers.ChoiceField(choices=settings.WALDUR_CORE.get('USER_COMPETENCE_LIST', []),
                                         allow_blank=True,
                                         required=False)
    token = serializers.ReadOnlyField(source='auth_token.key')
    customer_permissions = serializers.SerializerMethodField()
    project_permissions = serializers.SerializerMethodField()

    def get_customer_permissions(self, user):
        permissions = models.CustomerPermission.objects.filter(user=user, is_active=True).select_related('customer')
        serializer = BasicCustomerPermissionSerializer(instance=permissions, many=True,
                                                       context=self.context)
        return serializer.data

    def get_project_permissions(self, user):
        permissions = models.ProjectPermission.objects.filter(user=user, is_active=True).select_related('project')
        serializer = BasicProjectPermissionSerializer(instance=permissions, many=True,
                                                      context=self.context)
        return serializer.data

    class Meta(object):
        model = User
        fields = (
            'url',
            'uuid', 'username',
            'full_name', 'native_name',
            'job_title', 'email', 'phone_number',
            'organization',
            'civil_number',
            'description',
            'is_staff', 'is_active', 'is_support',
            'token', 'token_lifetime',
            'registration_method',
            'date_joined',
            'agree_with_policy',
            'agreement_date',
            'preferred_language',
            'competence',
            'customer_permissions',
            'project_permissions',
        )
        read_only_fields = (
            'uuid',
            'civil_number',
            'registration_method',
            'date_joined',
            'agreement_date',
            'customer_permissions',
            'project_permissions',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_fields(self):
        fields = super(UserSerializer, self).get_fields()

        try:
            request = self.context['view'].request
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if not user.is_staff and not user.is_support:
            del fields['is_active']
            del fields['is_staff']
            del fields['description']

        if not self._can_see_token(user):
            del fields['token']
            del fields['token_lifetime']

        if request.method in ('PUT', 'PATCH'):
            fields['username'].read_only = True

        return fields

    def _can_see_token(self, user):
        # Staff can see any token
        # User can see his own token either via details view or /api/users/?current

        if user.is_staff:
            return True
        elif isinstance(self.instance, list) and len(self.instance) == 1:
            return self.instance[0] == user
        else:
            return self.instance == user

    def validate(self, attrs):
        agree_with_policy = attrs.pop('agree_with_policy', False)
        if self.instance and not self.instance.agreement_date:
            if not agree_with_policy:
                raise serializers.ValidationError({'agree_with_policy': _('User must agree with the policy.')})
            else:
                attrs['agreement_date'] = timezone.now()

        # Convert validation error from Django to DRF
        # https://github.com/tomchristie/django-rest-framework/issues/2145
        try:
            user = User(id=getattr(self.instance, 'id', None), **attrs)
            user.clean()
        except django_exceptions.ValidationError as error:
            raise exceptions.ValidationError(error.message_dict)
        return attrs


class CreationTimeStatsSerializer(serializers.Serializer):
    MODEL_NAME_CHOICES = (('project', 'project'), ('customer', 'customer'),)
    MODEL_CLASSES = {'project': models.Project, 'customer': models.Customer}

    model_name = serializers.ChoiceField(choices=MODEL_NAME_CHOICES)
    start_timestamp = serializers.IntegerField(min_value=0)
    end_timestamp = serializers.IntegerField(min_value=0)
    segments_count = serializers.IntegerField(min_value=0)

    def get_stats(self, user):
        start_datetime = core_utils.timestamp_to_datetime(self.data['start_timestamp'])
        end_datetime = core_utils.timestamp_to_datetime(self.data['end_timestamp'])

        model = self.MODEL_CLASSES[self.data['model_name']]
        filtered_queryset = filter_queryset_for_user(model.objects.all(), user)
        created_datetimes = (
            filtered_queryset
            .filter(created__gte=start_datetime, created__lte=end_datetime)
            .values('created')
            .annotate(count=django_models.Count('id', distinct=True)))

        time_and_value_list = [
            (core_utils.datetime_to_timestamp(dt['created']), dt['count']) for dt in created_datetimes]

        return core_utils.format_time_and_value_to_segment_list(
            time_and_value_list, self.data['segments_count'],
            self.data['start_timestamp'], self.data['end_timestamp'])


class PasswordSerializer(serializers.Serializer):
    password = serializers.CharField(min_length=7, validators=[
        RegexValidator(
            regex='\d',
            message=_('Ensure this field has at least one digit.'),
        ),
        RegexValidator(
            regex='[a-zA-Z]',
            message=_('Ensure this field has at least one latin letter.'),
        ),
    ])


class SshKeySerializer(serializers.HyperlinkedModelSerializer):
    user_uuid = serializers.ReadOnlyField(source='user.uuid')

    class Meta(object):
        model = core_models.SshPublicKey
        fields = ('url', 'uuid', 'name', 'public_key', 'fingerprint', 'user_uuid', 'is_shared')
        read_only_fields = ('fingerprint', 'is_shared')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def validate_public_key(self, value):
        value = value.strip()
        if len(value.splitlines()) > 1:
            raise serializers.ValidationError(_('Key is not valid: it should be single line.'))

        try:
            fingerprint = core_models.get_ssh_key_fingerprint(value)
        except (IndexError, TypeError):
            raise serializers.ValidationError(_('Key is not valid: cannot generate fingerprint from it.'))
        if core_models.SshPublicKey.objects.filter(fingerprint=fingerprint).exists():
            raise serializers.ValidationError(_('Key with same fingerprint already exists.'))
        return value

    def get_fields(self):
        fields = super(SshKeySerializer, self).get_fields()

        try:
            user = self.context['request'].user
        except (KeyError, AttributeError):
            return fields

        if not user.is_staff:
            del fields['user_uuid']

        return fields


class ServiceCertificationsUpdateSerializer(serializers.Serializer):
    certifications = NestedServiceCertificationSerializer(
        queryset=models.ServiceCertification.objects.all(),
        required=True,
        many=True)

    @transaction.atomic
    def update(self, instance, validated_data):
        certifications = validated_data.pop('certifications', None)
        instance.certifications.clear()
        instance.certifications.add(*certifications)
        return instance


class ServiceCertificationSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.ServiceCertification
        fields = ('uuid', 'url', 'name', 'description', 'link')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'service-certification-detail'},
        }


class ServiceSettingsSerializer(PermissionFieldFilteringMixin,
                                core_serializers.RestrictedSerializerMixin,
                                core_serializers.AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
    customer_native_name = serializers.ReadOnlyField(source='customer.native_name')
    state = MappedChoiceField(
        choices=[(v, k) for k, v in core_models.StateMixin.States.CHOICES],
        choice_mappings={v: k for k, v in core_models.StateMixin.States.CHOICES},
        read_only=True)
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)
    scope = core_serializers.GenericRelatedField(related_models=models.ResourceMixin.get_all_models(), required=False)
    certifications = NestedServiceCertificationSerializer(many=True, read_only=True)
    geolocations = core_serializers.GeoLocationField(read_only=True)

    class Meta(object):
        model = models.ServiceSettings
        fields = (
            'url', 'uuid', 'name', 'type', 'state', 'error_message', 'shared',
            'backend_url', 'username', 'password', 'token', 'certificate',
            'customer', 'customer_name', 'customer_native_name',
            'homepage', 'terms_of_services', 'certifications',
            'quotas', 'scope', 'geolocations',
        )
        protected_fields = ('type', 'customer')
        read_only_fields = ('shared', 'state', 'error_message')
        related_paths = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
            'certifications': {'lookup_field': 'uuid'},
        }
        write_only_fields = ('backend_url', 'username', 'token', 'password', 'certificate')
        for field in write_only_fields:
            field_params = extra_kwargs.setdefault(field, {})
            field_params['write_only'] = True

    def get_filtered_field_names(self):
        return 'customer',

    @staticmethod
    def eager_load(queryset, request=None):
        return queryset.select_related('customer').prefetch_related('quotas', 'certifications')

    def get_fields(self):
        fields = super(ServiceSettingsSerializer, self).get_fields()
        request = self.context['request']

        if isinstance(self.instance, self.Meta.model):
            if self.can_see_extra_fields():
                # If user can change settings he should be able to see value
                for field in self.Meta.write_only_fields:
                    fields[field].write_only = False

                serializer = self.get_service_serializer()

                # Remove fields if they are not needed for service
                filter_fields = serializer.SERVICE_ACCOUNT_FIELDS
                if filter_fields is not NotImplemented:
                    for field in self.Meta.write_only_fields:
                        if field in filter_fields:
                            fields[field].help_text = filter_fields[field]
                        elif field in fields:
                            del fields[field]

                # Add extra fields stored in options dictionary
                extra_fields = serializer.SERVICE_ACCOUNT_EXTRA_FIELDS
                if extra_fields is not NotImplemented:
                    for field in extra_fields:
                        fields[field] = serializers.CharField(required=False,
                                                              source='options.' + field,
                                                              allow_blank=True,
                                                              help_text=extra_fields[field])

        if request.method == 'GET':
            fields['type'] = serializers.ReadOnlyField(source='get_type_display')

        return fields

    def get_service_serializer(self):
        service = SupportedServices.get_service_models()[self.instance.type]['service']
        # Find service serializer by service type of settings object
        return next(cls for cls in BaseServiceSerializer.__subclasses__()
                    if cls.Meta.model == service)

    def can_see_extra_fields(self):
        request = self.context['request']

        if request.user.is_staff:
            return True

        if not self.instance.customer:
            return False

        return self.instance.customer.has_user(request.user, models.CustomerRole.OWNER)

    def update(self, instance, validated_data):
        if 'options' in validated_data:
            new_options = dict.copy(instance.options)
            new_options.update(validated_data['options'])
            validated_data['options'] = new_options

        return super(ServiceSettingsSerializer, self).update(instance, validated_data)


class ServiceSerializerMetaclass(serializers.SerializerMetaclass):
    """ Build a list of supported services via serializers definition.
        See SupportedServices for details.
    """

    def __new__(cls, name, bases, args):
        SupportedServices.register_service(args['Meta'].model)
        serializer = super(ServiceSerializerMetaclass, cls).__new__(cls, name, bases, args)
        SupportedServices.register_service_serializer(args['Meta'].model, serializer)
        return serializer


class BaseServiceSerializer(six.with_metaclass(ServiceSerializerMetaclass,
                                               PermissionFieldFilteringMixin,
                                               core_serializers.RestrictedSerializerMixin,
                                               core_serializers.AugmentedSerializerMixin,
                                               serializers.HyperlinkedModelSerializer)):
    SERVICE_ACCOUNT_FIELDS = NotImplemented
    SERVICE_ACCOUNT_EXTRA_FIELDS = NotImplemented

    projects = BasicProjectSerializer(many=True, read_only=True)
    customer_native_name = serializers.ReadOnlyField(source='customer.native_name')
    settings = serializers.HyperlinkedRelatedField(
        queryset=models.ServiceSettings.objects.filter(shared=True),
        view_name='servicesettings-detail',
        lookup_field='uuid',
        allow_null=True)
    # if project is defined service will be automatically connected to projects customer
    # and SPL between service and project will be created
    project = serializers.HyperlinkedRelatedField(
        queryset=models.Project.objects.all().select_related('customer'),
        view_name='project-detail',
        lookup_field='uuid',
        allow_null=True,
        required=False,
        write_only=True)

    backend_url = serializers.URLField(max_length=200, allow_null=True, write_only=True, required=False)
    username = serializers.CharField(max_length=100, allow_null=True, write_only=True, required=False)
    password = serializers.CharField(max_length=100, allow_null=True, write_only=True, required=False)
    domain = serializers.CharField(max_length=200, allow_null=True, write_only=True, required=False)
    token = serializers.CharField(allow_null=True, write_only=True, required=False)
    certificate = serializers.FileField(allow_null=True, write_only=True, required=False)
    resources_count = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    scope = core_serializers.GenericRelatedField(related_models=models.ResourceMixin.get_all_models(), required=False)
    tags = serializers.SerializerMethodField()
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)

    shared = serializers.ReadOnlyField(source='settings.shared')
    error_message = serializers.ReadOnlyField(source='settings.error_message')
    terms_of_services = serializers.ReadOnlyField(source='settings.terms_of_services')
    homepage = serializers.ReadOnlyField(source='settings.homepage')
    geolocations = core_serializers.GeoLocationField(source='settings.geolocations', read_only=True)
    certifications = NestedServiceCertificationSerializer(many=True, read_only=True, source='settings.certifications')
    name = serializers.ReadOnlyField(source='settings.name')

    class Meta(object):
        model = NotImplemented
        fields = (
            'uuid', 'url', 'name', 'state', 'service_type', 'shared',
            'projects', 'project',
            'customer', 'customer_uuid', 'customer_name', 'customer_native_name', 'resources_count',
            'settings', 'settings_uuid', 'backend_url', 'username', 'password',
            'token', 'certificate', 'domain', 'terms_of_services', 'homepage',
            'certifications', 'geolocations', 'available_for_all', 'scope', 'tags', 'quotas',
        )
        settings_fields = ('backend_url', 'username', 'password', 'token', 'certificate', 'scope', 'domain')
        protected_fields = ('customer', 'settings', 'project') + settings_fields
        related_paths = ('customer', 'settings')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
            'settings': {'lookup_field': 'uuid'},
        }

    def __new__(cls, *args, **kwargs):
        if cls.SERVICE_ACCOUNT_EXTRA_FIELDS is not NotImplemented:
            cls.Meta.fields += tuple(cls.SERVICE_ACCOUNT_EXTRA_FIELDS.keys())
            cls.Meta.protected_fields += tuple(cls.SERVICE_ACCOUNT_EXTRA_FIELDS.keys())
        return super(BaseServiceSerializer, cls).__new__(cls, *args, **kwargs)

    @staticmethod
    def eager_load(queryset, request=None):
        queryset = queryset.select_related('customer', 'settings')
        projects = models.Project.objects.all().only('uuid', 'name')
        return queryset.prefetch_related(django_models.Prefetch('projects', queryset=projects), 'quotas')

    def get_tags(self, service):
        return service.settings.get_tags()

    def get_filtered_field_names(self):
        return 'customer',

    def get_fields(self):
        fields = super(BaseServiceSerializer, self).get_fields()

        if self.Meta.model is not NotImplemented and 'settings' in fields:
            key = SupportedServices.get_model_key(self.Meta.model)
            fields['settings'].queryset = fields['settings'].queryset.filter(type=key)

        if self.SERVICE_ACCOUNT_FIELDS is not NotImplemented:
            # each service settings could be connected to scope
            self.SERVICE_ACCOUNT_FIELDS['scope'] = _('VM that contains service')
            for field in self.Meta.settings_fields:
                if field not in fields:
                    continue
                if field in self.SERVICE_ACCOUNT_FIELDS:
                    fields[field].help_text = self.SERVICE_ACCOUNT_FIELDS[field]
                else:
                    del fields[field]

        return fields

    def build_unknown_field(self, field_name, model_class):
        if self.SERVICE_ACCOUNT_EXTRA_FIELDS is not NotImplemented:
            if field_name in self.SERVICE_ACCOUNT_EXTRA_FIELDS:
                backend = SupportedServices.get_service_backend(self.Meta.model)
                kwargs = {
                    'write_only': True,
                    'required': False,
                    'allow_blank': True,
                    'help_text': self.SERVICE_ACCOUNT_EXTRA_FIELDS[field_name],
                }
                if hasattr(backend, 'DEFAULTS') and field_name in backend.DEFAULTS:
                    kwargs['help_text'] += ' (default: %s)' % json.dumps(backend.DEFAULTS[field_name])
                    kwargs['initial'] = backend.DEFAULTS[field_name]
                return serializers.CharField, kwargs

        return super(BaseServiceSerializer, self).build_unknown_field(field_name, model_class)

    def validate_empty_values(self, data):
        # required=False is ignored for settings FK, deal with it here
        if 'settings' not in data:
            data = data.copy()
            data['settings'] = None
        return super(BaseServiceSerializer, self).validate_empty_values(data)

    def validate(self, attrs):
        user = self.context['request'].user
        customer = attrs.get('customer') or self.instance.customer
        project = attrs.get('project')
        if project and project.customer != customer:
            raise serializers.ValidationError(
                _('Service cannot be connected to project that does not belong to services customer.'))

        settings = attrs.get('settings')
        if not user.is_staff:
            if not customer.has_user(user, models.CustomerRole.OWNER):
                raise exceptions.PermissionDenied()
            if not self.instance and settings and not settings.shared:
                if attrs.get('customer') != settings.customer:
                    raise serializers.ValidationError(_('Customer must match settings customer.'))

        if self.context['request'].method == 'POST':
            name = self.initial_data.get('name')
            if not name or not name.strip():
                raise serializers.ValidationError({'name': 'Name cannot be empty'})
            # Make shallow copy to protect from mutations
            settings_fields = self.Meta.settings_fields[:]
            create_settings = any([attrs.get(f) for f in settings_fields])
            if not settings and not create_settings:
                raise serializers.ValidationError(
                    _('Either service settings or credentials must be supplied.'))

            extra_fields = tuple()
            if self.SERVICE_ACCOUNT_EXTRA_FIELDS is not NotImplemented:
                extra_fields += tuple(self.SERVICE_ACCOUNT_EXTRA_FIELDS.keys())

            if create_settings:
                required = getattr(self.Meta, 'required_fields', tuple())
                for field in settings_fields:
                    if field in required and (field not in attrs or attrs[field] is None):
                        error = self.fields[field].error_messages['required']
                        raise serializers.ValidationError({field: six.text_type(error)})

                args = {f: attrs.get(f) for f in settings_fields if f in attrs}
                if extra_fields:
                    args['options'] = {f: attrs[f] for f in extra_fields if f in attrs}

                name = self.initial_data.get('name')
                if name is None:
                    raise serializers.ValidationError({'name': _('Name field is required.')})

                settings = models.ServiceSettings(
                    type=SupportedServices.get_model_key(self.Meta.model),
                    name=name,
                    customer=customer,
                    **args)

                try:
                    backend = settings.get_backend()
                    backend.ping(raise_exception=True)
                except ServiceBackendError as e:
                    raise serializers.ValidationError(_('Wrong settings: %s.') % e)
                except ServiceBackendNotImplemented:
                    pass

                self._validate_settings(settings)

                settings.save()
                executors.ServiceSettingsCreateExecutor.execute(settings)
                attrs['settings'] = settings

            for f in settings_fields + extra_fields:
                if f in attrs:
                    del attrs[f]

        return attrs

    def _validate_settings(self, settings):
        pass

    def get_resources_count(self, service):
        return self.get_resources_count_map[service.pk]

    @cached_property
    def get_resources_count_map(self):
        resource_models = SupportedServices.get_service_resources(self.Meta.model)
        resource_models = set(resource_models) - set(models.SubResource.get_all_models())
        counts = defaultdict(lambda: 0)
        user = self.context['request'].user
        for model in resource_models:
            service_path = model.Permissions.service_path
            if isinstance(self.instance, list):
                query = {service_path + '__in': self.instance}
            else:
                query = {service_path: self.instance}
            queryset = filter_queryset_for_user(model.objects.all(), user)
            rows = queryset.filter(**query).values(service_path) \
                .annotate(count=django_models.Count('id'))
            for row in rows:
                service_id = row[service_path]
                counts[service_id] += row['count']
        return counts

    def get_service_type(self, obj):
        return SupportedServices.get_name_for_model(obj)

    def get_state(self, obj):
        return obj.settings.get_state_display()

    def create(self, attrs):
        project = attrs.pop('project', None)
        service = super(BaseServiceSerializer, self).create(attrs)
        spl_model = service.projects.through
        if project and not spl_model.objects.filter(project=project, service=service).exists():
            spl_model.objects.create(project=project, service=service)
        return service


class BaseServiceProjectLinkSerializer(PermissionFieldFilteringMixin,
                                       core_serializers.AugmentedSerializerMixin,
                                       serializers.HyperlinkedModelSerializer):
    project = serializers.HyperlinkedRelatedField(
        queryset=models.Project.objects.all(),
        view_name='project-detail',
        lookup_field='uuid')

    service_name = serializers.ReadOnlyField(source='service.settings.name')
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)

    class Meta(object):
        model = NotImplemented
        fields = (
            'url',
            'project', 'project_name', 'project_uuid',
            'service', 'service_uuid', 'service_name', 'quotas',
        )
        related_paths = ('project', 'service')
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': NotImplemented},
        }

    def get_filtered_field_names(self):
        return 'project', 'service'

    def validate(self, attrs):
        if attrs['service'].customer != attrs['project'].customer:
            raise serializers.ValidationError(_("Service customer doesn't match project customer."))

        # XXX: Consider adding unique key (service, project) to the model instead
        if self.Meta.model.objects.filter(service=attrs['service'], project=attrs['project']).exists():
            raise serializers.ValidationError(_('This service project link already exists.'))

        return attrs


class ResourceSerializerMetaclass(serializers.SerializerMetaclass):
    """ Build a list of supported resource via serializers definition.
        See SupportedServices for details.
    """

    def __new__(cls, name, bases, args):
        serializer = super(ResourceSerializerMetaclass, cls).__new__(cls, name, bases, args)
        SupportedServices.register_resource_serializer(args['Meta'].model, serializer)
        return serializer


class BasicResourceSerializer(serializers.Serializer):
    uuid = serializers.ReadOnlyField()
    name = serializers.ReadOnlyField()
    resource_type = serializers.SerializerMethodField()

    def get_resource_type(self, resource):
        return SupportedServices.get_name_for_model(resource)


class ManagedResourceSerializer(BasicResourceSerializer):
    project_name = serializers.ReadOnlyField(source='service_project_link.project.name')
    project_uuid = serializers.ReadOnlyField(source='service_project_link.project.uuid')

    customer_uuid = serializers.ReadOnlyField(source='service_project_link.project.customer.uuid')
    customer_name = serializers.ReadOnlyField(source='service_project_link.project.customer.name')


class TagList(list):
    """
    This class serializes tags as JSON list as the last step of serialization process.
    """

    def __str__(self):
        return json.dumps(self)


class TagSerializer(serializers.Serializer):
    """
    This serializer updates tags field using django-taggit API.
    """

    def create(self, validated_data):
        if 'tags' in validated_data:
            tags = validated_data.pop('tags')
            instance = super(TagSerializer, self).create(validated_data)
            instance.tags.set(*tags)
        else:
            instance = super(TagSerializer, self).create(validated_data)
        return instance

    def update(self, instance, validated_data):
        if 'tags' in validated_data:
            tags = validated_data.pop('tags')
            instance = super(TagSerializer, self).update(instance, validated_data)
            instance.tags.set(*tags)
        else:
            instance = super(TagSerializer, self).update(instance, validated_data)
        return instance


class TagListSerializerField(serializers.Field):
    child = serializers.CharField()
    default_error_messages = {
        'not_a_list': _('Expected a list of items but got type "{input_type}".'),
        'invalid_json': _('Invalid json list. A tag list submitted in string form must be valid json.'),
        'not_a_str': _('All list items must be of string type.')
    }

    def to_internal_value(self, value):
        if isinstance(value, six.string_types):
            if not value:
                value = '[]'
            try:
                value = json.loads(value)
            except ValueError:
                self.fail('invalid_json')

        if not isinstance(value, list):
            self.fail('not_a_list', input_type=type(value).__name__)

        for s in value:
            if not isinstance(s, six.string_types):
                self.fail('not_a_str')

            self.child.run_validation(s)

        return value

    def get_attribute(self, instance):
        """
        Fetch tags from cache defined in TagMixin.
        """
        return instance.get_tags()

    def to_representation(self, value):
        if not isinstance(value, TagList):
            value = TagList(value)
        return value


class BaseResourceSerializer(six.with_metaclass(ResourceSerializerMetaclass,
                                                core_serializers.RestrictedSerializerMixin,
                                                MonitoringSerializerMixin,
                                                PermissionFieldFilteringMixin,
                                                core_serializers.AugmentedSerializerMixin,
                                                TagSerializer,
                                                serializers.HyperlinkedModelSerializer)):
    state = serializers.ReadOnlyField(source='get_state_display')

    project = serializers.HyperlinkedRelatedField(
        queryset=models.Project.objects.all(),
        view_name='project-detail',
        lookup_field='uuid',
        allow_null=True,
        required=False,
    )

    project_name = serializers.ReadOnlyField(source='service_project_link.project.name')
    project_uuid = serializers.ReadOnlyField(source='service_project_link.project.uuid')

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name=NotImplemented,
        queryset=NotImplemented,
        allow_null=True,
        required=False,
    )

    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name=NotImplemented,
        read_only=True,
        lookup_field='uuid')

    service_name = serializers.ReadOnlyField(source='service_project_link.service.settings.name')
    service_uuid = serializers.ReadOnlyField(source='service_project_link.service.uuid')

    service_settings = serializers.HyperlinkedRelatedField(
        queryset=models.ServiceSettings.objects.all(),
        view_name='servicesettings-detail',
        lookup_field='uuid',
        allow_null=True,
        required=False,
    )
    service_settings_uuid = serializers.ReadOnlyField(source='service_project_link.service.settings.uuid')
    service_settings_state = serializers.ReadOnlyField(
        source='service_project_link.service.settings.human_readable_state')
    service_settings_error_message = serializers.ReadOnlyField(
        source='service_project_link.service.settings.error_message')

    customer = serializers.HyperlinkedRelatedField(
        source='service_project_link.project.customer',
        view_name='customer-detail',
        read_only=True,
        lookup_field='uuid')

    customer_name = serializers.ReadOnlyField(source='service_project_link.project.customer.name')
    customer_abbreviation = serializers.ReadOnlyField(source='service_project_link.project.customer.abbreviation')
    customer_native_name = serializers.ReadOnlyField(source='service_project_link.project.customer.native_name')

    created = serializers.DateTimeField(read_only=True)
    resource_type = serializers.SerializerMethodField()

    tags = TagListSerializerField(required=False)
    access_url = serializers.SerializerMethodField()
    is_link_valid = serializers.BooleanField(
        source='service_project_link.is_valid',
        read_only=True,
        help_text=_('True if resource is originated from a service that satisfies an associated project requirements.'))

    class Meta(object):
        model = NotImplemented
        fields = MonitoringSerializerMixin.Meta.fields + (
            'url', 'uuid', 'name', 'description',
            'service', 'service_name', 'service_uuid',
            'service_settings', 'service_settings_uuid',
            'service_settings_state', 'service_settings_error_message',
            'project', 'project_name', 'project_uuid',
            'customer', 'customer_name', 'customer_native_name', 'customer_abbreviation',
            'tags', 'error_message',
            'resource_type', 'state', 'created', 'service_project_link', 'backend_id',
            'access_url', 'is_link_valid',
        )
        protected_fields = ('service', 'service_project_link', 'project', 'service_settings')
        read_only_fields = ('error_message', 'backend_id')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_filtered_field_names(self):
        return 'service_project_link',

    def get_resource_type(self, obj):
        return SupportedServices.get_name_for_model(obj)

    def get_resource_fields(self):
        return [f.name for f in self.Meta.model._meta.get_fields()]

    # an optional generic URL for accessing a resource
    def get_access_url(self, obj):
        return obj.get_access_url()

    @staticmethod
    def eager_load(queryset, request=None):
        return (
            queryset
            .select_related(
                'service_project_link',
                'service_project_link__service',
                'service_project_link__service__settings',
                'service_project_link__project',
                'service_project_link__project__customer',
            ).prefetch_related('service_project_link__service__settings__certifications',
                               'service_project_link__project__certifications')
        )

    def get_fields(self):
        fields = super(BaseResourceSerializer, self).get_fields()
        # skip validation on object update
        if not self.instance:
            service_type = SupportedServices.get_model_key(self.Meta.model)
            if not fields['service_settings'].read_only:
                queryset = fields['service_settings'].queryset.filter(type=service_type)
                fields['service_settings'].queryset = queryset
        return fields

    def validate(self, attrs):
        # skip validation on object update
        if self.instance:
            return attrs

        service_settings = attrs.pop('service_settings', None)
        project = attrs.pop('project', None)
        service_project_link = attrs.get('service_project_link')

        if not service_project_link:
            if service_settings and project:
                spl_model = self.Meta.model.service_project_link.field.remote_field.model
                try:
                    service_project_link = spl_model.objects.get(
                        service__settings=service_settings,
                        project=project,
                    )
                    attrs['service_project_link'] = service_project_link
                except django_exceptions.ObjectDoesNotExist:
                    raise serializers.ValidationError(
                        _('You are not allowed to provision resource in current project using this provider. '
                          'Please specify another value for project and service_settings fields.')
                    )
            else:
                raise serializers.ValidationError(
                    _('Either service_project_link or service_settings and project should be specified.')
                )

        if not service_project_link.is_valid:
            raise serializers.ValidationError({
                'service_project_link': service_project_link.validation_message
            })

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        data = validated_data.copy()
        fields = self.get_resource_fields()
        # Remove `virtual` properties which ain't actually belong to the model
        for prop in data.keys():
            if prop not in fields:
                del data[prop]

        resource = super(BaseResourceSerializer, self).create(data)
        resource.increase_backend_quotas_usage()
        return resource


class BaseResourceActionSerializer(BaseResourceSerializer):
    project = serializers.HyperlinkedRelatedField(
        view_name='project-detail',
        lookup_field='uuid',
        read_only=True,
    )
    service_settings = serializers.HyperlinkedRelatedField(
        view_name='servicesettings-detail',
        lookup_field='uuid',
        read_only=True,
    )

    class Meta(BaseResourceSerializer.Meta):
        pass


class SummaryResourceSerializer(core_serializers.BaseSummarySerializer):
    @classmethod
    def get_serializer(cls, model):
        return SupportedServices.get_resource_serializer(model)


class SummaryServiceSerializer(core_serializers.BaseSummarySerializer):
    @classmethod
    def get_serializer(cls, model):
        return SupportedServices.get_service_serializer(model)


class BaseResourceImportSerializer(PermissionFieldFilteringMixin,
                                   core_serializers.AugmentedSerializerMixin,
                                   serializers.HyperlinkedModelSerializer):
    backend_id = serializers.CharField(write_only=True)
    project = serializers.HyperlinkedRelatedField(
        queryset=models.Project.objects.all(),
        view_name='project-detail',
        lookup_field='uuid',
        write_only=True)

    state = serializers.ReadOnlyField(source='get_state_display')
    created = serializers.DateTimeField(read_only=True)
    import_history = serializers.BooleanField(
        default=True, write_only=True, help_text=_('Import historical resource usage.'))

    class Meta(object):
        model = NotImplemented
        fields = (
            'url', 'uuid', 'name', 'state', 'created',
            'backend_id', 'project', 'import_history'
        )
        read_only_fields = ('name',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_filtered_field_names(self):
        return 'project',

    def get_fields(self):
        fields = super(BaseResourceImportSerializer, self).get_fields()
        # Context doesn't have service during schema generation
        if 'service' in self.context:
            fields['project'].queryset = self.context['service'].projects.all()

        return fields

    def validate(self, attrs):
        if self.Meta.model.objects.filter(backend_id=attrs['backend_id']).exists():
            raise serializers.ValidationError(
                {'backend_id': _('This resource is already linked to Waldur.')})

        spl_class = SupportedServices.get_related_models(self.Meta.model)['service_project_link']
        spl = spl_class.objects.get(service=self.context['service'], project=attrs['project'])
        attrs['service_project_link'] = spl

        return attrs

    def create(self, validated_data):
        validated_data.pop('project')
        return super(BaseResourceImportSerializer, self).create(validated_data)


class VirtualMachineSerializer(BaseResourceSerializer):
    external_ips = serializers.ListField(
        child=serializers.IPAddressField(protocol='ipv4'),
        read_only=True,
    )
    internal_ips = serializers.ListField(
        child=serializers.IPAddressField(protocol='ipv4'),
        read_only=True,
    )

    ssh_public_key = serializers.HyperlinkedRelatedField(
        view_name='sshpublickey-detail',
        lookup_field='uuid',
        queryset=core_models.SshPublicKey.objects.all(),
        required=False,
        write_only=True)

    class Meta(BaseResourceSerializer.Meta):
        fields = BaseResourceSerializer.Meta.fields + (
            'start_time', 'cores', 'ram', 'disk', 'min_ram', 'min_disk',
            'ssh_public_key', 'user_data', 'external_ips', 'internal_ips',
            'latitude', 'longitude', 'key_name', 'key_fingerprint', 'image_name'
        )
        read_only_fields = BaseResourceSerializer.Meta.read_only_fields + (
            'start_time', 'cores', 'ram', 'disk', 'min_ram', 'min_disk',
            'external_ips', 'internal_ips',
            'latitude', 'longitude', 'key_name', 'key_fingerprint', 'image_name'
        )
        protected_fields = BaseResourceSerializer.Meta.protected_fields + (
            'user_data', 'ssh_public_key'
        )

    def get_fields(self):
        fields = super(VirtualMachineSerializer, self).get_fields()
        if 'request' in self.context:
            user = self.context['request'].user
            ssh_public_key = fields.get('ssh_public_key')
            if ssh_public_key:
                ssh_public_key.query_params = {'user_uuid': user.uuid.hex}
                if not user.is_staff:
                    subquery = Q(user=user) | Q(is_shared=True)
                    ssh_public_key.queryset = ssh_public_key.queryset.filter(subquery)
        return fields

    def create(self, validated_data):
        if 'image' in validated_data:
            validated_data['image_name'] = validated_data['image'].name
        return super(VirtualMachineSerializer, self).create(validated_data)


class PropertySerializerMetaclass(serializers.SerializerMetaclass):
    """ Build a list of supported properties via serializers definition.
        See SupportedServices for details.
    """

    def __new__(cls, name, bases, args):
        SupportedServices.register_property(args['Meta'].model)
        return super(PropertySerializerMetaclass, cls).__new__(cls, name, bases, args)


class BasePropertySerializer(six.with_metaclass(PropertySerializerMetaclass,
                                                core_serializers.AugmentedSerializerMixin,
                                                serializers.HyperlinkedModelSerializer)):
    class Meta(object):
        model = NotImplemented


class AggregateSerializer(serializers.Serializer):
    MODEL_NAME_CHOICES = (
        ('project', 'project'),
        ('customer', 'customer'),
    )
    MODEL_CLASSES = {
        'project': models.Project,
        'customer': models.Customer,
    }

    aggregate = serializers.ChoiceField(choices=MODEL_NAME_CHOICES, default='customer')
    uuid = serializers.CharField(allow_null=True, default=None)

    def get_aggregates(self, user):
        model = self.MODEL_CLASSES[self.data['aggregate']]
        queryset = filter_queryset_for_user(model.objects.all(), user)

        if 'uuid' in self.data and self.data['uuid']:
            queryset = queryset.filter(uuid=self.data['uuid'])
        return queryset

    def get_projects(self, user):
        queryset = self.get_aggregates(user)

        if self.data['aggregate'] == 'project':
            return queryset.all()
        else:
            queryset = models.Project.objects.filter(customer__in=list(queryset))
            return filter_queryset_for_user(queryset, user)

    def get_service_project_links(self, user):
        projects = self.get_projects(user)
        return [model.objects.filter(project__in=projects)
                for model in models.ServiceProjectLink.get_all_models()]


class PrivateCloudSerializer(BaseResourceSerializer):
    extra_configuration = serializers.JSONField(read_only=True)

    class Meta(BaseResourceSerializer.Meta):
        fields = BaseResourceSerializer.Meta.fields + ('extra_configuration',)
