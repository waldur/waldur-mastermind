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

#### Requirements

- **Component Dependency**: Must reference an existing limit-based component (`billing_type: "limit"`)
- **Factor**: Must be a positive integer ≥ 1
- **Limits**: `min_limit` and `max_limit` apply to user input, not the calculated result

#### Implementation Components

- **Configuration**: `ComponentMultiplierConfiguration.tsx` - Admin interface for setting up the multiplier
- **User Field**: `ComponentMultiplierField.tsx` - User input field that handles multiplication and limit updates
