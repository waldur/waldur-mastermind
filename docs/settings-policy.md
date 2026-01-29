# Settings Policy

Settings configure the behavior of Waldur deployments for both core functionality and plugins.

## Plugin Settings

Plugins define their settings in `extension.py`. Not all settings are intended for production override - plugin developers are responsible for documenting which settings can be safely modified.

## Deployment Settings

### Environment Variables

The recommended approach for Docker-based deployments is to use environment variables. Common variables include:

| Variable | Description |
|----------|-------------|
| `GLOBAL_SECRET_KEY` | Django secret key (required) |
| `GLOBAL_DEBUG` | Enable debug mode (default: false) |
| `POSTGRESQL_HOST` | Database host |
| `POSTGRESQL_NAME` | Database name |
| `POSTGRESQL_USER` | Database user |
| `POSTGRESQL_PASSWORD` | Database password |
| `SENTRY_DSN` | Sentry error tracking DSN |
| `AUTH_TOKEN_LIFETIME` | Token lifetime in seconds |

See the [Configuration Guide](../admin-guide/mastermind-configuration/configuration-guide.md) for a complete list.

### Configuration Files

Additional configuration files can be placed in `/etc/waldur/` (or the directory specified by `WALDUR_BASE_CONFIG_DIR`):

| File | Purpose |
|------|---------|
| `override.conf.py` | Override any Django/Waldur settings |
| `logging.conf.py` | Logging configuration |
| `saml2.conf.py` | SAML2 authentication configuration |

These files are loaded in order, allowing later files to override earlier settings.

### Frontend Features

Frontend feature flags are configured through the `WALDUR_CORE` settings. See the [Features documentation](../admin-guide/mastermind-configuration/features.md) for available options.
