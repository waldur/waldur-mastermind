from django.db import models

from waldur_core.core.managers import GenericKeyMixin, SummaryQuerySet


def get_permission_subquery(permissions, user):
    subquery = models.Q()
    for entity in ('customer', 'project'):
        path = getattr(permissions, '%s_path' % entity, None)
        if not path:
            continue

        if path == 'self':
            prefix = 'permissions__'
        else:
            prefix = path + '__permissions__'

        kwargs = {
            prefix + 'user': user,
            prefix + 'is_active': True
        }

        subquery |= models.Q(**kwargs)

    # Add extra query which basically allows to
    # additionally filter by some flag and ignore permissions
    extra_query = getattr(permissions, 'extra_query', None)
    if extra_query:
        subquery |= models.Q(**extra_query)

    return subquery


def filter_queryset_for_user(queryset, user):
    if user is None or user.is_staff or user.is_support:
        return queryset

    try:
        permissions = queryset.model.Permissions
    except AttributeError:
        return queryset

    subquery = get_permission_subquery(permissions, user)
    if not subquery:
        return queryset

    return queryset.filter(subquery).distinct()


class StructureQueryset(models.QuerySet):
    """ Provides additional filtering by customer or project (based on permission definition).

        Example:

            .. code-block:: python

                Instance.objects.filter(project=12)

                Droplet.objects.filter(
                    customer__name__startswith='A',
                    state=Droplet.States.ONLINE)

                Droplet.objects.filter(Q(customer__name='Alice') | Q(customer__name='Bob'))
    """

    def exclude(self, *args, **kwargs):
        return super(StructureQueryset, self).exclude(
            *[self._patch_query_argument(a) for a in args],
            **self._filter_by_custom_fields(**kwargs))

    def filter(self, *args, **kwargs):
        return super(StructureQueryset, self).filter(
            *[self._patch_query_argument(a) for a in args],
            **self._filter_by_custom_fields(**kwargs))

    def _patch_query_argument(self, arg):
        # patch Q() objects if passed and add support of custom fields
        if isinstance(arg, models.Q):
            children = []
            for opt in arg.children:
                if isinstance(opt, models.Q):
                    children.append(self._patch_query_argument(opt))
                else:
                    args = self._filter_by_custom_fields(**dict([opt]))
                    children.append(tuple(args.items())[0])
            arg.children = children
        return arg

    def _filter_by_custom_fields(self, **kwargs):
        # traverse over filter arguments in search of custom fields
        args = {}
        fields = [f.name for f in self.model._meta.get_fields()]
        for field, val in kwargs.items():
            base_field = field.split('__')[0]
            if base_field in fields:
                args.update(**{field: val})
            elif base_field in ('customer', 'project'):
                args.update(self._filter_by_permission_fields(base_field, field, val))
            else:
                args.update(**{field: val})

        return args

    def _filter_by_permission_fields(self, name, field, value):
        # handle fields connected via permissions relations
        extra = '__'.join(field.split('__')[1:]) if '__' in field else None
        try:
            # look for the target field path in Permissions class,
            path = getattr(self.model.Permissions, '%s_path' % name)
        except AttributeError:
            # fallback to FieldError if it's missed
            return {field: value}
        else:
            if path == 'self':
                if extra:
                    return {extra: value}
                else:
                    return {'pk': value.pk if isinstance(value, models.Model) else value}
            else:
                if extra:
                    path += '__' + extra
                return {path: value}


StructureManager = models.Manager.from_queryset(StructureQueryset)


class ResourceSummaryQuerySet(SummaryQuerySet):
    # Hack for permissions
    @property
    def model(self):
        from waldur_core.structure.models import ResourceMixin
        return ResourceMixin


class ServiceSummaryQuerySet(SummaryQuerySet):
    # Hack for permissions
    @property
    def model(self):
        from waldur_core.structure.models import Service
        return Service


class ServiceSettingsManager(GenericKeyMixin, models.Manager):
    """ Allows to filter and get service settings by generic key """

    def get_available_models(self):
        """ Return list of models that are acceptable """
        from waldur_core.structure.models import ResourceMixin
        return ResourceMixin.get_all_models()


class SharedServiceSettingsManager(ServiceSettingsManager):
    def get_queryset(self):
        return super(SharedServiceSettingsManager, self).get_queryset().filter(shared=True)


class PrivateServiceSettingsManager(ServiceSettingsManager):
    def get_queryset(self):
        return super(PrivateServiceSettingsManager, self).get_queryset().filter(shared=False)
