import logging
from functools import lru_cache

import pyvat
from django.conf import settings
from django.contrib import auth
from django.core import exceptions as django_exceptions
from django.db import models as django_models
from django.db import transaction
from django.db.models import Q
from django.template.loader import get_template
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, serializers

from waldur_core.core import fields as core_fields
from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core.clean_html import clean_html
from waldur_core.core.fields import MappedChoiceField
from waldur_core.media.serializers import ProtectedMediaSerializerMixin
from waldur_core.structure import models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import utils
from waldur_core.structure.exceptions import (
    ServiceBackendError,
    ServiceBackendNotImplemented,
)
from waldur_core.structure.filters import filter_visible_users
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import CUSTOMER_DETAILS_FIELDS
from waldur_core.structure.registry import get_resource_type, get_service_type

User = auth.get_user_model()
logger = logging.getLogger(__name__)

SOCIAL_SIGNUP_DETAILS = {
    'eduteams': {
        'label_key': 'EDUTEAMS_LABEL',
        'management_url_key': 'EDUTEAMS_MANAGEMENT_URL',
        'details_fields_key': 'EDUTEAMS_USER_PROTECTED_FIELDS',
    },
    'keycloak': {
        'label_key': 'KEYCLOAK_LABEL',
        'management_url_key': 'KEYCLOAK_MANAGEMENT_URL',
        'details_fields_key': 'KEYCLOAK_USER_PROTECTED_FIELDS',
    },
    'tara': {
        'label_key': 'TARA_LABEL',
        'management_url_key': 'TARA_MANAGEMENT_URL',
        'details_fields_key': 'TARA_USER_PROTECTED_FIELDS',
    },
}


def get_options_serializer_class(service_type):
    return next(
        cls
        for cls in ServiceOptionsSerializer.get_subclasses()
        if get_service_type(cls) == service_type
    )


@lru_cache
def get_resource_serializer_class(resource_type):
    try:
        return next(
            cls
            for cls in BaseResourceSerializer.get_subclasses()
            if get_resource_type(cls.Meta.model) == resource_type
            and get_service_type(cls) is not None
        )
    except StopIteration:
        return None


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
        fields = super().get_fields()

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


class FieldFilteringMixin:
    """
    Mixin allowing to filter fields by user.

    In order to constrain the list of fields implement
    `get_filtered_field()` method returning list of tuples
    (field name, func for check access).
    """

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context['request']
            user = request.user
        except (KeyError, AttributeError):
            return fields

        for field_name, check_access in self.get_filtered_field():
            if field_name not in fields:
                continue

            if not check_access(user):
                del fields[field_name]

        return fields

    def get_filtered_field(self):
        raise NotImplementedError(
            'Implement get_filtered_field() ' 'to return list of tuples '
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

        return super().to_representation(data)


class BasicUserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = User
        fields = (
            'url',
            'uuid',
            'username',
            'full_name',
            'native_name',
            'email',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class BasicProjectSerializer(core_serializers.BasicInfoSerializer):
    class Meta(core_serializers.BasicInfoSerializer.Meta):
        model = models.Project


class PermissionProjectSerializer(BasicProjectSerializer):
    resource_count = serializers.SerializerMethodField()

    class Meta(BasicProjectSerializer.Meta):
        list_serializer_class = PermissionListSerializer
        fields = BasicProjectSerializer.Meta.fields + ('image', 'resource_count')

    def get_resource_count(self, project):
        from waldur_mastermind.marketplace import models as marketplace_models

        return marketplace_models.Resource.objects.filter(
            state__in=(
                marketplace_models.Resource.States.OK,
                marketplace_models.Resource.States.UPDATING,
            ),
            project=project,
        ).count()


class ProjectTypeSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.ProjectType
        fields = ('uuid', 'url', 'name', 'description')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'project_type-detail'},
        }


class ProjectDetailsSerializerMixin(serializers.Serializer):
    def validate_description(self, value):
        return clean_html(value.strip())

    def validate_end_date(self, end_date):
        if end_date and end_date < timezone.datetime.today().date():
            raise serializers.ValidationError(
                {'end_date': _('Cannot be earlier than the current date.')}
            )
        return end_date


class ProjectSerializer(
    ProjectDetailsSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    PermissionFieldFilteringMixin,
    ProtectedMediaSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    resources_count = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    oecd_fos_2007_label = serializers.ReadOnlyField(
        source='get_oecd_fos_2007_code_display'
    )

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
            'created',
            'type',
            'type_name',
            'type_uuid',
            'backend_id',
            'end_date',
            'end_date_requested_by',
            'oecd_fos_2007_code',
            'oecd_fos_2007_label',
            'is_industry',
            'image',
            'resources_count',
            'role',
        )
        protected_fields = ('end_date_requested_by',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
            'type': {'lookup_field': 'uuid', 'view_name': 'project_type-detail'},
            'end_date_requested_by': {
                'lookup_field': 'uuid',
                'view_name': 'user-detail',
            },
        }
        related_paths = {
            'customer': ('uuid', 'name', 'native_name', 'abbreviation'),
            'type': ('name', 'uuid'),
        }

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
        return queryset.select_related('customer').only(*related_fields)

    def get_filtered_field_names(self):
        return ('customer',)

    def validate(self, attrs):
        customer = (
            attrs.get('customer') if not self.instance else self.instance.customer
        )
        end_date = attrs.get('end_date')
        image = attrs.get('image')

        if end_date:
            structure_permissions.is_owner(self.context['request'], None, customer)
            attrs['end_date_requested_by'] = self.context['request'].user

        if image and self.instance:
            structure_permissions.is_manager(
                self.context['request'], None, self.context['view'].get_object()
            )
        elif image and not self.instance:
            structure_permissions.is_owner(self.context['request'], None, customer)

        return attrs

    def get_resources_count(self, project):
        from waldur_mastermind.marketplace import models as marketplace_models

        return marketplace_models.Resource.objects.filter(
            state__in=(
                marketplace_models.Resource.States.OK,
                marketplace_models.Resource.States.UPDATING,
            ),
            project=project,
        ).count()

    def get_role(self, project):
        user = self.context['request'].user
        if user.is_staff:
            return 'staff'
        if user.is_support:
            return 'support'
        permission = models.ProjectPermission.objects.filter(
            user=user, project=project, is_active=True
        ).first()
        if permission:
            return permission.role


class CountrySerializerMixin(serializers.Serializer):
    COUNTRIES = core_fields.COUNTRIES
    if settings.WALDUR_CORE.get('COUNTRIES'):
        COUNTRIES = [
            item for item in COUNTRIES if item[0] in settings.WALDUR_CORE['COUNTRIES']
        ]
    country = serializers.ChoiceField(
        required=False, choices=COUNTRIES, allow_blank=True
    )
    country_name = serializers.ReadOnlyField(source='get_country_display')


class CustomerSerializer(
    ProtectedMediaSerializerMixin,
    CountrySerializerMixin,
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

    display_name = serializers.ReadOnlyField(source='get_display_name')
    division_name = serializers.ReadOnlyField(source='division.name')
    division_uuid = serializers.ReadOnlyField(source='division.uuid')
    division_parent_name = serializers.ReadOnlyField(source='division.parent.name')
    division_parent_uuid = serializers.ReadOnlyField(source='division.parent.uuid')
    division_type_name = serializers.ReadOnlyField(source='division.type.name')
    division_type_uuid = serializers.ReadOnlyField(source='division.type.uuid')
    role = serializers.SerializerMethodField()
    projects_count = serializers.SerializerMethodField()
    users_count = serializers.SerializerMethodField()

    class Meta:
        model = models.Customer
        fields = (
            'url',
            'uuid',
            'created',
            'division',
            'division_name',
            'division_uuid',
            'division_parent_name',
            'division_parent_uuid',
            'division_type_name',
            'division_type_uuid',
            'display_name',
            'projects',
            'owners',
            'support_users',
            'service_managers',
            'backend_id',
            'image',
            'blocked',
            'archived',
            'default_tax_percent',
            'accounting_start_date',
            'inet',
            'role',
            'projects_count',
            'users_count',
            'sponsor_number',
        ) + CUSTOMER_DETAILS_FIELDS
        staff_only_fields = (
            'access_subnets',
            'accounting_start_date',
            'default_tax_percent',
            'agreement_number',
            'domain',
            'division',
            'blocked',
            'archived',
            'sponsor_number',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'division': {'lookup_field': 'uuid'},
        }

    def get_fields(self):
        fields = super().get_fields()

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
        return super().create(validated_data)

    @staticmethod
    def eager_load(queryset, request=None):
        return queryset.prefetch_related('projects')

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

    def get_role(self, customer):
        user = self.context['request'].user
        if user.is_staff:
            return 'staff'
        if user.is_support:
            return 'support'
        permission = models.CustomerPermission.objects.filter(
            user=user, customer=customer, is_active=True
        ).first()
        if permission:
            return permission.role

    def get_projects_count(self, customer):
        return models.Project.available_objects.filter(customer=customer).count()

    def get_users_count(self, customer):
        return customer.get_users().count()


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


class BasicCustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Customer
        fields = (
            'uuid',
            'name',
        )


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
    is_service_manager = serializers.ReadOnlyField()
    expiration_time = serializers.ReadOnlyField(source='perm.expiration_time')
    permission = serializers.HyperlinkedRelatedField(
        source='perm.pk',
        view_name='customer_permission-detail',
        read_only=True,
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
            'is_service_manager',
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
        is_service_manager = customer.has_user(
            user, role=models.CustomerRole.SERVICE_MANAGER
        )
        setattr(user, 'perm', permission)
        setattr(user, 'role', permission and permission.role)
        setattr(user, 'projects', projects)
        setattr(user, 'is_service_manager', is_service_manager)
        return super().to_representation(user)


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
        return super().to_representation(user)


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
    customer_division_name = serializers.ReadOnlyField(source='customer.division.name')
    customer_division_uuid = serializers.ReadOnlyField(source='customer.division.uuid')

    class Meta(BasePermissionSerializer.Meta):
        model = models.CustomerPermission
        fields = (
            'url',
            'pk',
            'role',
            'customer',
            'customer_uuid',
            'customer_name',
            'customer_native_name',
            'customer_abbreviation',
            'customer_division_name',
            'customer_division_uuid',
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
    customer_division_name = serializers.ReadOnlyField(source='customer.division.name')
    customer_division_uuid = serializers.ReadOnlyField(source='customer.division.uuid')

    class Meta(BasePermissionSerializer.Meta):
        model = models.CustomerPermission
        fields = (
            'url',
            'pk',
            'role',
            'created',
            'expiration_time',
            'created_by',
            'created_by_full_name',
            'created_by_username',
            'customer',
            'customer_uuid',
            'customer_name',
            'customer_native_name',
            'customer_abbreviation',
            'customer_division_name',
            'customer_division_uuid',
            'customer_created',
            'customer_email',
        ) + BasePermissionSerializer.Meta.fields
        related_paths = dict(
            customer=(
                'name',
                'native_name',
                'abbreviation',
                'uuid',
                'created',
                'email',
            ),
            created_by=('full_name', 'username'),
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
    customer_uuid = serializers.ReadOnlyField(source='project.customer.uuid')
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
            'created_by_full_name',
            'created_by_username',
            'project',
            'project_uuid',
            'project_name',
            'project_created',
            'project_end_date',
            'customer_uuid',
            'customer_name',
        ) + BasePermissionSerializer.Meta.fields
        related_paths = dict(
            project=('name', 'uuid', 'created', 'end_date'),
            created_by=('full_name', 'username'),
            **BasePermissionSerializer.Meta.related_paths
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

            if project.is_removed:
                raise serializers.ValidationError(_('Project is removed.'))

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
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    ProtectedMediaSerializerMixin,
    serializers.HyperlinkedModelSerializer,
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
    identity_provider_name = serializers.SerializerMethodField()
    identity_provider_label = serializers.SerializerMethodField()
    identity_provider_management_url = serializers.SerializerMethodField()
    identity_provider_fields = serializers.SerializerMethodField()

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

    def get_identity_provider_name(self, user):
        registration_method = user.registration_method
        if user.registration_method in SOCIAL_SIGNUP_DETAILS.keys():
            key = SOCIAL_SIGNUP_DETAILS[registration_method]['label_key']
            return settings.WALDUR_AUTH_SOCIAL[key]

        if registration_method == settings.WALDUR_AUTH_SAML2['NAME']:
            return settings.WALDUR_AUTH_SAML2['IDENTITY_PROVIDER_LABEL']

        if registration_method == 'valimo':
            return settings.WALDUR_AUTH_VALIMO['LABEL']

        if registration_method == 'default':
            return settings.WALDUR_CORE['LOCAL_IDP_NAME']

        return ''

    def get_identity_provider_label(self, user):
        registration_method = user.registration_method
        if user.registration_method in SOCIAL_SIGNUP_DETAILS.keys():
            key = SOCIAL_SIGNUP_DETAILS[registration_method]['label_key']
            return settings.WALDUR_AUTH_SOCIAL[key]

        if registration_method == settings.WALDUR_AUTH_SAML2['NAME']:
            return settings.WALDUR_AUTH_SAML2['IDENTITY_PROVIDER_LABEL']

        if registration_method == 'valimo':
            return settings.WALDUR_AUTH_VALIMO['LABEL']

        if registration_method == 'default':
            return settings.WALDUR_CORE['LOCAL_IDP_LABEL']

        return ''

    def get_identity_provider_management_url(self, user):
        registration_method = user.registration_method
        if user.registration_method in SOCIAL_SIGNUP_DETAILS.keys():
            key = SOCIAL_SIGNUP_DETAILS[registration_method]['management_url_key']
            return settings.WALDUR_AUTH_SOCIAL[key]

        if registration_method == settings.WALDUR_AUTH_SAML2['NAME']:
            return settings.WALDUR_AUTH_SAML2['MANAGEMENT_URL']

        if registration_method == 'valimo':
            return settings.WALDUR_AUTH_VALIMO['USER_MANAGEMENT_URL']

        if registration_method == 'default':
            return settings.WALDUR_CORE['LOCAL_IDP_MANAGEMENT_URL']

        return ''

    def get_identity_provider_fields(self, user):
        registration_method = user.registration_method
        idp_protected_fields_map = utils.get_idp_protected_fields_map()
        if registration_method in idp_protected_fields_map:
            return idp_protected_fields_map[registration_method]

        return []

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
            'identity_provider_name',
            'identity_provider_label',
            'identity_provider_management_url',
            'identity_provider_fields',
            'image',
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
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }
        protected_fields = ('email',)

    def get_fields(self):
        fields = super().get_fields()

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
            if 'token' in fields:
                del fields['token']
            if 'token_lifetime' in fields:
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
        # User can see the token either via details view or /api/users/me

        if isinstance(self.instance, list) and len(self.instance) == 1:
            return self.instance[0] == user
        else:
            return self.instance == user

    def validate(self, attrs):
        agree_with_policy = attrs.pop('agree_with_policy', False)
        if self.instance and not self.instance.agreement_date:
            if not agree_with_policy:
                if (
                    self.instance.is_active
                    and 'is_active' in attrs.keys()
                    and not attrs['is_active']
                    and len(attrs) == 1
                ):
                    # Deactivation of user.
                    pass
                else:
                    raise serializers.ValidationError(
                        {'agree_with_policy': _('User must agree with the policy.')}
                    )
            else:
                attrs['agreement_date'] = timezone.now()

        if self.instance:
            idp_fields = self.get_identity_provider_fields(self.instance)
            allowed_fields = set(attrs.keys()) - set(idp_fields)
            attrs = {k: v for k, v in attrs.items() if k in allowed_fields}

        if 'full_name' in attrs and 'first_name' in attrs:
            raise serializers.ValidationError(
                {'first_name': _('Cannot specify first name with full name')}
            )
        elif 'full_name' in attrs and 'last_name' in attrs:
            raise serializers.ValidationError(
                {'last_name': _('Cannot specify last name with full name')}
            )

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
            'type',
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
            core_models.get_ssh_key_fingerprint(value)
        except (IndexError, TypeError):
            raise serializers.ValidationError(
                _('Key is not valid: cannot generate fingerprint from it.')
            )
        return value


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
        return queryset.select_related('customer')

    def get_fields(self):
        fields = super().get_fields()
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


class BaseResourceSerializer(
    core_serializers.RestrictedSerializerMixin,
    PermissionFieldFilteringMixin,
    core_serializers.AugmentedSerializerMixin,
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
        fields = super().get_fields()
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

        resource = super().create(data)
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


class SshPublicKeySerializerMixin(serializers.HyperlinkedModelSerializer):
    ssh_public_key = serializers.HyperlinkedRelatedField(
        view_name='sshpublickey-detail',
        lookup_field='uuid',
        queryset=core_models.SshPublicKey.objects.all(),
        required=False,
        write_only=True,
    )

    def get_fields(self):
        fields = super().get_fields()
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
        child=serializers.IPAddressField(protocol='ipv4'),
        read_only=True,
    )
    internal_ips = serializers.ListField(
        child=serializers.IPAddressField(protocol='ipv4'),
        read_only=True,
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
        return super().create(validated_data)


class BasePropertySerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
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


class UserAgreementSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.UserAgreement
        fields = ('content', 'agreement_type', 'created')


class NotificationTemplateSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = core_models.NotificationTemplate
        fields = (
            'uuid',
            'url',
            'path',
            'name',
        )
        extra_kwargs = {
            'url': {
                'view_name': 'notification-messages-templates-detail',
                'lookup_field': 'uuid',
            },
        }


class NotificationSerializer(serializers.HyperlinkedModelSerializer):
    templates = NotificationTemplateSerializer(many=True, read_only=True)

    class Meta:
        model = core_models.Notification
        fields = (
            'uuid',
            'url',
            'key',
            'description',
            'enabled',
            'created',
            'templates',
        )
        read_only_fields = ('created', 'enabled')
        extra_kwargs = {
            'url': {
                'view_name': 'notification-messages-detail',
                'lookup_field': 'uuid',
            },
        }


class NotificationTemplateDetailSerializers(serializers.ModelSerializer):
    content = serializers.SerializerMethodField()

    class Meta:
        model = core_models.NotificationTemplate
        fields = (
            'uuid',
            'url',
            'path',
            'name',
            'content',
        )
        extra_kwargs = {
            'url': {
                'view_name': 'notification-messages-templates-detail',
                'lookup_field': 'uuid',
            },
        }

    def get_content(self, obj):
        return get_template(obj.path).template.source


class NotificationTemplateUpdateSerializers(serializers.Serializer):
    content = serializers.CharField()
