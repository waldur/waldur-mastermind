from __future__ import unicode_literals

from functools import reduce
import importlib
import inspect
import logging

from django.apps import apps
from django.conf import settings
from django.contrib.admindocs.views import simplify_regex
from django.urls import RegexURLResolver, RegexURLPattern
from django_filters import ModelMultipleChoiceFilter
from rest_framework.fields import ChoiceField, ReadOnlyField, ModelField
from rest_framework.relations import HyperlinkedRelatedField, ManyRelatedField
from rest_framework.serializers import ListSerializer, ModelSerializer
from rest_framework.settings import api_settings
from rest_framework.views import APIView
import six

from waldur_core.core.filters import ContentTypeFilter, MappedMultipleChoiceFilter
from waldur_core.core.serializers import GenericRelatedField
from waldur_core.core.utils import get_fake_context

logger = logging.getLogger(__name__)


def getdoc(obj, warning=True):
    doc = inspect.getdoc(obj) or ''
    if not doc and warning:
        if inspect.isclass(obj):
            name = '{}.{}'.format(obj.__module__, obj.__name__)
        elif inspect.ismethod(obj):
            cls = obj.im_class
            name = '{}.{}.{}'.format(cls.__module__, cls.__name__, obj.im_func.func_name)
        else:
            name = six.text_type(obj)
        logger.warning("Docstring is missing for %s", name)
    return doc


class ApiDocs(object):
    """ Generate RST docs for DRF endpoints from docstrings:
        - AppConfig class docstring may contain general info about an app,
          `verbose_name` in the config delivers human friendly title;
        - View class docstring describes intention of top-level endpoint;
        - View method docstring can explain usage of particular method or actions;
    """
    tree = {}
    exclude = ['rest_framework']

    def __init__(self, apps=None):
        root_urlconf = importlib.import_module(settings.ROOT_URLCONF)
        endpoints = self.get_all_view_names(root_urlconf.urlpatterns)
        self.build_docs_tree(endpoints)
        self.apps = apps

    def build_docs_tree(self, endpoints):
        for ep in endpoints:
            if ep.app in self.exclude:
                continue
            root = '/'.join(ep.path.split('/')[:3])
            if not root.endswith('/'):
                root += '/'
            self.tree.setdefault(ep.app, {})
            self.tree[ep.app].setdefault(root, [])
            self.tree[ep.app][root].append(ep)

    def get_all_view_names(self, urlpatterns, parent_pattern=None):
        for pattern in urlpatterns:
            if isinstance(pattern, RegexURLResolver):
                pp = None if pattern._regex == '^' else pattern
                for ep in self.get_all_view_names(pattern.url_patterns, parent_pattern=pp):
                    yield ep
            elif isinstance(pattern, RegexURLPattern) and self._is_drf_view(pattern):
                suffix = '?P<%s>' % api_settings.FORMAT_SUFFIX_KWARG
                if suffix not in pattern.regex.pattern:
                    yield ApiEndpoint(pattern, parent_pattern)

    def _is_drf_view(self, pattern):
        return hasattr(pattern.callback, 'cls') and issubclass(pattern.callback.cls, APIView)

    def _get_fields(self, fields):
        lines = []
        for field in fields:
            hl = '**' if field['required'] else ''
            cmnt = ' (%s)' % field['help_text'] if field['help_text'] else ''
            txt = '\t* {hl}{name}{hl} -- ``{type}``{cmnt}'.format(hl=hl, cmnt=cmnt, **field)
            lines.append(txt)
        return '\n'.join(lines)

    def generate(self, path):
        for app, endpoints in sorted(self.tree.items()):
            if self.apps and app not in self.apps:
                continue

            conf = apps.get_app_config(app)
            name = conf.verbose_name
            file = '%s.rst' % app

            print '\t* %s' % file

            with open(path + '/' + file, 'w') as f:
                doc = getdoc(conf) or name
                f.write(name + '\n' + '=' * len(name) + '\n\n')
                f.write(doc + '\n\n')
                for endpoint, actions in sorted(endpoints.items(), reverse=True):
                    top = actions[0]
                    fields = top.get_serializer_fields()

                    f.write(endpoint + '\n' + '-' * len(endpoint) + '\n\n')
                    if top.docstring:
                        f.write(top.docstring + '\n\n')

                    doc = top.get_filter_docs()
                    if doc:
                        f.write(doc + '\n')

                    f.write('Supported actions and methods:\n\n')
                    for idx, act in enumerate(actions, start=1):
                        methods = act.methods
                        # 1st line is supposed to be List/Create view -- remove unfeasible methods
                        if idx == 1:
                            methods = [m for m in methods if m not in ('PUT', 'PATCH', 'DELETE')]
                        # 2nd line is supposed to be Retrieve/Update/Delete view
                        if idx == 2 and act.path.endswith('>/'):
                            methods = [m for m in methods if m != 'POST']

                        f.write('.. topic:: ``%s``' % act.path + '\n\n')
                        f.write('\tMethods: ' + ', '.join(['``%s``' % m for m in methods]) + '\n\n')

                        # 1st line is supposed to be List/Create view -- add proper details
                        if idx == 1:
                            create_fields = [o for o in fields if not o['readonly']]
                            if 'POST' in methods and create_fields:
                                f.write('\tSupported fields for creation:\n\n')
                                f.write(self._get_fields(create_fields))
                                f.write('\n')

                        # 2nd line is supposed to be Retrieve/Update/Delete view
                        if idx == 2 and act.path.endswith('>/'):
                            update_fields = [o for o in fields if not o['readonly'] and not o['protected']]
                            if 'PUT' in methods and update_fields:
                                f.write('\tSupported fields for update:\n\n')
                                f.write(self._get_fields(update_fields))
                                f.write('\n')

                        cls = act.callback.cls
                        if act.docstring:
                            self._write_docstring(f, act.docstring)

                        if act.action:
                            doc = getdoc(getattr(cls, act.action))
                            if doc:
                                self._write_docstring(f, doc)
                        else:
                            # docs for ViewSets
                            for method, a in act.METHODS:
                                if idx == 1 and a == 'retrieve':
                                    continue
                                if idx >= 2 and a == 'list':
                                    continue

                                action = getattr(cls, a, None)
                                if action:
                                    doc = getdoc(action, warning=False)
                                    if doc:
                                        self._write_docstring(f, doc)

                            else:
                                continue

                            # docs for classic Views
                            for method in methods:
                                try:
                                    doc = getdoc(getattr(cls, method.lower()), warning=False)
                                except AttributeError:
                                    continue
                                if doc:
                                    self._write_docstring(f, doc)

                    f.write('\n\n')

        with open(path + '/index.rst', 'w') as f:
            lines = [
                "",
                ".. toctree::",
                "   :glob:",
                "   :titlesonly:",
                "   :maxdepth: 1",
                "",
                "   **",
            ]
            f.writelines([l + '\n' for l in lines])

    def _write_docstring(self, file, docstring):
        file.write('\n'.join(['\t' + s for s in docstring.split('\n')]) + '\n')


class ApiEndpoint(object):
    FIELDS = {
        # filter
        'BooleanFilter': 'boolean',
        'CharFilter': 'string',
        'TimestampFilter': 'UNIX timestamp',
        'QuotaFilter': 'float',
        'URLFilter': 'link',
        # serializer
        'BooleanField': 'boolean',
        'CharField': 'string',
        'DecimalField': 'float',
        'FloatField': 'float',
        'FileField': 'file',
        'EmailField': 'email',
        'IntegerField': 'integer',
        'IPAddressField': 'IP address',
        'HyperlinkedRelatedField': 'link',
        'URLField': 'URL',
    }

    METHODS = [
        ('GET', 'list'),
        ('GET', 'retrieve'),
        ('POST', 'create'),
        ('PUT', 'update'),
        ('PATCH', 'partial_update'),
        ('DELETE', 'destroy')
    ]

    VIEWS = {}

    def __init__(self, pattern, parent_pattern=None):
        # XXX: Hotfix for openstack app docs.
        app_name = pattern.callback.cls.__module__.split('.')[-2]
        app_name = app_name.replace('waldur_openstack', 'openstack')
        app_name = app_name.replace('waldur_auth_social', 'nodeconductor_auth')
        conf = apps.get_app_config(app_name)
        self.pattern = pattern
        self.callback = pattern.callback
        self.docstring = getdoc(self.callback.cls)
        self.name_parent = simplify_regex(parent_pattern.regex.pattern).replace('/', '') if parent_pattern else None
        self.path = self.get_path(parent_pattern)
        self.name = conf.verbose_name
        self.app = conf.label

        cls = self.callback.cls
        action = self.path.split('/')[-2]

        self.actions = [m for m in dir(cls) if hasattr(getattr(cls, m), 'bind_to_methods')]
        self.action = action if action in self.actions else None
        if self.action:
            self.methods = [m.upper() for m in reduce(getattr, [action, 'bind_to_methods'], cls)]
        else:
            self.methods = []
            for method, action in self.METHODS:
                if method in self.methods:
                    continue
                if hasattr(cls, action) or hasattr(cls, method.lower()):
                    self.methods.append(method)

        self.VIEWS[pattern.name] = self.path

    def get_serializer_fields(self, cls=None):
        if not cls:
            try:
                cls = self.callback.cls.serializer_class
            except AttributeError:
                return []

        if isinstance(cls, type):
            serializer = cls(context=get_fake_context())
        else:
            serializer = cls
        if cls:
            meta = getattr(serializer, 'Meta', object())
            ro = getattr(meta, 'read_only_fields', [])
            wo = getattr(meta, 'write_only_fields', [])
            pt = getattr(meta, 'protected_fields', [])
            return [{
                "name": key,
                "type": self._get_field_type(field),
                "help_text": field.help_text,
                "required": field.required,
                "readonly": key in ro or isinstance(field, ReadOnlyField) or field.read_only,
                "writeonly": key in wo or field.write_only,
                "protected": key in pt,
            } for key, field in serializer.get_fields().items()]

        return []

    def get_filter_docs(self):
        try:
            docs = []
            for cls in self.callback.cls.filter_backends:
                doc = getdoc(cls)
                if not doc.startswith('..drfdocs-ignore'):
                    docs.append(doc)
            return '\n\n'.join(docs)
        except AttributeError:
            return ''

    def get_path(self, parent_pattern):
        if parent_pattern:
            return "/{}{}".format(self.name_parent, simplify_regex(self.pattern.regex.pattern))
        return simplify_regex(self.pattern.regex.pattern)

    def _get_field_type(self, field):
        if isinstance(field, MappedMultipleChoiceFilter):
            return 'choice(%s)' % ', '.join(["'%s'" % f for f in sorted(field.mapped_to_model)])
        if isinstance(field, ChoiceField):
            return 'choice(%s)' % ', '.join(["'%s'" % f for f in sorted(field.choices)])
        if isinstance(field, HyperlinkedRelatedField):
            path = self.VIEWS.get(field.view_name)
            if path:
                return 'link to %s' % path
        if isinstance(field, GenericRelatedField):
            paths = [self.VIEWS.get(GenericRelatedField()._get_url(m())) for m in field.related_models]
            path = ', '.join([m for m in paths if m])
            if path:
                return 'link to any: %s' % path
        if isinstance(field, ContentTypeFilter):
            return 'string in form <app_label>.<model_name>'
        if isinstance(field, ModelSerializer):
            fields = {f['name']: f['type'] for f in self.get_serializer_fields(field) if not f['readonly']}
            return '{%s}' % ', '.join(['%s: %s' % (k, v) for k, v in fields.items()])
        if isinstance(field, ModelMultipleChoiceFilter):
            return self._get_field_type(field.field)
        if isinstance(field, ListSerializer):
            return 'list of [%s]' % self._get_field_type(field.child)
        if isinstance(field, ManyRelatedField):
            return 'list of [%s]' % self._get_field_type(field.child_relation)
        if isinstance(field, ModelField):
            return self._get_field_type(field.model_field)
        name = field.__class__.__name__
        return self.FIELDS.get(name, name)

    def __repr__(self):
        return "%s -- %s" % (self.path, ', '.join(self.methods))
