import json
import logging
from functools import lru_cache

import pyvat
from django.conf import settings
from django.contrib import auth
from django.core import exceptions as django_exceptions
from django.core.validators import RegexValidator
from django.db import models as django_models
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions, serializers

from waldur_core.core import fields as core_fields
from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core.clean_html import clean_html
from waldur_core.core.fields import MappedChoiceField
from waldur_core.media.serializers import ProtectedMediaSerializerMixin
from waldur_core.quotas import serializers as quotas_serializers
from waldur_core.structure import models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.exceptions import (
    ServiceBackendError,
    ServiceBackendNotImplemented,
)
from waldur_core.structure.filters import filter_visible_users
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.registry import get_resource_type, get_service_type

User = auth.get_user_model()
logger = logging.getLogger(__name__)


def get_options_serializer_class(service_type):
    return next(
        cls
        for cls in ServiceOptionsSerializer.get_subclasses()
        if get_service_type(cls) == service_type
    )


@lru_cache
def get_resource_serializer_class(resource_type):
    return next(
        cls
        for cls in BaseResourceSerializer.get_subclasses()
        if get_resource_type(cls.Meta.model) == resource_type
        and get_service_type(cls) is not None
    )


class PermissionFieldFilteringMixin:
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
            'Implement get_filtered_field_names() ' 'to return list of filtered fields'
        )


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
    class Meta:
        model = User
        fields = (
            'url',
            'uuid',
            'username',
            'full_name',
            'native_name',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class BasicProjectSerializer(core_serializers.BasicInfoSerializer):
    class Meta(core_serializers.BasicInfoSerializer.Meta):
        model = models.Project


class PermissionProjectSerializer(BasicProjectSerializer):
    class Meta(BasicProjectSerializer.Meta):
        list_serializer_class = PermissionListSerializer


class ProjectTypeSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.ProjectType
        fields = ('uuid', 'url', 'name', 'description')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'project_type-detail'},
        }


class ProjectSerializer(
    core_serializers.RestrictedSerializerMixin,
    PermissionFieldFilteringMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)

    class Meta:
        model = models.Project
        fields = (
            'url',
            'uuid',
            'name',
            'customer',
            'customer_uuid',
            'customer_name',
            'customer_native_name',
            'customer_abbreviation',
            'description',
            'quotas',
            'created',
            'type',
            'type_name',
            'backend_id',
            'end_date',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
            'type': {'lookup_field': 'uuid', 'view_name': 'project_type-detail'},
        }
        related_paths = {
            'customer': ('uuid', 'name', 'native_name', 'abbreviation'),
            'type': ('name',),
        }
        protected_fields = ('backend_id',)

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
        return (
            queryset.select_related('customer')
            .only(*related_fields)
            .prefetch_related('quotas')
        )

    def get_filtered_field_names(self):
        return ('customer',)

    def get_optional_fields(self):
        return ('quotas',)

    def validate_description(self, value):
        return clean_html(value.strip())

    def validate_end_date(self, end_date):
        if end_date <= timezone.datetime.today().date():
            raise serializers.ValidationError(
                {'end_date': _('Cannot be earlier than the current date.')}
            )
        return end_date

    def validate(self, attrs):
        customer = (
            attrs.get('customer') if not self.instance else self.instance.customer
        )
        end_date = attrs.get('end_date')

        if end_date:
            structure_permissions.is_owner(self.context['request'], None, customer)

        return attrs


class CustomerSerializer(
    ProtectedMediaSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    projects = PermissionProjectSerializer(many=True, read_only=True)
    owners = BasicUserSerializer(source='get_owners', many=True, read_only=True)
    support_users = BasicUserSerializer(
        source='get_support_users', many=True, read_only=True
    )
    service_managers = BasicUserSerializer(
        source='get_service_managers', many=True, read_only=True
    )
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)

    COUNTRIES = core_fields.CountryField.COUNTRIES
    if settings.WALDUR_CORE.get('COUNTRIES'):
        COUNTRIES = [
            item for item in COUNTRIES if item[0] in settings.WALDUR_CORE['COUNTRIES']
        ]
    country = serializers.ChoiceField(
        required=False, choices=COUNTRIES, allow_blank=True
    )
    country_name = serializers.ReadOnlyField(source='get_country_display')
    display_name = serializers.ReadOnlyField(source='get_display_name')
    division_name = serializers.ReadOnlyField(source='division.name')
    division_uuid = serializers.ReadOnlyField(source='division.uuid')
    division_parent_name = serializers.ReadOnlyField(source='division.parent.name')
    division_parent_uuid = serializers.ReadOnlyField(source='division.parent.uuid')
    division_type_name = serializers.ReadOnlyField(source='division.type.name')
    division_type_uuid = serializers.ReadOnlyField(source='division.type.uuid')

    class Meta:
        model = models.Customer
        fields = (
            'url',
            'uuid',
            'created',
            'name',
            'native_name',
            'abbreviation',
            'division',
            'division_name',
            'division_uuid',
            'division_parent_name',
            'division_parent_uuid',
            'division_type_name',
            'division_type_uuid',
            'contact_details',
            'domain',
            'display_name',
            'agreement_number',
            'email',
            'phone_number',
            'access_subnets',
            'projects',
            'owners',
            'support_users',
            'service_managers',
            'backend_id',
            'registration_code',
            'homepage',
            'quotas',
            'image',
            'country',
            'country_name',
            'vat_code',
            'postal',
            'address',
            'bank_name',
            'bank_account',
            'latitude',
            'longitude',
            'default_tax_percent',
            'accounting_start_date',
        )
        staff_only_fields = (
            'access_subnets',
            'accounting_start_date',
            'default_tax_percent',
            'agreement_number',
            'domain',
            'division',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'division': {'lookup_field': 'uuid'},
        }

    def get_fields(self):
        fields = super(CustomerSerializer, self).get_fields()

        try:
            request = self.context['view'].request
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if not user.is_staff:
            for field_name in set(CustomerSerializer.Meta.staff_only_fields) & set(
                fields.keys()
            ):
                fields[field_name].read_only = True

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

        if vat_code:
            # Check VAT format
            if not pyvat.is_vat_number_format_valid(vat_code, country):
                raise serializers.ValidationError(
                    {'vat_code': _('VAT number has invalid format.')}
                )

            # Check VAT number in EU VAT Information Exchange System
            # if customer is new or either VAT number or country of the customer has changed
            if (
                not self.instance
                or self.instance.vat_code != vat_code
                or self.instance.country != country
            ):
                check_result = pyvat.check_vat_number(vat_code, country)
                if check_result.is_valid:
                    attrs['vat_name'] = check_result.business_name
                    attrs['vat_address'] = check_result.business_address
                    if not attrs.get('contact_details'):
                        attrs['contact_details'] = attrs['vat_address']
                elif check_result.is_valid is False:
                    raise serializers.ValidationError(
                        {'vat_code': _('VAT number is invalid.')}
                    )
                else:
                    logger.debug(
                        'Unable to check VAT number %s for country %s. Error message: %s',
                        vat_code,
                        country,
                        check_result.log_lines,
                    )
                    raise serializers.ValidationError(
                        {'vat_code': _('Unable to check VAT number.')}
                    )
        return attrs


class NestedCustomerSerializer(
    core_serializers.AugmentedSerializerMixin,
    core_serializers.HyperlinkedRelatedModelSerializer,
):
    class Meta:
        model = models.Customer
        fields = ('uuid', 'url')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class NestedProjectSerializer(
    core_serializers.AugmentedSerializerMixin,
    core_serializers.HyperlinkedRelatedModelSerializer,
):
    class Meta:
        model = models.Project
        fields = ('uuid', 'url')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


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
        source='perm.pk', view_name='customer_permission-detail', read_only=True,
    )
    projects = NestedProjectPermissionSerializer(many=True, read_only=True)

    class Meta:
        model = User
        fields = [
            'url',
            'uuid',
            'username',
            'full_name',
            'email',
            'role',
            'permission',
            'projects',
            'expiration_time',
        ]
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def to_representation(self, user):
        customer = self.context['customer']
        permission = models.CustomerPermission.objects.filter(
            customer=customer, user=user, is_active=True
        ).first()
        projects = models.ProjectPermission.objects.filter(
            project__customer=customer, user=user, is_active=True
        )
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
        fields = [
            'url',
            'uuid',
            'username',
            'full_name',
            'email',
            'role',
            'permission',
            'expiration_time',
        ]
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def to_representation(self, user):
        project = self.context['project']
        permission = models.ProjectPermission.objects.filter(
            project=project, user=user, is_active=True
        ).first()
        setattr(user, 'perm', permission)
        setattr(user, 'role', permission and permission.role)
        return super(ProjectUserSerializer, self).to_representation(user)


class BasePermissionSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        fields = (
            'user',
            'user_full_name',
            'user_native_name',
            'user_username',
            'user_uuid',
            'user_email',
        )
        related_paths = {
            'user': ('username', 'full_name', 'native_name', 'uuid', 'email'),
        }


class BasicCustomerPermissionSerializer(BasePermissionSerializer):
    class Meta(BasePermissionSerializer.Meta):
        model = models.CustomerPermission
        fields = (
            'url',
            'pk',
            'role',
            'customer_uuid',
            'customer_name',
            'customer_native_name',
            'customer_abbreviation',
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


class CustomerPermissionSerializer(
    PermissionFieldFilteringMixin, BasePermissionSerializer
):
    class Meta(BasePermissionSerializer.Meta):
        model = models.CustomerPermission
        fields = (
            'url',
            'pk',
            'role',
            'created',
            'expiration_time',
            'created_by',
            'customer',
            'customer_uuid',
            'customer_name',
            'customer_native_name',
            'customer_abbreviation',
        ) + BasePermissionSerializer.Meta.fields
        related_paths = dict(
            customer=('name', 'native_name', 'abbreviation', 'uuid'),
            **BasePermissionSerializer.Meta.related_paths
        )
        protected_fields = ('customer', 'role', 'user', 'created_by', 'created')
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
            },
        }

    def validate(self, data):
        if not self.instance:
            customer = data['customer']
            user = data['user']

            if customer.has_user(user):
                raise serializers.ValidationError(
                    _('The fields customer and user must make a unique set.')
                )

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
            raise serializers.ValidationError(
                _('Expiration time should be greater than current time.')
            )
        return value

    def get_filtered_field_names(self):
        return ('customer',)


class CustomerPermissionLogSerializer(CustomerPermissionSerializer):
    class Meta(CustomerPermissionSerializer.Meta):
        view_name = 'customer_permission_log-detail'


class CustomerPermissionReviewSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.CustomerPermissionReview
        view_name = 'customer_permission_review-detail'
        fields = (
            'url',
            'uuid',
            'reviewer_full_name',
            'reviewer_uuid',
            'customer_uuid',
            'customer_name',
            'is_pending',
            'created',
            'closed',
        )
        read_only_fields = (
            'is_pending',
            'closed',
        )
        related_paths = {
            'reviewer': ('full_name', 'uuid'),
            'customer': ('name', 'uuid'),
        }
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class ProjectPermissionSerializer(
    PermissionFieldFilteringMixin, BasePermissionSerializer
):
    customer_name = serializers.ReadOnlyField(source='project.customer.name')

    class Meta(BasePermissionSerializer.Meta):
        model = models.ProjectPermission
        fields = (
            'url',
            'pk',
            'role',
            'created',
            'expiration_time',
            'created_by',
            'project',
            'project_uuid',
            'project_name',
            'customer_name',
        ) + BasePermissionSerializer.Meta.fields
        related_paths = dict(
            project=('name', 'uuid'), **BasePermissionSerializer.Meta.related_paths
        )
        protected_fields = ('project', 'role', 'user', 'created_by', 'created')
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
            },
        }

    def validate(self, data):
        if not self.instance:
            project = data['project']
            user = data['user']

            if project.has_user(user):
                raise serializers.ValidationError(
                    _('The fields project and user must make a unique set.')
                )

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
            raise serializers.ValidationError(
                _('Expiration time should be greater than current time.')
            )
        return value

    def get_filtered_field_names(self):
        return ('project',)


class BasicProjectPermissionSerializer(BasePermissionSerializer):
    customer_name = serializers.ReadOnlyField(source='project.customer.name')

    class Meta(BasePermissionSerializer.Meta):
        model = models.ProjectPermission
        fields = (
            'url',
            'pk',
            'role',
            'project_uuid',
            'project_name',
            'customer_name',
        )
        related_paths = dict(
            project=('name', 'uuid'), **BasePermissionSerializer.Meta.related_paths
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


class UserSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    email = serializers.EmailField()
    agree_with_policy = serializers.BooleanField(
        write_only=True,
        required=False,
        help_text=_('User must agree with the policy to register.'),
    )
    competence = serializers.ChoiceField(
        choices=settings.WALDUR_CORE.get('USER_COMPETENCE_LIST', []),
        allow_blank=True,
        required=False,
    )
    token = serializers.ReadOnlyField(source='auth_token.key')
    customer_permissions = serializers.SerializerMethodField()
    project_permissions = serializers.SerializerMethodField()
    requested_email = serializers.SerializerMethodField()
    full_name = serializers.CharField(max_length=200, required=False)

    def get_customer_permissions(self, user):
        permissions = models.CustomerPermission.objects.filter(
            user=user, is_active=True
        ).select_related('customer')
        serializer = BasicCustomerPermissionSerializer(
            instance=permissions, many=True, context=self.context
        )
        return serializer.data

    def get_project_permissions(self, user):
        permissions = models.ProjectPermission.objects.filter(
            user=user, is_active=True
        ).select_related('project')
        serializer = BasicProjectPermissionSerializer(
            instance=permissions, many=True, context=self.context
        )
        return serializer.data

    def get_requested_email(self, user):
        try:
            requested_email = core_models.ChangeEmailRequest.objects.get(user=user)
            return requested_email.email
        except core_models.ChangeEmailRequest.DoesNotExist:
            pass

    class Meta:
        model = User
        fields = (
            'url',
            'uuid',
            'username',
            'full_name',
            'native_name',
            'job_title',
            'email',
            'phone_number',
            'organization',
            'civil_number',
            'description',
            'is_staff',
            'is_active',
            'is_support',
            'token',
            'token_lifetime',
            'registration_method',
            'date_joined',
            'agree_with_policy',
            'agreement_date',
            'preferred_language',
            'competence',
            'customer_permissions',
            'project_permissions',
            'requested_email',
            'affiliations',
            'first_name',
            'last_name',
        )
        read_only_fields = (
            'uuid',
            'civil_number',
            'registration_method',
            'date_joined',
            'agreement_date',
            'customer_permissions',
            'project_permissions',
            'affiliations',
            'first_name',
            'last_name',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }
        protected_fields = ('email',)

    def get_fields(self):
        fields = super(UserSerializer, self).get_fields()

        try:
            request = self.context['view'].request
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if user.is_anonymous:
            return fields

        if not user.is_staff:
            protected_fields = ('is_active', 'is_staff', 'is_support', 'description')
            if user.is_support:
                for field in protected_fields:
                    if field in fields:
                        fields[field].read_only = True
            else:
                for field in protected_fields:
                    if field in fields:
                        del fields[field]

        if not self._can_see_token(user):
            del fields['token']
            del fields['token_lifetime']

        if request.method in ('PUT', 'PATCH'):
            fields['username'].read_only = True
            protected_methods = settings.WALDUR_CORE[
                'PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS'
            ]
            if (
                user.registration_method
                and user.registration_method in protected_methods
            ):
                detail_fields = (
                    'full_name',
                    'native_name',
                    'job_title',
                    'email',
                    'phone_number',
                    'organization',
                )
                for field in detail_fields:
                    fields[field].read_only = True

        return fields

    def _can_see_token(self, user):
        # Nobody apart from the user herself can see her token.
        # User can see the token either via details view or /api/users/?current

        if isinstance(self.instance, list) and len(self.instance) == 1:
            return self.instance[0] == user
        else:
            return self.instance == user

    def validate(self, attrs):
        agree_with_policy = attrs.pop('agree_with_policy', False)
        if self.instance and not self.instance.agreement_date:
            if not agree_with_policy:
                raise serializers.ValidationError(
                    {'agree_with_policy': _('User must agree with the policy.')}
                )
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


class UserEmailChangeSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordSerializer(serializers.Serializer):
    password = serializers.CharField(
        min_length=7,
        validators=[
            RegexValidator(
                regex=r'\d', message=_('Ensure this field has at least one digit.'),
            ),
            RegexValidator(
                regex='[a-zA-Z]',
                message=_('Ensure this field has at least one latin letter.'),
            ),
        ],
    )


class SshKeySerializer(serializers.HyperlinkedModelSerializer):
    user_uuid = serializers.ReadOnlyField(source='user.uuid')

    class Meta:
        model = core_models.SshPublicKey
        fields = (
            'url',
            'uuid',
            'name',
            'public_key',
            'fingerprint',
            'user_uuid',
            'is_shared',
        )
        read_only_fields = ('fingerprint', 'is_shared')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def validate_name(self, value):
        return value.strip()

    def validate_public_key(self, value):
        value = value.strip()
        if len(value.splitlines()) > 1:
            raise serializers.ValidationError(
                _('Key is not valid: it should be single line.')
            )

        try:
            fingerprint = core_models.get_ssh_key_fingerprint(value)
        except (IndexError, TypeError):
            raise serializers.ValidationError(
                _('Key is not valid: cannot generate fingerprint from it.')
            )
        if core_models.SshPublicKey.objects.filter(fingerprint=fingerprint).exists():
            raise serializers.ValidationError(
                _('Key with same fingerprint already exists.')
            )
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


class MoveProjectSerializer(serializers.Serializer):
    customer = NestedCustomerSerializer(
        queryset=models.Customer.objects.all(), required=True, many=False
    )


class ServiceOptionsSerializer(serializers.Serializer):
    class Meta:
        secret_fields = ()

    @classmethod
    def get_subclasses(cls):
        for subclass in cls.__subclasses__():
            yield from subclass.get_subclasses()
            yield subclass


class ServiceSettingsSerializer(
    PermissionFieldFilteringMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    customer_native_name = serializers.ReadOnlyField(source='customer.native_name')
    state = MappedChoiceField(
        choices=[(v, k) for k, v in core_models.StateMixin.States.CHOICES],
        choice_mappings={v: k for k, v in core_models.StateMixin.States.CHOICES},
        read_only=True,
    )
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)
    scope = core_serializers.GenericRelatedField(
        related_models=models.BaseResource.get_all_models(),
        required=False,
        allow_null=True,
    )
    options = serializers.DictField()

    class Meta:
        model = models.ServiceSettings
        fields = (
            'url',
            'uuid',
            'name',
            'type',
            'state',
            'error_message',
            'shared',
            'customer',
            'customer_name',
            'customer_native_name',
            'terms_of_services',
            'quotas',
            'scope',
            'options',
        )
        protected_fields = ('type', 'customer')
        read_only_fields = ('shared', 'state', 'error_message')
        related_paths = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
        }

    def get_filtered_field_names(self):
        return ('customer',)

    @staticmethod
    def eager_load(queryset, request=None):
        return queryset.select_related('customer').prefetch_related('quotas')

    def get_fields(self):
        fields = super(ServiceSettingsSerializer, self).get_fields()
        method = self.context['view'].request.method
        if method == 'GET' and 'options' in fields:
            fields['options'] = serializers.SerializerMethodField('get_options')
        return fields

    def get_options(self, service):
        options = {
            'backend_url': service.backend_url,
            'username': service.username,
            'password': service.password,
            'domain': service.domain,
            'token': service.token,
            **service.options,
        }
        request = self.context['request']

        if request.user.is_staff:
            return options

        if service.customer and service.customer.has_user(
            request.user, models.CustomerRole.OWNER
        ):
            return options

        options_serializer_class = get_options_serializer_class(service.type)
        secret_fields = options_serializer_class.Meta.secret_fields
        return {k: v for (k, v) in options.items() if k not in secret_fields}

    def validate(self, attrs):
        if 'options' not in attrs:
            return attrs
        service_type = self.instance and self.instance.type or attrs['type']
        options_serializer_class = get_options_serializer_class(service_type)
        options_serializer = options_serializer_class(
            instance=self.instance, data=attrs['options'], context=self.context
        )
        options_serializer.is_valid(raise_exception=True)
        service_options = options_serializer.validated_data
        attrs.update(service_options)
        self._validate_settings(models.ServiceSettings(**attrs))
        return attrs

    def _validate_settings(self, service_settings):
        try:
            backend = service_settings.get_backend()
            backend.validate_settings()
        except ServiceBackendError as e:
            raise serializers.ValidationError(_('Wrong settings: %s.') % e)
        except ServiceBackendNotImplemented:
            pass


class BasicResourceSerializer(serializers.Serializer):
    uuid = serializers.ReadOnlyField()
    name = serializers.ReadOnlyField()
    resource_type = serializers.SerializerMethodField()

    def get_resource_type(self, resource):
        return get_resource_type(resource)


class ManagedResourceSerializer(BasicResourceSerializer):
    project_name = serializers.ReadOnlyField(source='project.name')
    project_uuid = serializers.ReadOnlyField(source='project.uuid')

    customer_uuid = serializers.ReadOnlyField(source='project.customer.uuid')
    customer_name = serializers.ReadOnlyField(source='project.customer.name')


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
        'invalid_json': _(
            'Invalid json list. A tag list submitted in string form must be valid json.'
        ),
        'not_a_str': _('All list items must be of string type.'),
    }

    def to_internal_value(self, value):
        if isinstance(value, str):
            if not value:
                value = '[]'
            try:
                value = json.loads(value)
            except ValueError:
                self.fail('invalid_json')

        if not isinstance(value, list):
            self.fail('not_a_list', input_type=type(value).__name__)

        for s in value:
            if not isinstance(s, str):
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


class BaseResourceSerializer(
    core_serializers.RestrictedSerializerMixin,
    PermissionFieldFilteringMixin,
    core_serializers.AugmentedSerializerMixin,
    TagSerializer,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField(source='get_state_display')

    project = serializers.HyperlinkedRelatedField(
        queryset=models.Project.objects.all(),
        view_name='project-detail',
        lookup_field='uuid',
    )

    project_name = serializers.ReadOnlyField(source='project.name')
    project_uuid = serializers.ReadOnlyField(source='project.uuid')

    service_name = serializers.ReadOnlyField(source='service_settings.name')

    service_settings = serializers.HyperlinkedRelatedField(
        queryset=models.ServiceSettings.objects.all(),
        view_name='servicesettings-detail',
        lookup_field='uuid',
    )
    service_settings_uuid = serializers.ReadOnlyField(source='service_settings.uuid')
    service_settings_state = serializers.ReadOnlyField(
        source='service_settings.human_readable_state'
    )
    service_settings_error_message = serializers.ReadOnlyField(
        source='service_settings.error_message'
    )

    customer = serializers.HyperlinkedRelatedField(
        source='project.customer',
        view_name='customer-detail',
        read_only=True,
        lookup_field='uuid',
    )

    customer_name = serializers.ReadOnlyField(source='project.customer.name')
    customer_abbreviation = serializers.ReadOnlyField(
        source='project.customer.abbreviation'
    )
    customer_native_name = serializers.ReadOnlyField(
        source='project.customer.native_name'
    )

    created = serializers.DateTimeField(read_only=True)
    resource_type = serializers.SerializerMethodField()

    tags = TagListSerializerField(required=False)
    access_url = serializers.SerializerMethodField()

    class Meta:
        model = NotImplemented
        fields = (
            'url',
            'uuid',
            'name',
            'description',
            'service_name',
            'service_settings',
            'service_settings_uuid',
            'service_settings_state',
            'service_settings_error_message',
            'project',
            'project_name',
            'project_uuid',
            'customer',
            'customer_name',
            'customer_native_name',
            'customer_abbreviation',
            'tags',
            'error_message',
            'error_traceback',
            'resource_type',
            'state',
            'created',
            'modified',
            'backend_id',
            'access_url',
        )
        protected_fields = (
            'project',
            'service_settings',
        )
        read_only_fields = ('error_message', 'error_traceback', 'backend_id')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_filtered_field_names(self):
        return ('project', 'service_settings')

    def get_resource_type(self, obj):
        return get_resource_type(obj)

    def get_resource_fields(self):
        return [f.name for f in self.Meta.model._meta.get_fields()]

    # an optional generic URL for accessing a resource
    def get_access_url(self, obj):
        return obj.get_access_url()

    def get_fields(self):
        fields = super(BaseResourceSerializer, self).get_fields()
        # skip validation on object update
        if not self.instance:
            service_type = get_service_type(self.Meta.model)
            if (
                'service_settings' in fields
                and not fields['service_settings'].read_only
            ):
                queryset = fields['service_settings'].queryset.filter(type=service_type)
                fields['service_settings'].queryset = queryset
        return fields

    @transaction.atomic
    def create(self, validated_data):
        data = validated_data.copy()
        fields = self.get_resource_fields()

        # Remove `virtual` properties which ain't actually belong to the model
        data = {key: value for key, value in data.items() if key in fields}

        resource = super(BaseResourceSerializer, self).create(data)
        resource.increase_backend_quotas_usage()
        return resource

    @classmethod
    def get_subclasses(cls):
        for subclass in cls.__subclasses__():
            yield from subclass.get_subclasses()
            if subclass.Meta.model != NotImplemented:
                yield subclass


class BaseResourceActionSerializer(BaseResourceSerializer):
    project = serializers.HyperlinkedRelatedField(
        view_name='project-detail', lookup_field='uuid', read_only=True,
    )
    service_settings = serializers.HyperlinkedRelatedField(
        view_name='servicesettings-detail', lookup_field='uuid', read_only=True,
    )

    class Meta(BaseResourceSerializer.Meta):
        pass


class SshPublicKeySerializerMixin(serializers.HyperlinkedModelSerializer):
    ssh_public_key = serializers.HyperlinkedRelatedField(
        view_name='sshpublickey-detail',
        lookup_field='uuid',
        queryset=core_models.SshPublicKey.objects.all(),
        required=False,
        write_only=True,
    )

    def get_fields(self):
        fields = super(SshPublicKeySerializerMixin, self).get_fields()
        if 'request' in self.context:
            user = self.context['request'].user
            ssh_public_key = fields.get('ssh_public_key')
            if ssh_public_key:
                if not user.is_staff:
                    visible_users = list(filter_visible_users(User.objects.all(), user))
                    subquery = Q(user__in=visible_users) | Q(is_shared=True)
                    ssh_public_key.queryset = ssh_public_key.queryset.filter(subquery)
        return fields


class VirtualMachineSerializer(SshPublicKeySerializerMixin, BaseResourceSerializer):
    external_ips = serializers.ListField(
        child=serializers.IPAddressField(protocol='ipv4'), read_only=True,
    )
    internal_ips = serializers.ListField(
        child=serializers.IPAddressField(protocol='ipv4'), read_only=True,
    )

    class Meta(BaseResourceSerializer.Meta):
        fields = BaseResourceSerializer.Meta.fields + (
            'start_time',
            'cores',
            'ram',
            'disk',
            'min_ram',
            'min_disk',
            'ssh_public_key',
            'user_data',
            'external_ips',
            'internal_ips',
            'latitude',
            'longitude',
            'key_name',
            'key_fingerprint',
            'image_name',
        )
        read_only_fields = BaseResourceSerializer.Meta.read_only_fields + (
            'start_time',
            'cores',
            'ram',
            'disk',
            'min_ram',
            'min_disk',
            'external_ips',
            'internal_ips',
            'latitude',
            'longitude',
            'key_name',
            'key_fingerprint',
            'image_name',
        )
        protected_fields = BaseResourceSerializer.Meta.protected_fields + (
            'user_data',
            'ssh_public_key',
        )

    def create(self, validated_data):
        if 'image' in validated_data:
            validated_data['image_name'] = validated_data['image'].name
        return super(VirtualMachineSerializer, self).create(validated_data)


class BasePropertySerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = NotImplemented


class DivisionSerializer(serializers.HyperlinkedModelSerializer):
    type = serializers.ReadOnlyField(source='type.name')
    parent_uuid = serializers.ReadOnlyField(source='parent.uuid')
    parent_name = serializers.ReadOnlyField(source='parent.type.name')

    class Meta:
        model = models.Division
        fields = ('uuid', 'url', 'name', 'type', 'parent_uuid', 'parent_name', 'parent')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'parent': {'lookup_field': 'uuid'},
        }


class DivisionTypesSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.DivisionType
        fields = (
            'uuid',
            'url',
            'name',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'division-type-detail'},
        }
