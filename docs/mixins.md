# Mixin classes documentation

This document lists all mixin classes found in the Waldur codebase.

| Mixin Name | Module | Short Description |
|------------|--------|-------------------|
| [`BaseChecklistMixin`](#basechecklistmixin) | `waldur_core.checklist.mixins` | Base mixin providing common checklist functionality |
| [`ReviewerChecklistMixin`](#reviewerchecklistmixin) | `waldur_core.checklist.mixins` | Mixin for ViewSets that provide checklist review functionality to reviewers |
| [`UserChecklistMixin`](#userchecklistmixin) | `waldur_core.checklist.mixins` | Mixin for ViewSets that provide checklist functionality to end users |
| [`CopyButtonMixin`](#copybuttonmixin) | `waldur_core.core.admin` | Mixin to add copy-to-clipboard functionality to form fields in Django admin |
| [`ExcludedFieldsAdminMixin`](#excludedfieldsadminmixin) | `waldur_core.core.admin` | This mixin allows to toggle display of fields in Django model admin according... |
| [`ExtraActionsMixin`](#extraactionsmixin) | `waldur_core.core.admin` | Allows to add extra actions to admin list page |
| [`ExtraActionsObjectMixin`](#extraactionsobjectmixin) | `waldur_core.core.admin` | Allows to add extra actions to admin object edit page |
| [`HideAdminOriginalMixin`](#hideadminoriginalmixin) | `waldur_core.core.admin` | Encapsulate all admin options and functionality for a given model |
| [`NativeNameAdminMixin`](#nativenameadminmixin) | `waldur_core.core.admin` | This mixin allows to toggle display of fields in Django model admin according... |
| [`ReadOnlyAdminMixin`](#readonlyadminmixin) | `waldur_core.core.admin` | Disables all editing capabilities |
| [`DeleteExecutorMixin`](#deleteexecutormixin) | `waldur_core.core.executors` | Delete object on success or if force flag is enabled |
| [`ErrorExecutorMixin`](#errorexecutormixin) | `waldur_core.core.executors` | Set object as erred on fail |
| [`SuccessExecutorMixin`](#successexecutormixin) | `waldur_core.core.executors` | Set object as OK on success, cleanup action and its details |
| [`GenericKeyMixin`](#generickeymixin) | `waldur_core.core.managers` | Filtering by generic key field  Support filtering by:  - generic key directly... |
| [`CreateExecutorMixin`](#createexecutormixin) | `waldur_core.core.mixins` | Mixin to execute create operations using background executors |
| [`DeleteExecutorMixin`](#deleteexecutormixin) | `waldur_core.core.mixins` | Mixin to execute delete operations using background executors |
| [`EagerLoadMixin`](#eagerloadmixin) | `waldur_core.core.mixins` | Reduce number of requests to DB |
| [`ExecutorMixin`](#executormixin) | `waldur_core.core.mixins` | Execute create/update/delete operation with executor |
| [`GetValueMixin`](#getvaluemixin) | `waldur_core.core.mixins` | Mixin to provide helper method for getting values from attrs or instance |
| [`ProjectNameTemplateMixin`](#projectnametemplatemixin) | `waldur_core.core.mixins` | Mixin for models that need to generate project names from templates |
| [`ReviewMixin`](#reviewmixin) | `waldur_core.core.mixins` | An abstract base class model that provides self-updating ``created`` and ``mo... |
| [`ReviewStateMixin`](#reviewstatemixin) | `waldur_core.core.mixins` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`ScopeMixin`](#scopemixin) | `waldur_core.core.mixins` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`UpdateExecutorMixin`](#updateexecutormixin) | `waldur_core.core.mixins` | Mixin to execute update operations using background executors |
| [`ActionMixin`](#actionmixin) | `waldur_core.core.models` | Mixin for action tracking with state management |
| [`BackendMixin`](#backendmixin) | `waldur_core.core.models` | Mixin to add standard backend_id field |
| [`BackendModelMixin`](#backendmodelmixin) | `waldur_core.core.models` | Mixin for models connected to backend objects |
| [`DescendantMixin`](#descendantmixin) | `waldur_core.core.models` | Mixin to provide child-parent relationships |
| [`DescribableMixin`](#describablemixin) | `waldur_core.core.models` | Mixin to add a standardized "description" field |
| [`ErrorMessageMixin`](#errormessagemixin) | `waldur_core.core.models` | Mixin to add standardized error handling fields |
| [`LastSyncMixin`](#lastsyncmixin) | `waldur_core.core.models` | Mixin to track last synchronization time |
| [`NameMixin`](#namemixin) | `waldur_core.core.models` | Mixin to add a standardized "name" field with validation |
| [`RuntimeStateMixin`](#runtimestatemixin) | `waldur_core.core.models` | Mixin to provide runtime state tracking |
| [`SlugMixin`](#slugmixin) | `waldur_core.core.models` | Mixin to automatically generate a name-based slug |
| [`StateMixin`](#statemixin) | `waldur_core.core.models` | Mixin implementing finite state machine (FSM) functionality |
| [`UiDescribableMixin`](#uidescribablemixin) | `waldur_core.core.models` | Mixin to add a standardized "description" and "icon url" fields |
| [`UserDetailsMatchMixin`](#userdetailsmatchmixin) | `waldur_core.core.models` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`UserDetailsMixin`](#userdetailsmixin) | `waldur_core.core.models` | This mixin is shared by User and Invitation model |
| [`UuidMixin`](#uuidmixin) | `waldur_core.core.models` | Mixin to identify models by UUID |
| [`LookupMixin`](#lookupmixin) | `waldur_core.core.nested_routers` | Deprecated |
| [`NestedMixin`](#nestedmixin) | `waldur_core.core.nested_routers` | Mixin for creating nested routers that handle hierarchical URL structures |
| [`AugmentedSerializerMixin`](#augmentedserializermixin) | `waldur_core.core.serializers` | This mixin provides several extensions to stock Serializer class:  1 |
| [`RestrictedSerializerMixin`](#restrictedserializermixin) | `waldur_core.core.serializers` | This mixin allows to specify list of fields to be rendered by serializer |
| [`SlugSerializerMixin`](#slugserializermixin) | `waldur_core.core.serializers` | Ensures that slug is editable only by staff |
| [`TranslatedModelSerializerMixin`](#translatedmodelserializermixin) | `waldur_core.core.serializers` | A `ModelSerializer` is just a regular `Serializer`, except that:  * A set of ... |
| [`ExtensionTaskMixin`](#extensiontaskmixin) | `waldur_core.core.tasks` | This mixin allows to skip task scheduling if extension is disabled |
| [`ActionMethodMixin`](#actionmethodmixin) | `waldur_core.core.views` | Implements helper methods for viewset when use separate nested endpoints for ... |
| [`CheckExtensionMixin`](#checkextensionmixin) | `waldur_core.core.views` | Raise exception if extension is disabled |
| [`ConstanceCheckExtensionMixin`](#constancecheckextensionmixin) | `waldur_core.core.views` | Raise exception if extension is disabled |
| [`CreateReversionMixin`](#createreversionmixin) | `waldur_core.core.views` | Mixin to automatically create revision tracking for create operations |
| [`UpdateReversionMixin`](#updatereversionmixin) | `waldur_core.core.views` | Mixin to automatically create revision tracking for update operations |
| [`LoggableMixin`](#loggablemixin) | `waldur_core.logging.mixins` | Mixin to serialize model in logs |
| [`EventTypesMixin`](#eventtypesmixin) | `waldur_core.logging.models` | Mixin to add a event_types and event_groups fields |
| [`UuidMixin`](#uuidmixin) | `waldur_core.logging.models` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`ImageModelMixin`](#imagemodelmixin) | `waldur_core.media.mixins` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`PermissionMixin`](#permissionmixin) | `waldur_core.permissions.mixins` | Base permission management mixin for customer and project |
| [`UserRoleMixin`](#userrolemixin) | `waldur_core.permissions.views` | Mixin to provide user role management functionality for viewsets |
| [`ExtendableQuotaModelMixin`](#extendablequotamodelmixin) | `waldur_core.quotas.models` | Allows to add quotas to model in runtime |
| [`QuotaModelMixin`](#quotamodelmixin) | `waldur_core.quotas.models` | Add general fields and methods to model for quotas usage |
| [`SharedQuotaMixin`](#sharedquotamixin) | `waldur_core.quotas.models` | This mixin updates quotas for several scopes |
| [`ChangeReadonlyMixin`](#changereadonlymixin) | `waldur_core.structure.admin` | Mixin to set different readonly fields for add and change views in Django admin |
| [`FormRequestAdminMixin`](#formrequestadminmixin) | `waldur_core.structure.admin` | This mixin allows you to get current request user in the model admin form, wh... |
| [`ProtectedModelMixin`](#protectedmodelmixin) | `waldur_core.structure.admin` | Mixin to handle protected model deletion errors gracefully in Django admin |
| [`CoordinatesMixin`](#coordinatesmixin) | `waldur_core.structure.mixins` | Mixin to add a latitude and longitude fields |
| [`IPCoordinatesMixin`](#ipcoordinatesmixin) | `waldur_core.structure.mixins` | Mixin to add a latitude and longitude fields |
| [`CustomerDetailsMixin`](#customerdetailsmixin) | `waldur_core.structure.models` | Mixin containing customer detail fields |
| [`ProjectOECDFOS2007CodeMixin`](#projectoecdfos2007codemixin) | `waldur_core.structure.models` | Mixin providing OECD FOS 2007 classification codes for research projects |
| [`ServiceAccountMixin`](#serviceaccountmixin) | `waldur_core.structure.models` | Mixin for models that support service accounts |
| [`StructureLoggableMixin`](#structureloggablemixin) | `waldur_core.structure.models` | Extends LoggableMixin with structure-specific permission filtering |
| [`VATMixin`](#vatmixin) | `waldur_core.structure.models` | Add country and VAT number fields for tax compliance and record keeping |
| [`CountrySerializerMixin`](#countryserializermixin) | `waldur_core.structure.serializers` | The BaseSerializer class provides a minimal class which may be used for writi... |
| [`FieldFilteringMixin`](#fieldfilteringmixin) | `waldur_core.structure.serializers` | Mixin allowing to filter fields by user |
| [`PermissionFieldFilteringMixin`](#permissionfieldfilteringmixin) | `waldur_core.structure.serializers` | Mixin allowing to filter related fields |
| [`SshPublicKeySerializerMixin`](#sshpublickeyserializermixin) | `waldur_core.structure.serializers` | A type of `ModelSerializer` that uses hyperlinked relationships instead of pr... |
| [`ProjectMetadataTestMixin`](#projectmetadatatestmixin) | `waldur_core.structure.tests.test_project_metadata` | Shared test setup and utilities for project metadata tests |
| [`CheckExtensionMixin`](#checkextensionmixin) | `waldur_freeipa.views` | Raise exception if extension is disabled |
| [`PeriodMixin`](#periodmixin) | `waldur_mastermind.invoices.models` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`ConnectedResourceMixin`](#connectedresourcemixin) | `waldur_mastermind.marketplace.admin` | Protects object from modification if there are connected resources |
| [`ParentInlineMixin`](#parentinlinemixin) | `waldur_mastermind.marketplace.admin` | Mixin to get parent object from request in Django admin inline views |
| [`OfferingFilterMixin`](#offeringfiltermixin) | `waldur_mastermind.marketplace.filters` | Mixin to provide common offering-related filters |
| [`CostEstimateMixin`](#costestimatemixin) | `waldur_mastermind.marketplace.models` | Mixin for cost estimation functionality |
| [`ResourceDetailsMixin`](#resourcedetailsmixin) | `waldur_mastermind.marketplace.models` | Mixin combining resource details with cost estimation |
| [`SafeAttributesMixin`](#safeattributesmixin) | `waldur_mastermind.marketplace.models` | Mixin for safe attribute handling |
| [`ConnectedOfferingDetailsMixin`](#connectedofferingdetailsmixin) | `waldur_mastermind.marketplace.views` | Mixin to provide offering details action for connected resources |
| [`PublicViewsetMixin`](#publicviewsetmixin) | `waldur_mastermind.marketplace.views` | Mixin to allow anonymous access to offerings when configured |
| [`TenantMixin`](#tenantmixin) | `waldur_mastermind.marketplace_openstack.processors` | No description available |
| [`SelectiveDNSMockMixin`](#selectivednsmockmixin) | `waldur_mastermind.marketplace_remote.tests.dns_utils` | Mixin class that provides selective DNS mocking for test classes |
| [`ContainerExecutorMixin`](#containerexecutormixin) | `waldur_mastermind.marketplace_script.utils` | Mixin to execute scripts in containers for marketplace script processing |
| [`EstimatedCostPolicyMixin`](#estimatedcostpolicymixin) | `waldur_mastermind.policy.models` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`OfferingPolicySerializerMixin`](#offeringpolicyserializermixin) | `waldur_mastermind.policy.serializers` | This mixin provides several extensions to stock Serializer class:  1 |
| [`BackendNameMixin`](#backendnamemixin) | `waldur_mastermind.support.models` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`FileMixin`](#filemixin) | `waldur_mastermind.support.models` | Mixin to provide file-related functionality and properties |
| [`CheckExtensionMixin`](#checkextensionmixin) | `waldur_mastermind.support.views` | Raise exception if extension is disabled |
| [`ActionDetailsMixin`](#actiondetailsmixin) | `waldur_openstack.admin` | Encapsulate all admin options and functionality for a given model |
| [`ImageMetadataMixin`](#imagemetadatamixin) | `waldur_openstack.admin` | Encapsulate all admin options and functionality for a given model |
| [`MetadataMixin`](#metadatamixin) | `waldur_openstack.admin` | Encapsulate all admin options and functionality for a given model |
| [`TenantQuotaMixin`](#tenantquotamixin) | `waldur_openstack.models` | It allows to update both service settings and shared tenant quotas |
| [`LimitedPerTypeThrottleMixin`](#limitedpertypethrottlemixin) | `waldur_openstack.tasks` | No description available |
| [`TenantMixin`](#tenantmixin) | `waldur_openstack.tests.factories` | No description available |
| [`DataciteMixin`](#datacitemixin) | `waldur_pid.mixins` | A marker model for models that can be registered with PIDs and referred to in... |
| [`RoleMixin`](#rolemixin) | `waldur_rancher.models` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`SettingsMixin`](#settingsmixin) | `waldur_rancher.models` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`SyncDestroyMixin`](#syncdestroymixin) | `waldur_rancher.views` | No description available |
| [`YamlMixin`](#yamlmixin) | `waldur_rancher.views` | No description available |
| [`UsageMixin`](#usagemixin) | `waldur_slurm.models` | Make subclasses preserve the alters_data attribute on overridden methods |
| [`VirtualMachineMixin`](#virtualmachinemixin) | `waldur_vmware.models` | Make subclasses preserve the alters_data attribute on overridden methods |

## Detailed Descriptions

### BaseChecklistMixin

**Module:** `waldur_core.checklist.mixins`

**Description:**
Base mixin providing common checklist functionality.

Provides shared helper methods used by both UserChecklistMixin and ReviewerChecklistMixin.
Should not be used directly - use UserChecklistMixin or ReviewerChecklistMixin instead.

### ReviewerChecklistMixin

**Module:** `waldur_core.checklist.mixins`

**Description:**
Mixin for ViewSets that provide checklist review functionality to reviewers.

Provides actions for designated reviewers to view full checklist information
including sensitive review logic:
- checklist_review: Get full checklist with review logic exposed
- completion_review_status: Get full completion status with review triggers exposed

Security Design:
This mixin exposes privileged review information and should only be used with
proper reviewer permission controls.

IMPORTANT: Must override permissions with app-specific reviewer checks:
- checklist_review_permissions = [permission_factory(...)]  # Reviewer permissions required
- completion_review_status_permissions = [permission_factory(...)]  # Reviewer permissions required

**Base classes:** `BaseChecklistMixin`

### UserChecklistMixin

**Module:** `waldur_core.checklist.mixins`

**Description:**
Mixin for ViewSets that provide checklist functionality to end users.

Provides actions for users filling in checklists or viewing their answers:
- checklist: Get checklist questions with existing answers (hides review logic)
- completion_status: Get completion status (hides review triggers)
- submit_answers: Submit answers to checklist questions

Security Design:
This mixin hides all review logic information to prevent users from gaming
the system by seeing which answers trigger reviews.

Default permissions are IsAdminUser but should be overridden with app-specific permissions:
- checklist_permissions = [permission_factory(...)]
- completion_status_permissions = [permission_factory(...)]
- submit_answers_permissions = [permission_factory(...)]

**Base classes:** `BaseChecklistMixin`

### CopyButtonMixin

**Module:** `waldur_core.core.admin`

**Description:**
Mixin to add copy-to-clipboard functionality to form fields in Django admin.

### ExcludedFieldsAdminMixin

**Module:** `waldur_core.core.admin`

**Description:**
This mixin allows to toggle display of fields in Django model admin according to custom logic.
It's expected that inherited class has implemented excluded_fields property.

**Base classes:** `ModelAdmin`

### ExtraActionsMixin

**Module:** `waldur_core.core.admin`

**Description:**
Allows to add extra actions to admin list page.

### ExtraActionsObjectMixin

**Module:** `waldur_core.core.admin`

**Description:**
Allows to add extra actions to admin object edit page.

### HideAdminOriginalMixin

**Module:** `waldur_core.core.admin`

**Description:**
Encapsulate all admin options and functionality for a given model.

**Base classes:** `ModelAdmin`

### NativeNameAdminMixin

**Module:** `waldur_core.core.admin`

**Description:**
This mixin allows to toggle display of fields in Django model admin according to custom logic.
It's expected that inherited class has implemented excluded_fields property.

**Base classes:** `ExcludedFieldsAdminMixin`

### ReadOnlyAdminMixin

**Module:** `waldur_core.core.admin`

**Description:**
Disables all editing capabilities.
Please ensure that readonly_fields is specified in derived class.

### DeleteExecutorMixin

**Module:** `waldur_core.core.executors`

**Description:**
Delete object on success or if force flag is enabled

### ErrorExecutorMixin

**Module:** `waldur_core.core.executors`

**Description:**
Set object as erred on fail.

### SuccessExecutorMixin

**Module:** `waldur_core.core.executors`

**Description:**
Set object as OK on success, cleanup action and its details.

### GenericKeyMixin

**Module:** `waldur_core.core.managers`

**Description:**
Filtering by generic key field

Support filtering by:
 - generic key directly: <generic_key_name>=<value>
 - is generic key null: <generic_key_name>__isnull=True|False

### CreateExecutorMixin

**Module:** `waldur_core.core.mixins`

**Description:**
Mixin to execute create operations using background executors.

**Base classes:** `AsyncExecutor`

### DeleteExecutorMixin

**Module:** `waldur_core.core.mixins`

**Description:**
Mixin to execute delete operations using background executors.

**Base classes:** `AsyncExecutor`

### EagerLoadMixin

**Module:** `waldur_core.core.mixins`

**Description:**
Reduce number of requests to DB.

Serializer should implement static method "eager_load", that selects
objects that are necessary for serialization.

### ExecutorMixin

**Module:** `waldur_core.core.mixins`

**Description:**
Execute create/update/delete operation with executor

**Base classes:** `CreateExecutorMixin`, `UpdateExecutorMixin`, `DeleteExecutorMixin`

### GetValueMixin

**Module:** `waldur_core.core.mixins`

**Description:**
Mixin to provide helper method for getting values from attrs or instance.

### ProjectNameTemplateMixin

**Module:** `waldur_core.core.mixins`

**Description:**
Mixin for models that need to generate project names from templates.

**Base classes:** `Model`

### ReviewMixin

**Module:** `waldur_core.core.mixins`

**Description:**
An abstract base class model that provides self-updating
``created`` and ``modified`` fields.

**Base classes:** `ReviewStateMixin`, `TimeStampedModel`

### ReviewStateMixin

**Module:** `waldur_core.core.mixins`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

### ScopeMixin

**Module:** `waldur_core.core.mixins`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

### UpdateExecutorMixin

**Module:** `waldur_core.core.mixins`

**Description:**
Mixin to execute update operations using background executors.

**Base classes:** `AsyncExecutor`

### ActionMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin for action tracking with state management.

Extends StateMixin with action tracking fields including action name,
action details (JSON), and task ID for background task tracking.
Used for models that need to track ongoing operations.

**Base classes:** `StateMixin`

### BackendMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin to add standard backend_id field.

Provides a backend_id CharField for storing identifiers from
external backend systems. Used for mapping local objects to
their corresponding backend representations.

**Base classes:** `Model`

### BackendModelMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin for models connected to backend objects.

Represents models that are synchronized with external backend systems.
These models cannot be created or updated via admin interface because
backend queries are not supported in the admin.

### DescendantMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin to provide child-parent relationships.

Each related model can provide list of its parents through the
get_parents() method. Used for hierarchical data structures
where objects have parent-child relationships.

### DescribableMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin to add a standardized "description" field.

**Base classes:** `Model`

### ErrorMessageMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin to add standardized error handling fields.

Provides error_message and error_traceback TextField for storing
error information and debugging details when operations fail.

**Base classes:** `Model`

### LastSyncMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin to track last synchronization time.

Provides a last_sync DateTimeField that defaults to the current time
and is not editable through forms. Used for tracking when data was
last synchronized with external systems.

**Base classes:** `Model`

### NameMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin to add a standardized "name" field with validation.

Provides a CharField with maximum length of 150 characters and
validates the name using the validate_name validator.

**Base classes:** `Model`

### RuntimeStateMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin to provide runtime state tracking.

Adds a runtime_state field with predefined ONLINE/OFFLINE states.
Used to track the current operational status of resources.

**Base classes:** `Model`

### SlugMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin to automatically generate a name-based slug.

Generates unique slugs based on the source field (default: 'name')
during save operations. Uses generate_slug() to ensure uniqueness
by appending numeric suffixes when needed.

**Base classes:** `Model`

### StateMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin implementing finite state machine (FSM) functionality.

Provides state management with transitions between creation, updating,
deletion, OK, and error states. Includes error handling capabilities
and concurrent transition support.

**Base classes:** `ErrorMessageMixin`, `ConcurrentTransitionMixin`

### UiDescribableMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin to add a standardized "description" and "icon url" fields.

Extends DescribableMixin with an icon_url field for UI display purposes.
The icon_url field accepts URLs up to 500 characters.

**Base classes:** `DescribableMixin`

### UserDetailsMatchMixin

**Module:** `waldur_core.core.models`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

### UserDetailsMixin

**Module:** `waldur_core.core.models`

**Description:**
This mixin is shared by User and Invitation model. All fields are optional.
User is populated with these details when invitation is approved.
Note that civil_number and email fields are not included in this mixin
because they have different constraints in User and Invitation model.

**Base classes:** `Model`

### UuidMixin

**Module:** `waldur_core.core.models`

**Description:**
Mixin to identify models by UUID.

Provides a UUID field for unique model identification.
The UUID is automatically generated and used as a primary identifier.

**Base classes:** `Model`

### LookupMixin

**Module:** `waldur_core.core.nested_routers`

**Description:**
Deprecated.

No method override is needed since Django Rest Framework 2.4.

### NestedMixin

**Module:** `waldur_core.core.nested_routers`

**Description:**
Mixin for creating nested routers that handle hierarchical URL structures.

### AugmentedSerializerMixin

**Module:** `waldur_core.core.serializers`

**Description:**
This mixin provides several extensions to stock Serializer class:

1. Add extra fields to serializer from dependent applications in a way
    that doesn't introduce circular dependencies.

    To achieve this, dependent application should subscribe
    to pre_serializer_fields signal and inject additional fields.

    Example of signal handler implementation:

    from waldur_core.structure.serializers import CustomerSerializer

    def add_customer_name(sender, fields, **kwargs):
        fields['customer_name'] = ReadOnlyField(source='customer.name')

    pre_serializer_fields.connect(
        handlers.add_customer_name,
        sender=CustomerSerializer
    )

2. Declaratively add attributes fields of related entities for ModelSerializers.

    To achieve list related fields whose attributes you want to include.

    Example:
        class ProjectSerializer(AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
            class Meta:
                model = models.Project
                fields = (
                    'url', 'uuid', 'name',
                    'customer', 'customer_uuid', 'customer_name',
                )
                related_paths = ('customer',)

        # This is equivalent to listing the fields explicitly,
        # by default "uuid" and "name" fields of related object are added:

        class ProjectSerializer(AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
            customer_uuid = serializers.UUIDField(read_only=True, source='customer.uuid')
            customer_name = serializers.ReadOnlyField(source='customer.name')
            class Meta:
                model = models.Project
                fields = (
                    'url', 'uuid', 'name',
                    'customer', 'customer_uuid', 'customer_name',
                )
                lookup_field = 'uuid'

        # The fields of related object can be customized:

        class ProjectSerializer(AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
            class Meta:
                model = models.Project
                fields = (
                    'url', 'uuid', 'name',
                    'customer', 'customer_uuid',
                    'customer_name', 'customer_native_name',
                )
                related_paths = {
                    'customer': ('uuid', 'name', 'native_name')
                }

3. Protect some fields from change.

    Example:
        class ProjectSerializer(AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
            class Meta:
                model = models.Project
                fields = ('url', 'uuid', 'name', 'customer')
                protected_fields = ('customer',)

4. This mixin overrides "get_extra_kwargs" method and puts "view_name" to extra_kwargs
or uses URL name specified in a model of serialized object.

### RestrictedSerializerMixin

**Module:** `waldur_core.core.serializers`

**Description:**
This mixin allows to specify list of fields to be rendered by serializer.
It expects that request is available in serializer's context.

It is disabled for nested serializers (where parent is another serializer)
but remains active for list views (where parent is a ListSerializer).

### SlugSerializerMixin

**Module:** `waldur_core.core.serializers`

**Description:**
Ensures that slug is editable only by staff

**Base classes:** `Serializer`

### TranslatedModelSerializerMixin

**Module:** `waldur_core.core.serializers`

**Description:**
A `ModelSerializer` is just a regular `Serializer`, except that:

- A set of default fields are automatically populated.
- A set of default validators are automatically populated.
- Default `.create()` and `.update()` implementations are provided.

The process of automatically determining a set of serializer fields
based on the model fields is reasonably complex, but you almost certainly
don't need to dig into the implementation.

If the `ModelSerializer` class *doesn't* generate the set of fields that
you need you should either declare the extra/differing fields explicitly on
the serializer class, or simply use a `Serializer` class.

**Base classes:** `ModelSerializer`

### ExtensionTaskMixin

**Module:** `waldur_core.core.tasks`

**Description:**
This mixin allows to skip task scheduling if extension is disabled.
Subclasses should implement "is_extension_disabled" method which returns boolean value.

**Base classes:** `Task`

### ActionMethodMixin

**Module:** `waldur_core.core.views`

**Description:**
Implements helper methods for viewset when use separate
nested endpoints for create/edit relations objects.
Example:

@decorators.action(detail=True, methods=["get", "post"])
def offerings(self, request, uuid=None):
    return self.action_list_method("requestedoffering_set")(self, request, uuid)

offerings_serializer_class = serializers.RequestedOfferingSerializer

def offering_detail(self, request, uuid=None, obj_uuid=None):
    return self.action_detail_method(
        "requestedoffering_set",
        delete_validators=[],
        update_validators=[
            core_validators.StateValidator(
                RequestedOfferingStates.REQUESTED
            )
        ],
    )(self, request, uuid, obj_uuid)

offering_detail_serializer_class = serializers.RequestedOfferingSerializer

### CheckExtensionMixin

**Module:** `waldur_core.core.views`

**Description:**
Raise exception if extension is disabled

### ConstanceCheckExtensionMixin

**Module:** `waldur_core.core.views`

**Description:**
Raise exception if extension is disabled

### CreateReversionMixin

**Module:** `waldur_core.core.views`

**Description:**
Mixin to automatically create revision tracking for create operations.

### UpdateReversionMixin

**Module:** `waldur_core.core.views`

**Description:**
Mixin to automatically create revision tracking for update operations.

### LoggableMixin

**Module:** `waldur_core.logging.mixins`

**Description:**
Mixin to serialize model in logs.
Extends django model or custom class with fields extraction method.

### EventTypesMixin

**Module:** `waldur_core.logging.models`

**Description:**
Mixin to add a event_types and event_groups fields.

**Base classes:** `Model`

### UuidMixin

**Module:** `waldur_core.logging.models`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

### ImageModelMixin

**Module:** `waldur_core.media.mixins`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

### PermissionMixin

**Module:** `waldur_core.permissions.mixins`

**Description:**
Base permission management mixin for customer and project.
It is expected that reverse `permissions` relation is created for this model.
Provides method to grant, revoke and check object permissions.

### UserRoleMixin

**Module:** `waldur_core.permissions.views`

**Description:**
Mixin to provide user role management functionality for viewsets.

### ExtendableQuotaModelMixin

**Module:** `waldur_core.quotas.models`

**Description:**
Allows to add quotas to model in runtime.

Example:
    from waldur_core.quotas.fields import QuotaField

    QuotaScopeModel.add_quota_field(
        name='quota_name',
        quota_field=QuotaField(...),
    )

**Base classes:** `QuotaModelMixin`

### QuotaModelMixin

**Module:** `waldur_core.quotas.models`

**Description:**
Add general fields and methods to model for quotas usage.

Model with quotas have inherit this mixin.
For quotas implementation such methods and fields have to be defined:
- class Quota(QuotaModelMixin) - class with quotas fields as attributes.

Example:
    Customer(models.Model):
        ...
        Quotas(quotas_models.QuotaModelMixin.Quotas):
            nc_user_count = quotas_fields.QuotaField()  # define user count quota for customers

Use such methods to change objects quotas:
  set_quota_limit, set_quota_usage, add_quota_usage.

**Base classes:** `Model`

### SharedQuotaMixin

**Module:** `waldur_core.quotas.models`

**Description:**
This mixin updates quotas for several scopes.

### ChangeReadonlyMixin

**Module:** `waldur_core.structure.admin`

**Description:**
Mixin to set different readonly fields for add and change views in Django admin.

### FormRequestAdminMixin

**Module:** `waldur_core.structure.admin`

**Description:**
This mixin allows you to get current request user in the model admin form,
which then passed to add_user method, so that user which granted role,
is stored in the permission model.

### ProtectedModelMixin

**Module:** `waldur_core.structure.admin`

**Description:**
Mixin to handle protected model deletion errors gracefully in Django admin.

### CoordinatesMixin

**Module:** `waldur_core.structure.mixins`

**Description:**
Mixin to add a latitude and longitude fields

**Base classes:** `Model`

### IPCoordinatesMixin

**Module:** `waldur_core.structure.mixins`

**Description:**
Mixin to add a latitude and longitude fields

**Base classes:** `CoordinatesMixin`

### CustomerDetailsMixin

**Module:** `waldur_core.structure.models`

**Description:**
Mixin containing customer detail fields.

Provides comprehensive customer information fields including
native name, contact details, agreement number, email, phone,
address, banking information, and external system integration.

**Base classes:** `NameMixin`, `VATMixin`, `CoordinatesMixin`

### ProjectOECDFOS2007CodeMixin

**Module:** `waldur_core.structure.models`

**Description:**
Mixin providing OECD FOS 2007 classification codes for research projects.

Provides standardized classification codes for different scientific fields
according to the OECD Fields of Science and Technology (FOS) 2007 standard.
Used to categorize research projects by their scientific domain.

**Base classes:** `Model`

### ServiceAccountMixin

**Module:** `waldur_core.structure.models`

**Description:**
Mixin for models that support service accounts.

Provides functionality for managing service accounts with
configurable maximum limits. Used by customers and projects
to control service account creation.

**Base classes:** `Model`

### StructureLoggableMixin

**Module:** `waldur_core.structure.models`

**Description:**
Extends LoggableMixin with structure-specific permission filtering.

Provides permission filtering for logging operations based on
structure-specific user permissions and visibility rules.

**Base classes:** `LoggableMixin`

### VATMixin

**Module:** `waldur_core.structure.models`

**Description:**
Add country and VAT number fields for tax compliance and record keeping.
VAT validation is optional and can be done manually or through external services.

**Base classes:** `Model`

### CountrySerializerMixin

**Module:** `waldur_core.structure.serializers`

**Description:**
The BaseSerializer class provides a minimal class which may be used
for writing custom serializer implementations.

Note that we strongly restrict the ordering of operations/properties
that may be used on the serializer in order to enforce correct usage.

In particular, if a `data=` argument is passed then:

.is_valid() - Available.
.initial_data - Available.
.validated_data - Only available after calling `is_valid()`
.errors - Only available after calling `is_valid()`
.data - Only available after calling `is_valid()`

If a `data=` argument is not passed then:

.is_valid() - Not available.
.initial_data - Not available.
.validated_data - Not available.
.errors - Not available.
.data - Available.

**Base classes:** `Serializer`

### FieldFilteringMixin

**Module:** `waldur_core.structure.serializers`

**Description:**
Mixin allowing to filter fields by user.

In order to constrain the list of fields implement
`get_filtered_field()` method returning list of tuples
(field name, func for check access).

### PermissionFieldFilteringMixin

**Module:** `waldur_core.structure.serializers`

**Description:**
Mixin allowing to filter related fields.

In order to constrain the list of entities that can be used
as a value for the field:

1. Make sure that the entity in question has corresponding
   Permission class defined.

2. Implement `get_filtered_field_names()` method
   in the class that this mixin is mixed into and return
   the field in question from that method.

### SshPublicKeySerializerMixin

**Module:** `waldur_core.structure.serializers`

**Description:**
A type of `ModelSerializer` that uses hyperlinked relationships instead
of primary key relationships. Specifically:

- A 'url' field is included instead of the 'id' field.
- Relationships to other instances are hyperlinks, instead of primary keys.

**Base classes:** `HyperlinkedModelSerializer`

### ProjectMetadataTestMixin

**Module:** `waldur_core.structure.tests.test_project_metadata`

**Description:**
Shared test setup and utilities for project metadata tests.

### CheckExtensionMixin

**Module:** `waldur_freeipa.views`

**Description:**
Raise exception if extension is disabled

**Base classes:** `ConstanceCheckExtensionMixin`

### PeriodMixin

**Module:** `waldur_mastermind.invoices.models`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

### ConnectedResourceMixin

**Module:** `waldur_mastermind.marketplace.admin`

**Description:**
Protects object from modification if there are connected resources.

### ParentInlineMixin

**Module:** `waldur_mastermind.marketplace.admin`

**Description:**
Mixin to get parent object from request in Django admin inline views.

### OfferingFilterMixin

**Module:** `waldur_mastermind.marketplace.filters`

**Description:**
Mixin to provide common offering-related filters.

**Base classes:** `FilterSet`

### CostEstimateMixin

**Module:** `waldur_mastermind.marketplace.models`

**Description:**
Mixin for cost estimation functionality.

Provides cost estimation with plan-based calculations and policy
validation. Used for calculating costs based on limits and plans
with policy compliance checking.

**Base classes:** `Model`

### ResourceDetailsMixin

**Module:** `waldur_mastermind.marketplace.models`

**Description:**
Mixin combining resource details with cost estimation.

Provides comprehensive resource details including cost estimation,
safe attributes, and end date management. Used for resource
lifecycle management and billing calculations.

**Base classes:** `SafeAttributesMixin`, `CostEstimateMixin`, `NameMixin`, `SlugMixin`, `DescribableMixin`

### SafeAttributesMixin

**Module:** `waldur_mastermind.marketplace.models`

**Description:**
Mixin for safe attribute handling.

Provides safe attribute functionality excluding secret attributes.
Used for secure attribute access that filters out sensitive
information like passwords and credentials.

**Base classes:** `Model`

### ConnectedOfferingDetailsMixin

**Module:** `waldur_mastermind.marketplace.views`

**Description:**
Mixin to provide offering details action for connected resources.

### PublicViewsetMixin

**Module:** `waldur_mastermind.marketplace.views`

**Description:**
Mixin to allow anonymous access to offerings when configured.

### TenantMixin

**Module:** `waldur_mastermind.marketplace_openstack.processors`

**Description:** No description available.

### SelectiveDNSMockMixin

**Module:** `waldur_mastermind.marketplace_remote.tests.dns_utils`

**Description:**
Mixin class that provides selective DNS mocking for test classes.

Usage:
    class MyTestClass(SelectiveDNSMockMixin, test.APITransactionTestCase):
        def setUp(self):
            super().setUp()
            # Your additional setup code here

### ContainerExecutorMixin

**Module:** `waldur_mastermind.marketplace_script.utils`

**Description:**
Mixin to execute scripts in containers for marketplace script processing.

### EstimatedCostPolicyMixin

**Module:** `waldur_mastermind.policy.models`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `PeriodMixin`

### OfferingPolicySerializerMixin

**Module:** `waldur_mastermind.policy.serializers`

**Description:**
This mixin provides several extensions to stock Serializer class:

1. Add extra fields to serializer from dependent applications in a way
    that doesn't introduce circular dependencies.

    To achieve this, dependent application should subscribe
    to pre_serializer_fields signal and inject additional fields.

    Example of signal handler implementation:

    from waldur_core.structure.serializers import CustomerSerializer

    def add_customer_name(sender, fields, **kwargs):
        fields['customer_name'] = ReadOnlyField(source='customer.name')

    pre_serializer_fields.connect(
        handlers.add_customer_name,
        sender=CustomerSerializer
    )

2. Declaratively add attributes fields of related entities for ModelSerializers.

    To achieve list related fields whose attributes you want to include.

    Example:
        class ProjectSerializer(AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
            class Meta:
                model = models.Project
                fields = (
                    'url', 'uuid', 'name',
                    'customer', 'customer_uuid', 'customer_name',
                )
                related_paths = ('customer',)

        # This is equivalent to listing the fields explicitly,
        # by default "uuid" and "name" fields of related object are added:

        class ProjectSerializer(AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
            customer_uuid = serializers.UUIDField(read_only=True, source='customer.uuid')
            customer_name = serializers.ReadOnlyField(source='customer.name')
            class Meta:
                model = models.Project
                fields = (
                    'url', 'uuid', 'name',
                    'customer', 'customer_uuid', 'customer_name',
                )
                lookup_field = 'uuid'

        # The fields of related object can be customized:

        class ProjectSerializer(AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
            class Meta:
                model = models.Project
                fields = (
                    'url', 'uuid', 'name',
                    'customer', 'customer_uuid',
                    'customer_name', 'customer_native_name',
                )
                related_paths = {
                    'customer': ('uuid', 'name', 'native_name')
                }

3. Protect some fields from change.

    Example:
        class ProjectSerializer(AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
            class Meta:
                model = models.Project
                fields = ('url', 'uuid', 'name', 'customer')
                protected_fields = ('customer',)

4. This mixin overrides "get_extra_kwargs" method and puts "view_name" to extra_kwargs
or uses URL name specified in a model of serialized object.

**Base classes:** `AugmentedSerializerMixin`

### BackendNameMixin

**Module:** `waldur_mastermind.support.models`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

### FileMixin

**Module:** `waldur_mastermind.support.models`

**Description:**
Mixin to provide file-related functionality and properties.

### CheckExtensionMixin

**Module:** `waldur_mastermind.support.views`

**Description:**
Raise exception if extension is disabled

**Base classes:** `ConstanceCheckExtensionMixin`

### ActionDetailsMixin

**Module:** `waldur_openstack.admin`

**Description:**
Encapsulate all admin options and functionality for a given model.

**Base classes:** `ModelAdmin`

### ImageMetadataMixin

**Module:** `waldur_openstack.admin`

**Description:**
Encapsulate all admin options and functionality for a given model.

**Base classes:** `ModelAdmin`

### MetadataMixin

**Module:** `waldur_openstack.admin`

**Description:**
Encapsulate all admin options and functionality for a given model.

**Base classes:** `ModelAdmin`

### TenantQuotaMixin

**Module:** `waldur_openstack.models`

**Description:**
It allows to update both service settings and shared tenant quotas.

**Base classes:** `SharedQuotaMixin`

### LimitedPerTypeThrottleMixin

**Module:** `waldur_openstack.tasks`

**Description:** No description available.

### TenantMixin

**Module:** `waldur_openstack.tests.factories`

**Description:** No description available.

### DataciteMixin

**Module:** `waldur_pid.mixins`

**Description:**
A marker model for models that can be registered with PIDs and referred to in a Datacite PID way.

**Base classes:** `Model`

### RoleMixin

**Module:** `waldur_rancher.models`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

### SettingsMixin

**Module:** `waldur_rancher.models`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

### SyncDestroyMixin

**Module:** `waldur_rancher.views`

**Description:** No description available.

### YamlMixin

**Module:** `waldur_rancher.views`

**Description:** No description available.

### UsageMixin

**Module:** `waldur_slurm.models`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

### VirtualMachineMixin

**Module:** `waldur_vmware.models`

**Description:**
Make subclasses preserve the alters_data attribute on overridden methods.

**Base classes:** `Model`

