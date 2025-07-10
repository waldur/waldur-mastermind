# How to write serializers

This guide provides comprehensive patterns and best practices for writing serializers in Waldur MasterMind, based on analysis of the current codebase architecture.

## Core Serializer Architecture Principles

### Mixin-Based Composition

Waldur uses extensive mixin composition to build complex serializers with reusable functionality. The recommended order follows Python's Method Resolution Order (MRO):

```python
class ResourceSerializer(
    DomainSpecificMixin,                    # e.g., SshPublicKeySerializerMixin
    core_serializers.RestrictedSerializerMixin,    # Field filtering
    PermissionFieldFilteringMixin,          # Security filtering
    core_serializers.AugmentedSerializerMixin,     # Core extensions
    serializers.HyperlinkedModelSerializer,        # DRF base
):
```

### Key Mixin Classes

1. **AugmentedSerializerMixin**: Core functionality for signal injection and related fields
2. **RestrictedSerializerMixin**: Field-level control to avoid over-fetching
3. **PermissionFieldFilteringMixin**: Security filtering based on user permissions
4. **SlugSerializerMixin**: Slug field management with staff-only editing
5. **CountrySerializerMixin**: Internationalization support

## Object Identity and HATEOAS

### UUID-Based Identity

All objects are identified by UUIDs rather than database IDs for distributed database support:

```python
project = serializers.HyperlinkedRelatedField(
    queryset=models.Project.objects.all(),
    view_name='project-detail',
    lookup_field='uuid',  # Always use UUID
    write_only=True
)
```

### Consistent URL Patterns

- Detail views: `{model_name}-detail`
- List views: `{model_name}-list`
- Custom actions: `{model_name}-{action}`

```python
class Meta:
    extra_kwargs = {
        "url": {"lookup_field": "uuid"},
        "customer": {"lookup_field": "uuid"},
        "project": {"lookup_field": "uuid", "view_name": "project-detail"},
    }
```

## Automatic Related Field Generation

### Related Paths Pattern

Use `related_paths` to automatically generate related object fields:

```python
class ProjectSerializer(core_serializers.AugmentedSerializerMixin, ...):
    class Meta:
        model = models.Project
        fields = (
            'url', 'uuid', 'name', 'customer',
            'customer_uuid', 'customer_name', 'customer_native_name'
        )
        related_paths = {
            'customer': ('uuid', 'name', 'native_name', 'abbreviation'),
            'type': ('name', 'uuid'),
        }
```

This automatically generates: `customer_uuid`, `customer_name`, `customer_native_name`, `customer_abbreviation`, etc.

## Security and Permissions

### Permission-Based Field Filtering

Always use `PermissionFieldFilteringMixin` for related fields to ensure users can only reference objects they have access to:

```python
class ResourceSerializer(PermissionFieldFilteringMixin, ...):
    def get_filtered_field_names(self):
        return ('project', 'service_settings', 'customer')
```

### Permission List Serializers

For `many=True` relationships, use `PermissionListSerializer`:

```python
class PermissionProjectSerializer(BasicProjectSerializer):
    class Meta(BasicProjectSerializer.Meta):
        list_serializer_class = PermissionListSerializer
```

### Staff-Only Fields

Restrict sensitive fields to staff users:

```python
class Meta:
    staff_only_fields = (
        "access_subnets", "accounting_start_date",
        "default_tax_percent", "backend_id"
    )

def get_fields(self):
    fields = super().get_fields()
    if not self.context['request'].user.is_staff:
        for field_name in self.Meta.staff_only_fields:
            if field_name in fields:
                fields[field_name].read_only = True
    return fields
```

### Protected Fields

Use `protected_fields` to make fields read-only during updates:

```python
class Meta:
    protected_fields = ("customer", "service_settings", "end_date_requested_by")
```

## Performance Optimization

### Eager Loading

Always implement `eager_load()` static methods for query optimization:

```python
@staticmethod
def eager_load(queryset, request=None):
    return queryset.select_related(
        'customer', 'project', 'service_settings'
    ).prefetch_related(
        'security_groups', 'volumes', 'floating_ips'
    ).only(
        'uuid', 'name', 'created', 'customer__uuid', 'customer__name'
    )
```

### Optional Fields

Mark expensive computation fields as optional:

```python
def get_optional_fields(self):
    return super().get_optional_fields() + ["projects", "billing_price_estimate"]
```

Users can request these fields via `?field=projects` query parameter.

### Restricted Field Rendering

Use `RestrictedSerializerMixin` to allow field selection:

```python
# URL: /api/projects/?field=name&field=uuid
# Only renders 'name' and 'uuid' fields
```

## Complex Validation Patterns

### Hierarchical Validation

Implement validation in layers:

```python
def validate(self, attrs):
    # 1. Cross-field validation
    self.validate_cross_field_constraints(attrs)

    # 2. Permission validation
    if attrs.get('end_date'):
        if not has_permission(self.context['request'],
                             PermissionEnum.DELETE_PROJECT,
                             attrs.get('customer')):
            raise exceptions.PermissionDenied()

    # 3. Business rule validation
    self.validate_business_rules(attrs)

    return attrs
```

### Dynamic Field Behavior

Use `get_fields()` for context-dependent field behavior:

```python
def get_fields(self):
    fields = super().get_fields()

    # Time-based restrictions
    if (isinstance(self.instance, models.Project)
        and self.instance.start_date
        and self.instance.start_date < timezone.now().date()):
        fields["start_date"].read_only = True

    # Role-based restrictions
    if not self.context["request"].user.is_staff:
        fields["max_service_accounts"].read_only = True

    return fields
```

### External API Integration

For external validation (e.g., VAT numbers):

```python
def validate(self, attrs):
    vat_code = attrs.get('vat_code')
    country = attrs.get('country')

    if vat_code:
        # Format validation
        if not pyvat.is_vat_number_format_valid(vat_code, country):
            raise serializers.ValidationError(
                {"vat_code": _("VAT number has invalid format.")}
            )

        # External API validation
        check_result = pyvat.check_vat_number(vat_code, country)
        if check_result.is_valid:
            attrs["vat_name"] = check_result.business_name
            attrs["vat_address"] = check_result.business_address
        elif check_result.is_valid is False:
            raise serializers.ValidationError(
                {"vat_code": _("VAT number is invalid.")}
            )

    return attrs
```

## Service Configuration Patterns

### Options Pattern for Flexible Configuration

Use the options pattern for service-specific configuration without model changes:

```python
class OpenStackServiceSerializer(structure_serializers.ServiceOptionsSerializer):
    class Meta:
        secret_fields = ("backend_url", "username", "password", "certificate")

    # Map to options.* for flexible storage
    availability_zone = serializers.CharField(source="options.availability_zone")
    dns_nameservers = serializers.ListField(source="options.dns_nameservers")
    external_network_id = serializers.CharField(source="options.external_network_id")
```

### Secret Field Management

Protect sensitive configuration data:

```python
class Meta:
    secret_fields = ("password", "certificate", "private_key", "api_token")
```

## Complex Resource Orchestration

### Transactional Resource Creation

For resources that create multiple related objects:

```python
@transaction.atomic
def create(self, validated_data):
    # Extract sub-resource data
    quotas = validated_data.pop("quotas", {})
    subnet_cidr = validated_data.pop("subnet_cidr")

    # Create main resource
    resource = super().create(validated_data)

    # Create related resources
    self._create_default_network(resource, subnet_cidr)
    self._create_security_groups(resource)
    self._apply_quotas(resource, quotas)

    return resource

def _create_default_network(self, resource, cidr):
    # Implementation with proper error handling
    pass
```

## Advanced Serializer Patterns

### Nested Resource Serializers

For complex relationships:

```python
class OpenStackInstanceSerializer(structure_serializers.VirtualMachineSerializer):
    security_groups = OpenStackNestedSecurityGroupSerializer(many=True, required=False)
    floating_ips = OpenStackNestedFloatingIPSerializer(many=True, required=False)
    volumes = OpenStackDataVolumeSerializer(many=True, required=False)

    def validate_security_groups(self, security_groups):
        # Validate security groups belong to same tenant
        return security_groups
```

### Generic Relationships

For polymorphic relationships:

```python
scope = core_serializers.GenericRelatedField(
    related_models=structure_models.BaseResource.get_all_models(),
    required=False,
    allow_null=True,
)

# In model:
resource_content_type = models.ForeignKey(ContentType, ...)
resource_object_id = models.PositiveIntegerField(...)
resource = GenericForeignKey('resource_content_type', 'resource_object_id')
```

## Signal-Based Field Injection

### Extensible Serializers

Avoid circular dependencies by using signals for field injection:

```python
# Host serializer
class ProjectSerializer(core_serializers.AugmentedSerializerMixin, ...):
    pass

# Guest application injects fields
def add_marketplace_resource_uuid(sender, fields, **kwargs):
    fields["marketplace_resource_uuid"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_resource_uuid", get_marketplace_resource_uuid)

core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.ProjectSerializer,
    receiver=add_marketplace_resource_uuid,
)
```

## Standard Meta Class Configuration

### Complete Meta Example

```python
class Meta:
    model = models.MyModel
    fields = (
        "url", "uuid", "name", "customer", "customer_uuid", "customer_name",
        "created", "description", "state", "backend_id"
    )
    extra_kwargs = {
        "url": {"lookup_field": "uuid"},
        "customer": {"lookup_field": "uuid"},
    }
    related_paths = {
        "customer": ("uuid", "name", "native_name"),
    }
    protected_fields = ("customer", "backend_id")
    staff_only_fields = ("backend_id", "internal_notes")
    list_serializer_class = PermissionListSerializer  # For many=True
```

## Custom Field Types

### Specialized Fields

- **HTMLCleanField**: Automatically sanitizes HTML content
- **DictSerializerField**: Handles JSON dictionary serialization
- **GenericRelatedField**: Supports multiple model types in relations
- **MappedChoiceField**: Maps choice values for API consistency

```python
description = core_serializers.HTMLCleanField(required=False, allow_blank=True)
options = serializers.DictField()
state = MappedChoiceField(
    choices=[(v, k) for k, v in CoreStates.CHOICES],
    choice_mappings={v: k for k, v in CoreStates.CHOICES},
    read_only=True,
)
```

## Testing Serializers

### Factory-Based Testing

Use factory classes for test data generation:

```python
def test_project_serializer():
    project = factories.ProjectFactory()
    serializer = ProjectSerializer(project)
    data = serializer.data

    assert 'customer_uuid' in data
    assert 'customer_name' in data
    assert data['url'].endswith(f'/api/projects/{project.uuid}/')
```

### Permission Testing

Test permission-based filtering:

```python
def test_permission_filtering(self, user):
    customer = factories.CustomerFactory()
    project = factories.ProjectFactory(customer=customer)

    # User with no permissions should not see the project
    serializer = ProjectSerializer(context={'request': rf.get('/', user=user)})
    queryset = serializer.fields['customer'].queryset
    assert customer not in queryset
```

## Common Pitfalls and Best Practices

### Do's

1. **Always use UUID lookup fields** for all hyperlinked relationships
2. **Implement eager_load()** for any serializer used in list views
3. **Use PermissionFieldFilteringMixin** for all related fields
4. **Follow the mixin order** for consistent behavior
5. **Use related_paths** for automatic related field generation
6. **Implement comprehensive validation** at multiple levels
7. **Use transactions** for multi-resource creation
8. **Mark expensive fields as optional**

### Don'ts

1. **Don't use `fields = '__all__'`** - always be explicit
2. **Don't forget lookup_field='uuid'** in extra_kwargs
3. **Don't skip permission filtering** for security-sensitive fields
4. **Don't implement custom field logic** without using established patterns
5. **Don't create circular dependencies** - use signal injection instead
6. **Don't ignore performance** - always consider query optimization
7. **Don't hardcode view names** - use consistent naming patterns

## Migration from Legacy Patterns

### Updating Existing Serializers

When updating legacy serializers:

1. Add missing mixins in the correct order
2. Implement `eager_load()` static methods
3. Add `related_paths` for automatic field generation
4. Add permission filtering with `get_filtered_field_names()`
5. Use `protected_fields` instead of custom read-only logic
6. Update to use `lookup_field='uuid'` consistently

This comprehensive guide provides the patterns and practices needed to write maintainable, secure, and performant serializers that follow Waldur's architectural conventions.
