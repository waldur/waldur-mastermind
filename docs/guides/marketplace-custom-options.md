# Implementing Custom Marketplace Option Types

This guide explains how to add new option types to Waldur's marketplace offering system, using the `conditional_cascade` implementation as a reference.

## Overview

Waldur marketplace options allow service providers to define custom form fields for their offerings. The system supports various built-in types like `string`, `select_string`, `boolean`, etc., and can be extended with custom types.

## Architecture

The marketplace options system consists of several components:

- **Backend**: Option type validation, serialization, and storage
- **Admin Interface**: Configuration UI for service providers
- **User Interface**: Form fields displayed to users during ordering
- **Form Processing**: Attribute handling during order creation

## Implementation Steps

### 1. Backend: Add Field Type Constant

Add your new type to the `FIELD_TYPES` constant:

**File**: `src/waldur_mastermind/marketplace/serializers.py`

```python
FIELD_TYPES = (
    "boolean",
    "integer",
    "string",
    # ... existing types ...
    "your_custom_type",  # Add your new type here
)
```

### 2. Backend: Create Configuration Serializers

Define serializers for validating your option configuration:

**File**: `src/waldur_mastermind/marketplace/serializers.py`

```python
class YourCustomConfigSerializer(serializers.Serializer):
    # Define configuration fields specific to your type
    custom_param = serializers.CharField(required=False)
    custom_choices = serializers.ListField(child=serializers.DictField(), required=False)

    def validate(self, attrs):
        # Add custom validation logic
        return attrs

class OptionFieldSerializer(serializers.Serializer):
    # ... existing fields ...
    your_custom_config = YourCustomConfigSerializer(required=False)

    def validate(self, attrs):
        field_type = attrs.get("type")

        if field_type == "your_custom_type":
            if not attrs.get("your_custom_config"):
                raise serializers.ValidationError(
                    "your_custom_config is required for your_custom_type"
                )

        return attrs
```

### 3. Backend: Add Order Validation Support

Register your field type for order processing:

**File**: `src/waldur_mastermind/common/serializers.py`

```python
class YourCustomField(serializers.Field):
    """Custom field for handling your specific data format"""

    def to_internal_value(self, data):
        # Validate and process the incoming data
        if not self.is_valid_format(data):
            raise serializers.ValidationError("Invalid format for your_custom_type")
        return data

    def is_valid_format(self, data):
        # Implement your validation logic
        return isinstance(data, dict)  # Example validation

FIELD_CLASSES = {
    # ... existing mappings ...
    "your_custom_type": YourCustomField,
}
```

### 4. Frontend: Add Type Constant

Add the new type to the frontend constants:

**File**: `src/marketplace/offerings/update/options/constants.ts`

```typescript
export const FIELD_TYPES: Array<{ value: OptionFieldTypeEnum; label: string }> =
  [
    // ... existing types ...
    {
      value: "your_custom_type",
      label: "Your Custom Type",
    },
  ];
```

### 5. Frontend: Create Configuration Component

Create an admin configuration component:

**File**: `src/marketplace/offerings/update/options/YourCustomConfiguration.tsx`

```typescript
import { Field } from 'react-final-form';
import { InputField } from '@waldur/form/InputField';
import { translate } from '@waldur/i18n';
import { FormGroup } from '../../FormGroup';

export const YourCustomConfiguration = ({ name }) => {
  return (
    <FormGroup
      label={translate('Your Custom Configuration')}
      description={translate('Configure your custom option type')}
    >
      <Field
        name={`${name}.custom_param`}
        component={InputField}
        placeholder={translate('Enter custom parameter')}
      />
      {/* Add more configuration fields as needed */}
    </FormGroup>
  );
};
```

### 6. Frontend: Create User-Facing Component

Create the component that users see in order forms:

**File**: `src/marketplace/common/YourCustomField.tsx`

```typescript
import { useState, useEffect, useCallback, useRef } from 'react';
import { FormField } from '@waldur/form/types';
import { translate } from '@waldur/i18n';

interface YourCustomFieldProps extends FormField {
  field: {
    your_custom_config?: {
      custom_param?: string;
      // ... other config fields
    };
    label?: string;
    help_text?: string;
  };
}

export const YourCustomField = ({
  field,
  input,
  tooltip,
}: YourCustomFieldProps) => {
  const fieldValue = input?.value || '';
  const [localValue, setLocalValue] = useState<string>(fieldValue);

  const inputRef = useRef(input);
  inputRef.current = input;

  // Sync external changes to local state
  useEffect(() => {
    setLocalValue(fieldValue);
  }, [fieldValue]);

  // Handle user input
  const handleChange = useCallback((newValue: string) => {
    setLocalValue(newValue);
    if (inputRef.current?.onChange) {
      inputRef.current.onChange(newValue);
    }
  }, []);

  return (
    <div className="your-custom-field">
      {tooltip && <div className="form-text text-muted mb-3">{tooltip}</div>}
      {/* Implement your custom UI here */}
      <input
        type="text"
        value={localValue}
        onChange={(e) => handleChange(e.target.value)}
        placeholder={translate('Enter value')}
      />
    </div>
  );
};
```

### 7. Frontend: Update Configuration Forms

Add your type to the option configuration form:

**File**: `src/marketplace/offerings/update/options/OptionForm.tsx`

```typescript
import { YourCustomConfiguration } from './YourCustomConfiguration';

export const OptionForm = ({ resourceType }) => {
  const {values} = useFormState();
  const type = values.type.value;

  return (
    <>
      {/* ... existing form fields ... */}
      {type === 'your_custom_type' && (
        <YourCustomConfiguration name="your_custom_config" />
      )}
      {/* ... rest of form ... */}
    </>
  );
};
```

### 8. Frontend: Update Order Form Rendering

Add your field to the order form renderer:

**File**: `src/marketplace/common/OptionsForm.tsx`

```typescript
import { YourCustomField } from "./YourCustomField";

const getComponentAndParams = (option, key, customer, finalForm = false) => {
  let OptionField: FC<Partial<FormGroupProps>> = StringField;
  let params: Record<string, any> = {};

  switch (option.type) {
    // ... existing cases ...

    case "your_custom_type":
      OptionField = YourCustomField;
      params = {
        field: option,
      };
      break;
  }

  return { OptionField, params };
};
```

### 9. Frontend: Handle Form Data Processing

Update form utilities if needed:

**File**: `src/marketplace/offerings/store/utils.ts`

```typescript
export const formatOption = (option: OptionFormData) => {
  const { type, choices, your_custom_config, ...rest } = option;
  const item: OptionField = {
    type: type.value as OptionFieldTypeEnum,
    ...rest,
  };

  // Handle your custom configuration
  if (your_custom_config && item.type === "your_custom_type") {
    item.your_custom_config = your_custom_config;
  }

  return item;
};
```

**File**: `src/marketplace/details/utils.ts`

```typescript
const formatAttributes = (props): OrderCreateRequest["attributes"] => {
  // ... existing logic ...

  for (const [key, value] of Object.entries(attributes)) {
    const optionConfig = props.offering.options?.options?.[key];

    if (optionConfig?.type === "your_custom_type") {
      // Handle your custom type's data format
      newAttributes[key] = value; // Keep as-is or transform as needed
    } else if (optionConfig?.type === "conditional_cascade") {
      newAttributes[key] = value; // Existing cascade handling
    } else if (typeof value === "object" && !Array.isArray(value)) {
      newAttributes[key] = value["value"]; // Regular select handling
    } else {
      newAttributes[key] = value;
    }
  }

  return newAttributes;
};
```

### 10. Testing

Create comprehensive tests for your new option type:

**File**: `src/waldur_mastermind/marketplace/tests/test_your_custom_type.py`

```python
from rest_framework import test
from waldur_mastermind.marketplace import serializers
from waldur_mastermind.common.serializers import validate_options

class YourCustomTypeTest(test.APITestCase):
    def test_valid_configuration(self):
        """Test that valid configurations are accepted"""
        option_data = {
            "type": "your_custom_type",
            "label": "Custom Field",
            "your_custom_config": {
                "custom_param": "value"
            },
        }

        serializer = serializers.OptionFieldSerializer(data=option_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_order_validation(self):
        """Test that order attributes are validated correctly"""
        options = {
            'custom_field': {
                'type': 'your_custom_type',
                'label': 'Custom Field',
                'required': True,
            }
        }

        attributes = {
            'custom_field': 'valid_value'  # Or whatever format your type expects
        }

        try:
            validate_options(options, attributes)
        except Exception as e:
            self.fail(f"validate_options should accept your_custom_type: {e}")
```

## Key Considerations

### Data Format Consistency

- **Configuration Phase**: How admins configure the option (JSON strings for complex data)
- **Display Phase**: How the option is displayed in forms (parsed objects)
- **Submission Phase**: What format users submit (depends on your UI component)
- **Storage Phase**: How the data is stored in orders/resources (final format)

### Error Handling

- Ensure all error dictionaries use string keys for JSON serialization compatibility
- Provide clear, actionable error messages
- Handle edge cases (empty values, malformed data, etc.)

### Form Integration

- **React-final-form compatibility**: For configuration and user interfaces
- **FormContainer integration**: For most user order forms

### Performance

- Use `useCallback` and `useRef` to prevent unnecessary re-renders
- Avoid object dependencies in `useEffect` that cause infinite loops
- Memoize expensive computations

## Example: Conditional Cascade Implementation

The `conditional_cascade` type demonstrates all these concepts:

### Backend Components

- `CascadeStepSerializer` - Validates individual steps with JSON parsing
- `CascadeConfigSerializer` - Validates overall configuration with dependency checking
- `ConditionalCascadeField` (in common/serializers.py) - Handles order validation

### Frontend Components

- `ConditionalCascadeConfiguration` - Admin configuration interface
- `ConditionalCascadeWidget` - Admin form component
- `ConditionalCascadeField` - User order form component

### Key Features

- **Cascading Dependencies**: Dropdowns that depend on previous selections
- **JSON Configuration**: Complex configuration stored as JSON strings
- **Object Preservation**: Keeps selection objects intact through form processing
- **Bidirectional Sync**: Proper state management between form and component

## Testing Strategy

Create tests covering:

1. **Configuration Validation** - Valid/invalid option configurations
2. **Order Processing** - Attribute validation during order creation
3. **Edge Cases** - Unicode, special characters, empty values, malformed data
4. **Error Handling** - JSON serialization compatibility, clear error messages
5. **Integration** - Mixed field types, form submission end-to-end

## Best Practices

1. **Follow Existing Patterns** - Study similar option types before implementing
2. **Incremental Development** - Implement backend validation first, then frontend
3. **Comprehensive Testing** - Test all data paths and edge cases
4. **Error Prevention** - Use TypeScript interfaces and runtime validation
5. **Documentation** - Document configuration format and usage examples

## Common Pitfalls

1. **JSON Serialization Errors** - Always use string keys in error dictionaries
2. **Infinite Re-renders** - Avoid objects in useEffect dependencies
3. **Form Integration Issues** - Ensure proper `input` prop handling
4. **Data Format Mismatches** - Handle format differences between config/display/submission
5. **Validation Bypass** - Don't forget to add your type to `FIELD_CLASSES` mapping

### Update Frontend Type Handlers

Add your new type to the `OptionValueRenders` object in the frontend:

**File**: `src/marketplace/resources/options/OptionValue.tsx`

```typescript
const OptionValueRenders: Record<OptionFieldTypeEnum, (value) => ReactNode> = {
  // ... existing handlers ...
  your_custom_type: (value) => value, // Add appropriate renderer
};
```

**Important**: If this step is missed, TypeScript compilation will fail with:

```text
Property 'your_custom_type' is missing in type {...} but required in type 'Record<OptionFieldTypeEnum, (value: any) => ReactNode>'
```

Following this guide ensures your custom option type integrates seamlessly with Waldur's marketplace system and provides a consistent user experience.

## Built-in Option Types

### Component Multiplier

The `component_multiplier` option type allows users to input a value that gets automatically multiplied by a configurable factor to set limits for limit-based offering components.

#### Use Case

Perfect for scenarios where users need to specify resources in user-friendly units that need conversion:

- **Storage**: User enters "2 TB", automatically sets 100,000 inodes (2 × 50,000)
- **Compute**: User enters "4 cores", automatically sets 16 GB RAM (4 × 4)
- **Network**: User enters "100 Mbps", automatically sets bandwidth limits in bytes

#### Configuration

**Backend Configuration** (`component_multiplier_config`):

```json
{
  "component_type": "storage_inodes",
  "factor": 50000,
  "min_limit": 1,
  "max_limit": 100
}
```

**Option Definition**:

```json
{
  "storage_size": {
    "type": "component_multiplier",
    "label": "Storage Size (TB)",
    "help_text": "Enter storage size in terabytes",
    "required": true,
    "component_multiplier_config": {
      "component_type": "storage_inodes",
      "factor": 50000,
      "min_limit": 1,
      "max_limit": 100
    }
  }
}
```

#### Behavior

1. **User Input**: User enters a value (e.g., "2" for 2 TB)
2. **Frontend Multiplication**: Value is multiplied by factor (2 × 50,000 = 100,000)
3. **Automatic Limit Setting**: The calculated value (100,000) is automatically set as the limit for the specified component (`storage_inodes`)
4. **Validation**: Frontend validates user input against `min_limit` and `max_limit` before multiplication

The multiplication happens in the order form only; the server stores the
entered value as an attribute and does not recalculate any limit from it. To
derive limits the server enforces, use [Component Formula](#component-formula).

#### Requirements

- **Component Dependency**: Must reference an existing limit-based component (`billing_type: "limit"`)
- **Factor**: Must be a positive integer ≥ 1
- **Limits**: `min_limit` and `max_limit` apply to user input, not the calculated result

#### Implementation Components

- **Configuration**: `ComponentMultiplierConfiguration.tsx` - Admin interface for setting up the multiplier
- **User Field**: `ComponentMultiplierField.tsx` - User input field that handles multiplication and limit updates

### Component Formula

The `component_formula` option type asks the customer for one number and sets
one or more limit-based components from it. The customer orders in their own
terms, such as net database capacity, and the offering derives the gross
quantities it bills for.

#### Configuration

```json
{
  "storage": {
    "type": "component_formula",
    "label": "Required database storage (GB)",
    "required": true,
    "min": 10,
    "max": 5000,
    "component_formula_config": {
      "targets": [
        {"component_type": "data_primary", "formula": "input * 2"},
        {"component_type": "wal_primary", "formula": "input * 2 * 0.25"},
        {"component_type": "data_replica", "formula": "input * 2"},
        {"component_type": "wal_replica", "formula": "input * 2 * 0.25"}
      ]
    }
  }
}
```

`min` and `max` bound the value the customer enters, not the results.

#### Formula language

A formula may use only `input` (the entered value), numbers such as `2` or
`0.25`, the operators `+ - * /`, unary minus and parentheses. There are no
functions and no other names. Formulas are at most 255 characters long.

### Component Sum

The `component_sum` option type sets a limit-based component to the sum of
other limit-based components. The customer does not fill it in; any value sent
for it is dropped.

```json
{
  "backup": {
    "type": "component_sum",
    "label": "Daily full backup",
    "component_sum_config": {
      "target_component": "backup",
      "components": ["data_primary", "wal_primary", "data_replica", "wal_replica"]
    }
  }
}
```

The summed components may be formula targets, components the customer enters
directly, or the targets of other sums.

#### Derived limit behaviour

- **The server calculates the limits.** When an order is created, Waldur
  evaluates the formulas and then the sums, and writes the results into the
  order's limits. It replaces any value the client sent for a derived
  component, so the price and the provisioned quantity always follow the
  configuration. Changing the formula input of a pending order recalculates
  them the same way.
- **Rounding**: each result is rounded up to the target component's
  `limit_decimal_places`. It then passes the component's usual checks
  (minimum, maximum, maximum available), and an error names the component.
- **No input, no limit**: an optional formula left empty, or hidden by
  `visible_if`, derives nothing. A sum is written only when at least one of its
  components has a value.
- **Division by zero or a negative result** is an order validation error.
- **Existing resources**: every later change to a resource's limits (limit
  update, limit change request, renewal, plan switch) recalculates the derived
  limits from the inputs recorded on the resource. A derived value sent by the
  client is replaced and one left out is put back, and a sum follows the
  components it adds up. A resource ordered before the option existed has no
  recorded input and keeps its current derived values. Derived limits cannot
  be reallocated between resources.
- **Provider approval**: a provider who changes a formula input while
  approving an order changes its derived limits and price with it.
- **Order options only**: `component_sum` cannot be a resource option;
  `component_formula` can only as described below.

#### Changing the input after ordering

To let customers change the value after ordering, add a **resource option**
of type `component_formula` with the same internal name as the order option:

```json
{
  "resource_options": {
    "order": ["storage"],
    "options": {
      "storage": {"type": "component_formula", "label": "Required database storage (GB)"}
    }
  }
}
```

- It has no formulas of its own: the order option's are used, and its `min`
  and `max` are copied from the order option when the offering is saved. A
  `component_formula` resource option without such an order option is
  refused, and so is removing or retyping an order option that one pairs with.
  The resource option itself cannot be removed, or re-paired under another
  name, while a resource of the offering holds a value changed since
  ordering: without the pairing, that resource's limits would fall back to
  the ordered value on their next change.
- The value entered at order time is copied onto the resource, so it shows on
  the resource's Options tab (resources ordered earlier show the value from
  their order).
- Changing it through `update_options` always creates an UPDATE order, whatever
  `create_orders_on_resource_option_change` says, carrying the new value
  (`new_options`) and the recalculated limits (`old_limits` and `limits`), so
  it is priced, approved and provisioned like any limit change. When
  completed, the new value and the new limits are applied together.
- A provider who changes the value while approving the order changes the
  limits and price with it. `update_options_direct` refuses to change it,
  since that would bypass the order.
- Later limit changes calculate from the current value: the resource option
  when set, otherwise the value from the order.
- **Defaults and bounds**: a formula option's `default` is used when the input
  is omitted, and `min`/`max` are enforced on every path that changes the
  input, not only on order creation.

#### Validation when the offering is saved

- Every formula must parse under the language above.
- Every component a formula or sum names must be a limit-based component of
  the offering (`billing_type: limit`; prepaid one-time components are not
  accepted).
- While an option refers to a component, the component cannot be removed,
  renamed or switched to another billing type, including by importing the
  offering with `import_offering`. If a plan's billing mode stops
  billing it as a limit, orders on that plan are refused with an error naming
  the option.
- A component may be derived by only one option.
- A sum may not include its own target, and sums may not form a cycle.

### Kubernetes Cluster Configuration

An option of type `k8s_cluster_config` asks the customer for a Kubernetes
cluster: version, topology, the OpenStack infrastructure and node groups of
each datacenter, access rules and egress rules. Its `default_configs` holds
the provider's policy: node sizes, available versions, the load balancer
mode and the topology.

`topology_mode` decides how many sites a cluster spans:

- `1-datacenter`: one site with three controllers.
- `3-datacenter`: three sites with one controller each.
- `customer_choice` (the default when unset): the customer picks one of the two.

The value's `topology`, when present, must be allowed by the mode, and the
number of `datacenters` must match it.

It replaced `single_datacenter_k8s_config` and `multi_datacenter_k8s_config`,
which are no longer accepted. Migration `marketplace.0338` converted the
options of existing offerings and offering templates, setting `topology_mode`
to what the old type implied (`1-datacenter` or `3-datacenter`) where the
option had none.

#### Sites fixed by the provider

By default the customer picks the OpenStack infrastructure of each site from
the tenant offerings they can order. A provider whose automation provisions
the tenants itself sets `infrastructure_mode: provider_fixed` and lists the
sites in `fixed_openstack_offerings`, per topology and in site order:

```json
{
  "infrastructure_mode": "provider_fixed",
  "topology_mode": "customer_choice",
  "fixed_openstack_offerings": {
    "1-datacenter": ["<tenant offering uuid>"],
    "3-datacenter": ["<uuid>", "<uuid>", "<uuid>"]
  }
}
```

- Saving the option checks that every topology `topology_mode` allows has its
  number of distinct `OpenStack.Tenant` offerings. UUIDs are stored in hex.
- A site must be active or paused, and either the provider's own offering or
  one shared with the provider's organisation (as `filter_for_customer`
  decides, organisation groups included). Another organisation's private
  offering is refused: its name and flavors would be shown to everyone who
  sees the Kubernetes offering. The same rule is checked on create, on
  `update_options` / `update_resource_options`, on `import_offering` and when
  the sites are served.
- `GET marketplace-public-offerings/{uuid}/k8s_sites/?option=<name>&topology=<topology>`
  returns the sites of a topology and the flavors every site has under one
  name with the same vCPUs and RAM, each with every site's own flavor UUID.
  Any caller who can see the offering may call it, whether or not they can
  see the OpenStack offerings.
- An order is refused when its datacenters do not use the provider's sites in
  order, when the node groups differ between sites, when a flavor is not that
  site's own or differs in size between the sites, or when a worker flavor is
  below `minimal_worker_vcpus` / `minimal_worker_ram_gb`. Each site's node
  groups carry that site's flavor UUID.

The check runs through `register_option_value_check` in
`common/serializers.py`: `validate_options` calls the checks registered for
an option type on every value its field accepted. The marketplace app
registers this one in `apps.py`, because the field itself cannot reach the
database.

### Kubernetes Egress Rules

A Kubernetes cluster configuration (`k8s_cluster_config`) can carry the
outbound destinations the cluster may reach, in its `egress_rules` list. Waldur validates and stores
them; enforcing them (security groups, network policy, an egress proxy) is
the provider's automation's job.

#### Semantics

- No `egress_rules`, or an empty list: outbound traffic is unrestricted.
- One or more rules: outbound traffic is denied except to those
  destinations and to the offering's baseline destinations.
- Baseline destinations alone do not restrict a cluster.

#### Rule shape

```json
{
  "name": "Registry",
  "destination_type": "domain",
  "destination": "*.docker.io",
  "protocol": "TCP",
  "port_range_min": 443,
  "port_range_max": 443,
  "description": "Container images"
}
```

- `destination_type` is `cidr` (an IPv4 or IPv6 network with an explicit
  prefix length, such as `10.0.0.0/8`; no netmask form, no zone id) or
  `domain` (a hostname of two or more labels whose last label holds a
  letter, optionally led by a `*.` wildcard).
- `protocol` (`TCP`, `UDP`, `ICMP`) and the ports are optional; leaving both
  out allows any traffic to the destination. Ports without a protocol mean
  those ports over TCP and UDP. Ports go from 1 to 65535, ICMP takes none,
  and a single `port_range_min` is stored with `port_range_max` set to it.
- `uuid` is an optional client-side row id, kept as given; ignore it.
- Rules are stored as validated: strings trimmed, ports as numbers, keys
  outside this shape dropped.

The ingress lists of the same configuration, `public_access_rules` and
`administrative_access_rules`, keep `direction: "ingress"` on every row and
refuse rows with `direction: "egress"`.

!!! warning "Migration 0330 changes what existing clusters may reach"
    Migration `marketplace.0330` moved rows with `direction: "egress"` from
    the ingress lists of existing orders and resources into `egress_rules`,
    made valid (a blank name becomes `Egress rule N`, cleared ports become
    any port, ICMP loses its ports), and turned on `egress_rules_enabled` for
    the offerings concerned. Under the semantics above such a cluster is now
    restricted to those destinations and the baseline. Check the automation
    that applies the rules before deploying, and the affected clusters.

#### Offering policy

The k8s option's `default_configs` decides what customers may enter:

```json
{
  "default_configs": {
    "egress_rules_enabled": true,
    "egress_domain_rules_allowed": false,
    "egress_baseline_rules": [
      {"name": "DNS", "destination_type": "cidr", "destination": "10.0.0.53/32",
       "protocol": "UDP", "port_range_min": 53}
    ]
  }
}
```

- Without `egress_rules_enabled`, an order carrying egress rules is refused.
- Without `egress_domain_rules_allowed`, `domain` rules are refused.
- `egress_baseline_rules` use the same rule shape. They are shown to the
  customer read-only and are not copied into the order.

#### Changing the rules after provisioning

Add a resource option of type `k8s_egress_rules`. It is only available as a
resource option, and the offering must have exactly one Kubernetes cluster
order option, which it pairs with:

```json
{
  "resource_options": {
    "order": ["egress"],
    "options": {"egress": {"type": "k8s_egress_rules", "label": "Egress rules"}}
  }
}
```

- When the offering is saved, the server sets `k8s_config_option` to the
  paired order option's name and copies its egress policy into the resource
  option's `default_configs`, so a changed value is checked like an ordered
  one. Changing the order option's policy updates the copy.
- A new resource's value is the ordered `egress_rules` (an empty list when
  none were given), never an order attribute of the option's own name.
- When the option is added, resources created earlier get the rules they
  were ordered with; values they already hold are kept.
- Customers change it through `update_options`. With
  `create_orders_on_resource_option_change` the change becomes an UPDATE
  order with `old_options` and `new_options`; otherwise it is applied
  directly. Either way, read the current rules from the resource's
  `options`, not from its ordered `attributes`.

### Network in CIDR Form

A `cidr` option asks for a network in CIDR notation, or a list of them. It
works as an order option and as a resource option of any offering. The
value is stored as validated: trimmed, written as `ipaddress` writes it
(IPv6 compressed, in lower case).

```json
{
  "type": "cidr",
  "label": "Cluster network",
  "required": true,
  "default_configs": {
    "ip_version": "ipv4",
    "prefix_length_min": 16,
    "prefix_length_max": 28,
    "allowed_ranges": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
    "excluded_ranges": ["10.96.0.0/12"],
    "multiple": false,
    "max_items": 5
  }
}
```

#### Value

- `"10.20.0.0/16"`, or with `multiple` a list such as
  `["10.20.0.0/16", "fd00:1::/64"]`. With `multiple`, a single string is
  accepted and stored as a list of one.
- An empty list is refused for a required option.

#### Offering policy

Every key of `default_configs` is optional; without any, an IPv4 or IPv6
network of any size is accepted.

- `ip_version`: `ipv4`, `ipv6` or `any` (the default).
- `prefix_length_min` / `prefix_length_max`: the largest and the smallest
  network allowed. They apply to IPv4 and IPv6 alike, so with `any` set them
  only when that makes sense for both.
- `allowed_ranges`: the value must lie inside one of them. The order form
  offers a "private only" preset of the RFC 1918 ranges and `fc00::/7`.
- `excluded_ranges`: the value must not overlap any of them.
- `multiple` and `max_items`: a list, and its longest length. A list is
  never longer than 256 entries.

When the offering is saved, the ranges must be networks without host bits
(of the policy's IP version), the prefix lengths must fit the version (0–32
or 0–128) with the smallest not above the largest, and `max_items` must be
at least 1. Keys of other option types are dropped.

#### Refusals

Each is a field error under the option's name:

- a value that is not a network, or a bare address:
  "Enter a network in CIDR notation, such as 10.0.0.0/16.";
- host bits set: "10.0.0.5/24 has host bits set. Did you mean 10.0.0.0/24?";
- the wrong IP version; a prefix length outside the limits;
- a network outside `allowed_ranges`, or overlapping one of
  `excluded_ranges` (the message names that range);
- with `multiple`: entries overlapping each other (the message names both),
  more than `max_items`, or a bad entry, reported by its position
  (`{"net": {"1": ["…"]}}`);
- a list when `multiple` is off.

The Kubernetes egress rules use the same network parser, but keep accepting
a network with host bits set.

### IPsec VPN Configuration

An `ipsec_vpn_config` option collects a site-to-site IPsec VPN: the
customer's gateway, the networks behind it and the IKE and ESP proposals.
It works as an order option and as a resource option of any offering.
Waldur checks and stores the configuration; building the tunnel is the
provider's automation's job. It holds no pre-shared key: add a
[secret option](#secret) next to it for that (see the example there).

#### Value

As stored in order attributes, resource attributes and resource options:

```json
{
  "peer_gateway_ip": "198.51.100.10",
  "peer_id": "",
  "remote_subnets": ["192.168.10.0/24"],
  "ike_version": "ikev2",
  "phase1": {"encryption": "aes256", "integrity": "sha256", "dh_group": 14, "lifetime": 28800},
  "phase2": {"encryption": "aes256", "integrity": "sha256", "pfs_group": 14, "lifetime": 3600},
  "dpd": {"action": "restart", "interval": 30, "timeout": 120},
  "provider": {"gateway_ip": "203.0.113.10", "local_subnets": ["10.0.0.0/16"]}
}
```

- The customer sends `peer_gateway_ip` and `remote_subnets` (at least one);
  the rest is optional. Phase settings left out are filled from the policy's
  defaults, `ike_version` defaults to `ikev2`, and `dpd` is stored as `null`
  when not given.
- `peer_id` is an IP address, a domain name or an identity such as
  `vpn@example.com`.
- `provider` is the provider's end of the tunnel. A customer's value for it
  is ignored: the server keeps the resource's stored `provider`, or sets the
  policy's defaults. The provider changes it per resource with
  `update_options_direct` on a resource option.

#### Offering policy

Every key of `default_configs` is optional:

```json
{
  "ikev1_allowed": false,
  "allow_private_peer": false,
  "provider_gateway_ip": "203.0.113.10",
  "provider_local_subnets": ["10.0.0.0/16"],
  "remote_subnets": {"ip_version": "ipv4", "prefix_length_min": 8, "max_items": 10},
  "no_overlap_with": ["cluster_network"],
  "phase1": {
    "encryption": ["aes256", "aes128", "aes256gcm16"],
    "integrity": ["sha256", "sha384", "sha512"],
    "dh_groups": [14, 19, 20],
    "lifetime_min": 3600, "lifetime_max": 86400,
    "defaults": {"encryption": "aes256", "integrity": "sha256", "dh_group": 14, "lifetime": 28800}
  },
  "phase2": {
    "encryption": ["aes256", "aes128", "aes256gcm16"],
    "integrity": ["sha256", "sha384", "sha512"],
    "pfs_groups": [14, 19, 20],
    "lifetime_min": 900, "lifetime_max": 28800,
    "defaults": {"encryption": "aes256", "integrity": "sha256", "pfs_group": 14, "lifetime": 3600}
  }
}
```

- Vocabularies: encryption `aes128`, `aes192`, `aes256`, `aes128gcm16`,
  `aes256gcm16`, `chacha20poly1305`; integrity `sha1`, `sha256`, `sha384`,
  `sha512`; DH and PFS groups 2, 5, 14, 15, 16, 19, 20, 21, 31. They are
  enums in the API schema (`IpsecEncryptionEnum`, `IpsecIntegrityEnum`,
  `IpsecDhGroupEnum`).
- An allowed list left out is the whole vocabulary except `sha1` and groups
  2 and 5. `pfs_groups` may list `null`, which allows no PFS.
- Defaults left out are AES-256, SHA-256, group 14, 28800 s (phase 1) and
  3600 s (phase 2), or the first allowed value when the provider's list
  excludes the built-in one. Lifetime limits left out are 3600–86400 s
  (phase 1) and 900–28800 s (phase 2).
- `remote_subnets` takes the keys of a `cidr` policy except `multiple`.
- `no_overlap_with` names `cidr` options whose networks the remote subnets
  must not overlap. Order options name order options; resource options name
  resource options, or order options, whose value the resource's attributes
  hold.

When the offering is saved: the defaults must be in the allowed lists and
inside the lifetime limits, the lifetime limits in order, the provider's
address and subnets valid, and every `no_overlap_with` name a `cidr` option
(an order `cidr` option a resource option names cannot be removed).

#### Refusals

Field errors under the option's name, keyed by the value's field
(`{"vpn": {"phase1": {"encryption": ["…"]}}}`):

- a `peer_gateway_ip` that is not an IP address, or, unless
  `allow_private_peer`, one in a private (RFC 1918, `fc00::/7`), loopback,
  link-local, multicast, reserved, unspecified or CGNAT (`100.64.0.0/10`)
  range. Documentation ranges are accepted;
- `ikev1` unless `ikev1_allowed`;
- algorithms or groups outside the allowed lists, lifetimes out of range;
- remote subnets breaking the `cidr` rules, overlapping each other, or
  overlapping the value of a `no_overlap_with` option on the same order or
  resource (on a partial change, the stored value counts);
- remote subnets overlapping the provider end's `local_subnets`, which
  would break routing through the tunnel: "192.168.10.0/24 overlaps the
  provider's subnet 192.168.0.0/16." The provider end is the one the value
  is stored with: the resource's stored `provider`, otherwise the policy's
  `provider_local_subnets`. A provider override through
  `update_options_direct` that overlaps the remote subnets is refused under
  `provider.local_subnets` instead ("172.16.0.0/16 overlaps the remote
  subnet 172.16.0.0/24.");
- a DPD interval or timeout that is not positive, or a timeout shorter than
  the interval.

### Secret

A `secret` option asks for a value that must not be stored in plain text: a
VPN pre-shared key, an API token, a kubeconfig, a certificate's private key.
It works as an order option and as a resource option of any offering.

```json
{
  "type": "secret",
  "label": "VPN pre-shared key",
  "required": true,
  "default_configs": {
    "secret_source": "customer",
    "customer_can_reveal": false,
    "min_length": 8,
    "max_length": 255,
    "multiline": false
  }
}
```

#### Offering policy

Every key of `default_configs` is optional:

- `secret_source`: `customer` (the default; the customer types it) or
  `provider` (the provider or its site agent sets it).
- `customer_can_reveal`: whether users holding `RESOURCE.REVEAL_API_KEY` on
  the resource's project or organization may read it back. When omitted,
  no for a customer-sourced secret and yes for a provider-sourced one.
- `min_length` (1) and `max_length` (4096, at most 65536).
- `multiline`: allows line breaks and tabs, for a PEM key or a kubeconfig;
  the order form then shows a text area. Off when omitted.

When the offering is saved, the lengths must be at least 1, the shortest
not above the longest and the longest within the cap. A secret option
takes no `default`, which would be stored in plain text in the offering.

#### Value

A string, sent only for `secret_source: customer`: in the order's
attributes (which a required option needs), with an edit of the pending
order, in `update_options` or through `set_option_secret`. It is checked
against the length limits; unless `multiline` is on it must be a single
line without control characters, and with it, control characters other
than line breaks and tabs are still refused. An empty string sets nothing.
For `secret_source: provider`, a customer's value is refused, and
`update_options_direct` refuses any value (use `set_option_secret`).

The value is never stored with the other values: it is taken out before
order or resource attributes, resource options or update orders are
written, so it never reaches events, webhooks, exports, site-agent payloads
or API output, and error messages never echo it. Each resource keeps the
values of its secret options in `ResourceOptionSecret`, encrypted in a
`CiphertextField` (`reencrypt_fields` rotates it). A value given with
`update_options` takes effect at once, also when the change itself waits
for an order.

Values that reach Sentry are filtered too: the `value` of a
`set_option_secret` request body by its path, and every value a secret
option received during the request by its content, wherever it appears in
the event (request body, frame locals, messages). Values shorter than 8
characters are filtered only where they are a whole string.

#### Status and actions

Resources and orders show the state of each secret in `option_secrets`,
keyed by the names of the offering's secret options:

```json
{"vpn_key": {"is_set": true, "source": "customer", "modified": "2026-11-25T12:00:00Z"}}
```

Actions, all `POST`, with a body naming the option
(`{"option_name": "vpn_key"}`, plus `"value"` to set one). A response
carrying a value is sent with `Cache-Control: no-store`, and every set and
reveal writes an event (`marketplace_resource_option_secret_set`,
`marketplace_resource_option_secret_revealed`) that never holds the value
and names any impersonator.

| Endpoint | Who | Notes |
| --- | --- | --- |
| `marketplace-resources/{uuid}/set_option_secret/` | who may `update_options` the resource | `secret_source: customer` only |
| `marketplace-resources/{uuid}/reveal_option_secret/` | `RESOURCE.REVEAL_API_KEY` on the project or organization, as for API keys | only with `customer_can_reveal`; 403 otherwise |
| `marketplace-provider-resources/{uuid}/set_option_secret/` | who may `update_options_direct` | any secret option, whatever its source |
| `marketplace-provider-resources/{uuid}/reveal_option_secret/` | who may `update_options_direct` | any secret option |

A reveal with no value set, or one the configured encryption keys cannot
decrypt, answers 409. Values can be set while the resource is creating,
OK, updating or erred.

#### Example: a VPN with its pre-shared key

```json
{
  "order": ["cluster_network", "vpn", "vpn_key"],
  "options": {
    "cluster_network": {"type": "cidr", "label": "Cluster network", "required": true,
                        "default_configs": {"ip_version": "ipv4", "allowed_ranges": ["10.0.0.0/8"]}},
    "vpn": {"type": "ipsec_vpn_config", "label": "Site-to-site VPN", "required": true,
            "default_configs": {"provider_gateway_ip": "203.0.113.10",
                                "no_overlap_with": ["cluster_network"]}},
    "vpn_key": {"type": "secret", "label": "VPN pre-shared key", "required": true,
                "default_configs": {"secret_source": "customer", "min_length": 8,
                                    "max_length": 255}}
  }
}
```

The customer orders with `{"cluster_network": "10.20.0.0/16", "vpn": {...},
"vpn_key": "..."}`. The order and the resource hold `cluster_network` and
`vpn`; `vpn_key` is stored encrypted and shows in `option_secrets`. The
provider's automation reads it with the provider `reveal_option_secret`.
With `"secret_source": "provider"` instead, the provider sets the key after
provisioning and the customer reveals it to configure their gateway.

## Showing a Resource Option as a Usage Limit

A provider's backend may enforce a quota for a component that Waldur only
meters, such as a file count. The quota is held in a resource option, and
`display_limit_config` on that option names the usage-based component it
belongs to. The resource then shows the component's usage against that value.

```json
{
  "resource_options": {
    "order": ["inode_quota"],
    "options": {
      "inode_quota": {
        "type": "integer",
        "label": "Inode quota",
        "display_limit_config": {"component_type": "inodes"}
      }
    }
  }
}
```

- **Shown, not stored**: the resource API returns the option's value in the
  resource's `display_limits` under the component's type, for example
  `"display_limits": {"inodes": 2000000}`. It is computed when the resource is
  read, so the value always follows the option and is not affected by limit
  change orders.
- **`limits` is unchanged**: the resource's `limits` keep holding only the
  stored limits of limit-based components. Clients that send a resource's
  limits back in an order, or compare them with a backend's limits, never see
  the displayed value there.
- **Display only**: the value is not billed, validated as a limit or enforced.
  The component stays usage-based, and usage reports are accepted above it.
- **A stored limit wins**: if the resource holds a limit for the component, or
  its plan bills the component by limit, that limit is reported in `limits`
  and the option's value is not returned in `display_limits`.
- **No value, no limit**: a resource without the option, or with a value that
  is not a non-negative number, shows usage only.
- **Units**: the value is shown as entered, so the option must be in the same
  unit as the usage reported for the component.

### Validation when the offering is saved

- The setting is accepted on resource options of type `integer` only, not on
  order options.
- The component must be a usage-based component of the offering
  (`billing_type: usage`).
- A component takes its displayed limit from one option only.
- While an option refers to a component, the component cannot be removed,
  renamed or switched to another billing type, including by importing the
  offering with `import_offering`.
