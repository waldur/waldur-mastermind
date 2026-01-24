# Waldur Development Scripts

This directory contains utility scripts for Waldur development, testing, and data generation.

## Demo Preset Data Generation

### generate_preset_billing_data.py

Generates realistic billing and reporting demo data for Waldur preset files. This is useful for:

- Frontend chart validation
- Reporting feature testing
- Demo environment setup

**Generated data includes:**

- Invoices (configurable months of history)
- Invoice items (per-resource breakdown)
- Customer credits
- Project credits
- Component usages (with growth trends)
- Component user usages (per-user breakdown)

**Usage:**

```bash
# Generate 12 months of billing data (default)
python scripts/generate_preset_billing_data.py src/waldur_mastermind/marketplace/demo_presets/presets/ai_factory.json

# Generate 6 months of data
python scripts/generate_preset_billing_data.py my_preset.json --months 6

# Save to a different file
python scripts/generate_preset_billing_data.py input.json --output output_with_billing.json

# Use a seed for reproducible output
python scripts/generate_preset_billing_data.py preset.json --seed 42
```

**Prerequisites:**

The input preset must have:

- `customers` - Organizations to generate invoices for
- `projects` - Projects to allocate costs to
- `resources` - Resources to generate usage for
- `offerings` - Service offerings
- `offering_components` - Components for usage tracking

## Changelog Generation

### generate_ansible_changelog.py

Generates changelog entries from git commits for Ansible deployment.

```bash
python scripts/generate_ansible_changelog.py
```

## Testing and Simulation

### access_log_emulator.py

Emulates access log patterns for load testing and log analysis development.

### simulate_rabbitmq_load.py

Simulates RabbitMQ message load for testing message queue performance and celery task processing.

## Documentation Validation

### validate_docs.py

Validates documentation files for correctness and consistency.

```bash
python scripts/validate_docs.py
```

## Adding New Scripts

When adding new scripts to this directory:

1. Include a docstring explaining the script's purpose
2. Use `argparse` for command-line arguments
3. Add usage examples to this README
4. Make the script executable: `chmod +x script_name.py`
5. Follow Python best practices (type hints, error handling)
