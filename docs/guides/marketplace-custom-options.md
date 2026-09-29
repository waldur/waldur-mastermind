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
  renamed or switched to another billing type. If a plan's billing mode stops
  billing it as a limit, orders on that plan are refused with an error naming
  the option.
- A component may be derived by only one option.
- A sum may not include its own target, and sums may not form a cycle.
