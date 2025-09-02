# Waldur Django Architecture

## Project Structure Overview

**Waldur MasterMind** is a Django-based cloud orchestration platform built with a highly modular, plugin-based architecture demonstrating advanced Django patterns and enterprise-level design principles.

## Settings Configuration

- **Hierarchical Settings**: `base_settings.py` (core) → `settings.py` (local) → specialized settings
- **Extension System**: Automatic discovery and registration of plugins via WaldurExtension
- **Multi-database**: PostgreSQL primary with optional read replicas
- **REST Framework**: Custom authentication (Token, SAML2, OIDC, OAuth)
- **Celery Integration**: Distributed task processing with priority queues

## Django Apps Organization

### Core Layer (`waldur_core/`)

- **`core`**: Foundation with extension system, base models, authentication
- **`structure`**: Organizational hierarchy (customers → projects → resources)
- **`users`**: User management with profiles
- **`permissions`**: Role-based access control with hierarchical scoping
- **`quotas`**: Resource quota management
- **`logging`**: Event logging and audit trail

### Business Logic Layer (`waldur_mastermind/`)

- **`marketplace`**: Central service catalog and provisioning (assembly app)
- **`billing`**: Financial management and invoicing
- **`support`**: Integrated support ticket system
- **`analytics`**: Usage analytics and reporting

### Provider Integration Layer

- **Cloud Providers**: OpenStack, AWS, Azure, VMware, DigitalOcean
- **Compute Platforms**: Rancher, SLURM, Kubernetes
- **Authentication**: SAML2, Social/OAuth, Valimo

## URL Routing and API Structure

- **Base Path**: All REST endpoints under `/api/`
- **Router System**: `SortedDefaultRouter` + `NestedSimpleRouter` for hierarchical resources
- **Naming Convention**: Hyphenated resource names, UUID-based lookup
- **Extension Registration**: Automatic URL discovery through plugin system

## Models, Serializers, and Views Architecture

### Model Architecture

- **Mixin-based Design**: `UuidMixin`, `StateMixin`, `LoggableMixin` for code reuse
- **Hierarchical Structure**: Customer → Project → Resource relationships
- **State Management**: FSM-based transitions with django-fsm
- **Soft Deletion**: Logical deletion for data retention

### Serializer Patterns

- **`AugmentedSerializerMixin`**: Dynamic field injection via signals
- **Permission Integration**: Automatic queryset filtering
- **Eager Loading**: Query optimization through `eager_load()` methods
- **Field Protection**: Sensitive field protection during updates
- **Related Fields**: ALWAYS use SlugRelatedField with slug_field="uuid" instead of PrimaryKeyRelatedField

### ViewSet Architecture

- **`ActionsViewSet`**: Base class with action-specific serializers
- **`ExecutorMixin`**: Asynchronous resource operations
- **Permission Integration**: Automatic permission checking
- **Atomic Transactions**: Configurable transaction support

## Authentication and Permissions

- **Multi-modal Auth**: Token, Session, OIDC, SAML2 support
- **Impersonation**: Staff user impersonation with audit trail
- **RBAC System**: Hierarchical role-based access control
- **Scope-based Permissions**: Customer/Project/Resource level permissions
- **Time-based Roles**: Role assignments with expiration

## Signal Handlers

- **Organization**: Place signal handlers in dedicated `handlers.py` files, not in models.py
- **Registration**: Register signals in `apps.py` ready() method with proper dispatch_uid

## Task Queue and Background Processing

- **Celery Queues**: `tasks`, `heavy`, `background` with priority routing
- **Beat Scheduler**: Scheduled task system (24+ tasks)
- **Event Context**: Thread-local context passing to background tasks
- **Extension Tasks**: Automatic task registration from plugins

## Key Design Patterns

- **Plugin Architecture**: WaldurExtension base class for extensibility
- **Assembly Pattern**: Marketplace loaded last as it depends on others
- **Factory Pattern**: Extensions create Django apps dynamically
- **Observer Pattern**: Extensive use of Django signals
- **State Machine**: FSM-based resource state management
- **Mixin Pattern**: Code reuse through multiple inheritance

## Architecture Strengths

1. **Modularity**: Clean separation of concerns with extension system
2. **Scalability**: Multi-tenant architecture with horizontal scaling
3. **Extensibility**: Plugin system for easy provider addition
4. **Security**: Authentication and authorization layers
5. **Auditability**: Complete event logging and audit trail
6. **Maintainability**: Consistent patterns and well-structured code
7. **Performance**: Optimized queries and caching strategies
