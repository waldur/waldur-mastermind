# How to write serializers

## Object identity

When you're writing serializer, you may want user to reliably specify
particular object in API request and serialize object in API response.
Basically there are six aspects to consider:

1) Consistency. We need to ensure consistent serialization format for
    API request and response not only within particular application, but
    also within whole system across different applications.
2) Reliability. We need to reliable identify object using some stable
    field so that value of this field would be the same even if all
    other fields are changed.
3) Security. We need to ensure that user has permission to get access
    to the object in question. Typically API renders 400 error if user
    specifies object he doesn't have access to in API request. On the
    backend side permission check should be done consistently.
4) Universality. There are generic API endpoints which accept objects
    from different application.
5) Performance. We need to consider how much data serializer fetches
    from database so that it wouldn't fetch data which is not used
    anyways and doesn't perform multiple queries when it's enough to
    issue single query.
6) Extensibility. Usually serializer does not have outside
    dependencies. But sometimes it makes sense to inject extra fields to
    the serializer defined in other application.

Therefore you may ask what is the best way to reliably and consistently
identify object in API.

In terms of frontend rendering, user is usually concerned with object
name. Typically we use name only as filtering parameter because names
are not unique. That's why object identity is implemented via a
[UUID](https://en.wikipedia.org/wiki/Universally_unique_identifier).
Please note that usually we're not exposing ID in REST API in favor of
UUID because it allows easy [distribution of databases across multiple
servers](https://blog.codinghorror.com/primary-keys-ids-versus-guids/).

In order to decouple client and server we're implementing
[HATEOAS](https://en.wikipedia.org/wiki/HATEOAS) component of REST API.
That's why usually we're using HyperlinkedRelatedField serializer, for
example:

```python
project = serializers.HyperlinkedRelatedField(
    queryset=models.Project.objects.all(),
    view_name='project-detail',
    lookup_field='uuid',
    write_only=True)
```

There are four notes here:

1) We need to specify `lookup_field` explicitly because it's default
    value is 'pk'.
2) We need to specify `view_name` explicitly in order to avoid clash of
    models names between different applications. You need to ensure that
    it matches view name specified in urls.py module.
3) When debug mode is enabled, you may navigate to related objects via
    hyperlinks using browsable API renderer and select related object
    from the list.
4) Serialized hyperlink contains not only UUID, but also application
    name and model. It allows to use serialized URL as request parameter
    for generic API endpoint. Generic API works with different models
    from arbitrary applications. Thus UUID alone is not enough for full
    unambiguous identification of the object in this case.

## Generic serializers

Typically serializer allows you to specify object related to one
particular database model. However it is not always the case. For
example, issue serializer allows you to specify object related to any
model with quota. In this case you would need to use GenericRelatedField
serializer. It is expected that related\_models parameter provides a
list of all valid models.

```python
class IssueSerializer(JiraPropertySerializer):
    scope = core_serializers.GenericRelatedField(
        source='resource',
        related_models=structure_models.ResourceMixin.get_all_models(),
        required=False
    )
```

Usually `get_all_models` method is implemented in base class and uses
Django application registry which provides access to all registered
models. Consider the following example:

```python
@classmethod
@lru_cache(maxsize=1)
def get_all_models(cls):
    return [model for model in apps.get_models() if issubclass(model, cls)]
```

In terms of database model reference to the resource is stored as
generic foreign key, for example:

```python
resource_content_type = models.ForeignKey(ContentType, blank=True, null=True, related_name='jira_issues')
resource_object_id = models.PositiveIntegerField(blank=True, null=True)
resource = GenericForeignKey('resource_content_type', 'resource_object_id')
```

## Secure serializers

In Waldur we're using role-based-access-control (RBAC) for restricting
system access to authorized users. In terms of serializers there are two
abstract base serializer classes, PermissionFieldFilteringMixin and
PermissionListSerializer which allow to filter related fields. They are
needed in order to constrain the list of entities that can be used as a
value for the field. Consider the following example:

```python
class BaseServiceProjectLinkSerializer(PermissionFieldFilteringMixin,
                                       core_serializers.AugmentedSerializerMixin,
                                       serializers.HyperlinkedModelSerializer):
    project = serializers.HyperlinkedRelatedField(
        queryset=models.Project.objects.all(),
        view_name='project-detail',
        lookup_field='uuid')

    class Meta(object):
        model = NotImplemented
        fields = (
            'project', 'project_name', 'project_uuid',
        )
        related_paths = ('project',)

    def get_filtered_field_names(self):
        return 'project',
```

By using PermissionFieldFilteringMixin we ensure that value of project
field is validated against current user so that only authorized user
which has corresponding role in either project or customer is allowed to
use this serializer.

## High-performance serializers

### Avoiding over-fetching

By default serializer renders value for all fields specified in fields
parameter. However, sometimes user does not really need to transfer all
fields over the network. It is especially important when you're
targeting at mobile users with slow network or even regular users when
serializer renders a lot of data which is thrown away by application
anyways.

If you want to allow user to specify exactly and explicitly list of
fields to render, you just need to use RestrictedSerializerMixin.

### Avoiding under-fetching

By default Django doesn't optimize database queries to the related
objects, so separate query is executed each time when related object is
needed. Fortunately enough, Django provides you with powerful methods to
join database queries together and cache resulting queryset in RAM using
identity map, so that instead of performing multiple consequent queries
to the database it's enough to issue single query.

So in order to reduce number of requests to DB your view should use
EagerLoadMixin. It is expected that corresponding serializer implements
static method `eager_load`, which selects objects necessary for
serialization.

Consider the following example:

```python
class BaseServiceViewSet(core_mixins.EagerLoadMixin, core_views.ActionsViewSet):
    pass


class ServiceSettingsSerializer(PermissionFieldFilteringMixin,
                                core_serializers.AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):

    @staticmethod
    def eager_load(queryset):
        return queryset.select_related('customer').prefetch_related('quotas', 'certifications')
```

## Extensible serializers

Usually serializer does not have outside dependencies, but sometimes it
makes sense to inject extra fields to the serializer defined in other
application so that it would not introduce [circular
dependencies](https://en.wikipedia.org/wiki/Circular_dependency). Please
note that this mechanism should be used with caution as it makes harder
to track dependencies.

The main idea is that instead of introducing circular dependency we're
introducing extension point. This extension point is used in depending
application in order to inject new fields to existing serializer.

Example of host serializer implementation:

```python
class ProjectSerializer(core_serializers.RestrictedSerializerMixin,
                        PermissionFieldFilteringMixin,
                        core_serializers.AugmentedSerializerMixin):
    pass
```

Guest application should subscribe to `pre_serializer_fields` signal and
inject additional fields. Example of signal handler implementation:

```python
def add_price_estimate(sender, fields, **kwargs):
    fields['billing_price_estimate'] = serializers.SerializerMethodField()
    setattr(sender, 'get_billing_price_estimate', get_price_estimate)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.ProjectSerializer,
    receiver=add_price_estimate,
)
```
