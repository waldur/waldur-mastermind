from collections import OrderedDict

from django.utils.encoding import force_text
from django.utils.http import urlencode
from rest_framework import exceptions
from rest_framework import serializers
from rest_framework.metadata import SimpleMetadata
from rest_framework.request import clone_request
from rest_framework.reverse import reverse
from rest_framework.utils.field_mapping import ClassLookupDict

from waldur_core.core.utils import sort_dict


class ActionSerializer(object):
    def __init__(self, func, name, request, view, resource):
        self.func = func
        self.name = name
        self.request = request
        self.resource = resource
        self.view = view

    def serialize(self):
        reason = self.get_reason()

        return {
            'title': self.get_title(),
            'method': self.get_method(),
            'destructive': self.is_destructive(),
            'url': self.get_url(),
            'reason': reason,
            'enabled': not reason
        }

    def is_destructive(self):
        if self.name == 'destroy':
            return True
        return getattr(self.func, 'destructive', False)

    def get_title(self):
        try:
            return getattr(self.func, 'title')
        except AttributeError:
            return self.name.replace('_', ' ').title()

    def get_reason(self):
        try:
            self.view.initial(self.request)
        except exceptions.APIException as e:
            return force_text(e)

    def get_method(self):
        if self.name == 'destroy':
            return 'DELETE'
        elif self.name == 'update':
            return 'PUT'
        return getattr(self.func, 'method', 'POST')

    def get_url(self):
        base_url = self.request.build_absolute_uri()
        method = self.get_method()
        if method in ('DELETE', 'PUT'):
            return base_url
        return base_url + self.name + '/'


def merge_dictionaries(a, b):
    new = a.copy()
    new.update(b)
    return new


class ActionsMetadata(SimpleMetadata):
    """
    Difference from SimpleMetadata class:
    1) Skip read-only fields, because options are used only for provisioning new resource.
    2) Don't expose choices for fields with queryset in order to reduce size of response.
    3) Attach actions metadata
    """

    label_lookup = ClassLookupDict(
        mapping=merge_dictionaries({
            serializers.JSONField: 'text'
        }, SimpleMetadata.label_lookup.mapping)
    )

    def determine_metadata(self, request, view):
        self.request = request
        metadata = OrderedDict()
        if view.lookup_field in view.kwargs:
            metadata['actions'] = self.get_actions(request, view)
        else:
            metadata['actions'] = self.determine_actions(request, view)
        return metadata

    def get_actions(self, request, view):
        """
        Return metadata for resource-specific actions,
        such as start, stop, unlink
        """
        metadata = OrderedDict()
        actions = self.get_resource_actions(view)

        resource = view.get_object()
        for action_name, action in actions.items():
            if action_name == 'update':
                view.request = clone_request(request, 'PUT')
            else:
                view.action = action_name

            data = ActionSerializer(action, action_name, request, view, resource)
            metadata[action_name] = data.serialize()
            if not metadata[action_name]['enabled']:
                continue
            fields = self.get_action_fields(view, action_name, resource)
            if not fields:
                metadata[action_name]['type'] = 'button'
            else:
                metadata[action_name]['type'] = 'form'
                metadata[action_name]['fields'] = fields

            view.action = None
            view.request = request

        return metadata

    @classmethod
    def get_resource_actions(cls, view):
        actions = {}
        disabled_actions = getattr(view.__class__, 'disabled_actions', [])

        for key in dir(view.__class__):
            callback = getattr(view.__class__, key)
            if getattr(callback, 'deprecated', False):
                continue
            if 'post' not in getattr(callback, 'bind_to_methods', []):
                continue
            if key in disabled_actions:
                continue
            actions[key] = callback

        if 'DELETE' in view.allowed_methods and 'destroy' not in disabled_actions:
            actions['destroy'] = view.destroy

        if 'PUT' in view.allowed_methods and 'update' not in disabled_actions:
            actions['update'] = view.update

        return sort_dict(actions)

    def get_action_fields(self, view, action_name, resource):
        """
        Get fields exposed by action's serializer
        """
        serializer = view.get_serializer(resource)
        fields = OrderedDict()
        if not isinstance(serializer, view.serializer_class) or action_name == 'update':
            fields = self.get_fields(serializer.fields)
        return fields

    def get_serializer_info(self, serializer):
        """
        Given an instance of a serializer, return a dictionary of metadata
        about its fields.
        """
        if hasattr(serializer, 'child'):
            # If this is a `ListSerializer` then we want to examine the
            # underlying child serializer instance instead.
            serializer = serializer.child
        return self.get_fields(serializer.fields)

    def get_fields(self, serializer_fields):
        """
        Get fields metadata skipping empty fields
        """
        fields = OrderedDict()
        for field_name, field in serializer_fields.items():
            # Skip tags field in action because it is needed only for resource creation
            # See also: WAL-1223
            if field_name == 'tags':
                continue
            info = self.get_field_info(field, field_name)
            if info:
                fields[field_name] = info
        return fields

    def get_field_info(self, field, field_name):
        """
        Given an instance of a serializer field, return a dictionary
        of metadata about it.
        """
        field_info = OrderedDict()
        field_info['type'] = self.label_lookup[field]
        field_info['required'] = getattr(field, 'required', False)

        attrs = [
            'label', 'help_text', 'default_value', 'placeholder', 'required',
            'min_length', 'max_length', 'min_value', 'max_value', 'many'
        ]

        if getattr(field, 'read_only', False):
            return None

        for attr in attrs:
            value = getattr(field, attr, None)
            if value is not None and value != '':
                field_info[attr] = force_text(value, strings_only=True)

        if 'label' not in field_info:
            field_info['label'] = field_name.replace('_', ' ').title()

        if hasattr(field, 'view_name'):
            list_view = field.view_name.replace('-detail', '-list')
            base_url = reverse(list_view, request=self.request)
            field_info['type'] = 'select'
            field_info['url'] = base_url
            if hasattr(field, 'query_params'):
                field_info['url'] += '?%s' % urlencode(field.query_params)
            field_info['value_field'] = getattr(field, 'value_field', 'url')
            field_info['display_name_field'] = getattr(field, 'display_name_field', 'display_name')

        if hasattr(field, 'choices') and not hasattr(field, 'queryset'):
            field_info['choices'] = [
                {
                    'value': choice_value,
                    'display_name': force_text(choice_name, strings_only=True)
                }
                for choice_value, choice_name in field.choices.items()
            ]

        return field_info
