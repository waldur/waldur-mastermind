from django.urls import NoReverseMatch
from django.utils.encoding import smart_text, force_text
from django_filters import OrderingFilter, ChoiceFilter, ModelMultipleChoiceFilter
from rest_framework import exceptions, schemas
from rest_framework.compat import coreapi
from rest_framework.fields import ChoiceField, ModelField, HiddenField
from rest_framework.permissions import AllowAny, SAFE_METHODS
from rest_framework.relations import HyperlinkedRelatedField, ManyRelatedField
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.serializers import ListSerializer, Serializer
from rest_framework.utils import formatting
from rest_framework.views import APIView
from rest_framework_swagger import renderers

from waldur_core.core import (permissions as core_permissions, views as core_views,
                              utils as core_utils, filters as core_filters, serializers as core_serializers)
from waldur_core.cost_tracking.filters import ResourceTypeFilter
from waldur_core.structure import SupportedServices, filters as structure_filters


# XXX: Drop after removing HEAD requests
class WaldurEndpointInspector(schemas.EndpointInspector):
    def get_allowed_methods(self, callback):
        """
        Return a list of the valid HTTP methods for this endpoint.
        """
        if hasattr(callback, 'actions'):
            return [method.upper() for method in callback.actions.keys() if method != 'head']

        return [
            method for method in
            callback.cls().allowed_methods if method not in ('OPTIONS', 'HEAD')
        ]


def get_entity_description(entity):
    """
    Returns description in format:
    * entity human readable name
     * docstring
    """

    try:
        entity_name = entity.__name__.strip('_')
    except AttributeError:
        # entity is a class instance
        entity_name = entity.__class__.__name__

    label = '* %s' % formatting.camelcase_to_spaces(entity_name)
    if entity.__doc__ is not None:
        entity_docstring = formatting.dedent(smart_text(entity.__doc__)).replace('\n', '\n\t')
        return '%s\n * %s' % (label, entity_docstring)

    return label


def get_validators_description(view):
    """
    Returns validators description in format:
    ### Validators:
    * validator1 name
     * validator1 docstring
    * validator2 name
     * validator2 docstring
    """
    action = getattr(view, 'action', None)
    if action is None:
        return ''

    description = ''
    validators = getattr(view, action + '_validators', [])
    for validator in validators:
        validator_description = get_entity_description(validator)
        description += '\n' + validator_description if description else validator_description

    return '### Validators:\n' + description if description else ''


def get_actions_permission_description(view, method):
    """
    Returns actions permissions description in format:
    * permission1 name
     * permission1 docstring
    * permission2 name
     * permission2 docstring
    """
    action = getattr(view, 'action', None)
    if action is None:
        return ''

    if hasattr(view, action + '_permissions'):
        permission_types = (action,)
    elif method in SAFE_METHODS:
        permission_types = ('safe_methods', '%s_extra' % action)
    else:
        permission_types = ('unsafe_methods', '%s_extra' % action)

    description = ''
    for permission_type in permission_types:
        action_perms = getattr(view, permission_type + '_permissions', [])
        for permission in action_perms:
            action_perm_description = get_entity_description(permission)
            description += '\n' + action_perm_description if description else action_perm_description

    return description


def get_permissions_description(view, method):
    """
    Returns permissions description in format:
    ### Permissions:
    * permission1 name
     * permission1 docstring
    * permission2 name
     * permission2 docstring
    """
    if not hasattr(view, 'permission_classes'):
        return ''

    description = ''
    for permission_class in view.permission_classes:
        if permission_class == core_permissions.ActionsPermission:
            actions_perm_description = get_actions_permission_description(view, method)
            if actions_perm_description:
                description += '\n' + actions_perm_description if description else actions_perm_description
            continue
        perm_description = get_entity_description(permission_class)
        description += '\n' + perm_description if description else perm_description

    return '### Permissions:\n' + description if description else ''


def get_validation_description(view, method):
    """
    Returns validation description in format:
    ### Validation:
    validate method docstring
    * field1 name
     * field1 validation docstring
    * field2 name
     * field2 validation docstring
    """
    if method not in ('PUT', 'PATCH', 'POST') or not hasattr(view, 'get_serializer'):
        return ''

    serializer = view.get_serializer()
    description = ''
    if hasattr(serializer, 'validate') and serializer.validate.__doc__ is not None:
        description += formatting.dedent(smart_text(serializer.validate.__doc__))

    for field in serializer.fields.values():
        if not hasattr(serializer, 'validate_' + field.field_name):
            continue

        field_validation = getattr(serializer, 'validate_' + field.field_name)

        if field_validation.__doc__ is not None:
            docstring = formatting.dedent(smart_text(field_validation.__doc__)).replace('\n', '\n\t')
            field_description = '* %s\n * %s' % (field.field_name, docstring)
            description += '\n' + field_description if description else field_description

    return '### Validation:\n' + description if description else ''


FIELDS = {
    'Boolean': 'boolean',
    'Char': 'string',
    'Timestamp': 'UNIX timestamp',
    'DateTime': 'DateTime',
    'URL': 'link',
    'Number': 'float',
    'UUID': 'string',
    'ContentType': 'string in form app_label.model_name',
    'Decimal': 'float',
    'Float': 'float',
    'File': 'file',
    'Email': 'email',
    'Integer': 'integer',
    'IPAddress': 'IP address',
    'HyperlinkedRelated': 'link',
}


def get_field_type(field):
    """
    Returns field type/possible values.
    """
    if isinstance(field, core_filters.MappedMultipleChoiceFilter):
        return ' | '.join(['"%s"' % f for f in sorted(field.mapped_to_model)])
    if isinstance(field, OrderingFilter) or isinstance(field, ChoiceFilter):
        return ' | '.join(['"%s"' % f[0] for f in field.extra['choices']])
    if isinstance(field, ChoiceField):
        return ' | '.join(['"%s"' % f for f in sorted(field.choices)])
    if isinstance(field, HyperlinkedRelatedField):
        if field.view_name.endswith('detail'):
            return 'link to %s' % reverse(field.view_name,
                                          kwargs={'%s' % field.lookup_field: "'%s'" % field.lookup_field})
        return reverse(field.view_name)
    if isinstance(field, structure_filters.ServiceTypeFilter):
        return ' | '.join(['"%s"' % f for f in SupportedServices.get_filter_mapping().keys()])
    if isinstance(field, ResourceTypeFilter):
        return ' | '.join(['"%s"' % f for f in SupportedServices.get_resource_models().keys()])
    if isinstance(field, core_serializers.GenericRelatedField):
        links = []
        for model in field.related_models:
            detail_view_name = core_utils.get_detail_view_name(model)
            for f in field.lookup_fields:
                try:
                    link = reverse(detail_view_name, kwargs={'%s' % f: "'%s'" % f})
                except NoReverseMatch:
                    pass
                else:
                    links.append(link)
                    break
        path = ', '.join(links)
        if path:
            return 'link to any: %s' % path
    if isinstance(field, core_filters.ContentTypeFilter):
        return "string in form 'app_label'.'model_name'"
    if isinstance(field, ModelMultipleChoiceFilter):
        return get_field_type(field.field)
    if isinstance(field, ListSerializer):
        return 'list of [%s]' % get_field_type(field.child)
    if isinstance(field, ManyRelatedField):
        return 'list of [%s]' % get_field_type(field.child_relation)
    if isinstance(field, ModelField):
        return get_field_type(field.model_field)

    name = field.__class__.__name__
    for w in ('Filter', 'Field', 'Serializer'):
        name = name.replace(w, '')
    return FIELDS.get(name, name)


def is_disabled_action(view):
    """
    Checks whether Link action is disabled.
    """
    if not isinstance(view, core_views.ActionsViewSet):
        return False

    action = getattr(view, 'action', None)
    return action in view.disabled_actions if action is not None else False


class WaldurSchemaGenerator(schemas.SchemaGenerator):
    endpoint_inspector_cls = WaldurEndpointInspector

    def create_view(self, callback, method, request=None):
        """
        Given a callback, return an actual view instance.
        """
        view = super(WaldurSchemaGenerator, self).create_view(callback, method, request)
        if is_disabled_action(view):
            view.exclude_from_schema = True

        return view

    def get_description(self, path, method, view):
        """
        Determine a link description.

        This will be based on the method docstring if one exists,
        or else the class docstring.
        """
        description = super(WaldurSchemaGenerator, self).get_description(path, method, view)

        permissions_description = get_permissions_description(view, method)
        if permissions_description:
            description += '\n\n' + permissions_description if description else permissions_description

        if isinstance(view, core_views.ActionsViewSet):
            validators_description = get_validators_description(view)
            if validators_description:
                description += '\n\n' + validators_description if description else validators_description

        validation_description = get_validation_description(view, method)
        if validation_description:
            description += '\n\n' + validation_description if description else validation_description

        return description

    def get_filter_fields(self, path, method, view):
        if not schemas.is_list_view(path, method, view):
            return []

        if not getattr(view, 'filter_backends', None):
            return []

        fields = []
        for filter_backend in view.filter_backends:
            backend = filter_backend()
            if not hasattr(backend, 'get_filter_class'):
                fields += filter_backend().get_schema_fields(view)
                continue

            filter_class = backend.get_filter_class(view, view.get_queryset())
            if not filter_class:
                continue

            for filter_name, filter_instance in filter_class().filters.items():
                filter_type = get_field_type(filter_instance)
                field = coreapi.Field(
                    name=filter_name,
                    required=False,
                    location=filter_type,
                )
                # Prevent double rendering
                if field not in fields:
                    fields.append(field)

        return fields

    def get_serializer_fields(self, path, method, view):
        """
        Return a list of `coreapi.Field` instances corresponding to any
        request body input, as determined by the serializer class.
        """
        if method not in ('PUT', 'PATCH', 'POST'):
            return []

        if not hasattr(view, 'get_serializer'):
            return []

        serializer = view.get_serializer()
        if not isinstance(serializer, Serializer):
            return []

        fields = []
        for field in serializer.fields.values():
            if field.read_only or isinstance(field, HiddenField):
                continue

            required = field.required and method != 'PATCH'
            description = force_text(field.help_text) if field.help_text else ''
            field_type = get_field_type(field)
            description += '; ' + field_type if description else field_type
            field = coreapi.Field(
                name=field.field_name,
                location='form',
                required=required,
                description=description,
                schema=schemas.field_to_schema(field),
            )
            fields.append(field)

        return fields


class WaldurSchemaView(APIView):
    permission_classes = [AllowAny]
    renderer_classes = [
        renderers.OpenAPIRenderer,
        renderers.SwaggerUIRenderer
    ]

    def get(self, request):
        generator = WaldurSchemaGenerator(
            title='Waldur MasterMind',
        )
        schema = generator.get_schema(request=request)

        if not schema:
            raise exceptions.ValidationError(
                'The schema generator did not return a schema Document'
            )

        return Response(schema)
