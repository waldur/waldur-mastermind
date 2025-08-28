# Developer's Guide to OpenAPI Schema Generation in Waldur

This document provides an in-depth explanation of our approach to generating a high-quality OpenAPI 3 schema for the Waldur API using `drf-spectacular`. A well-defined schema is critical for API documentation, client generation, automated testing, and providing a clear contract for our API consumers.

We heavily customize `drf-spectacular`'s default behavior to produce a schema that is not only accurate but also rich with metadata, developer-friendly, and reflective of Waldur's specific architecture and conventions.

---

## 1. Architectural Overview

`drf-spectacular` generates a schema by introspecting your Django Rest Framework project. Our customizations hook into this process at four key stages, each handled by a different component:

| Component                           | File                    | Responsibility                                                                                                                                                                                                   | When to Use                                                                                                                                                              |
| :---------------------------------- | :---------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Endpoint Enumerator**             | `openapi_generators.py` | **Discovering Endpoints.** Controls *which* API endpoints and methods are included in the schema.                                                                                                                | When you need to globally filter out views or methods based on a project-specific convention (e.g., a `disabled_actions` property on a viewset).                         |
| **Schema Inspector (`AutoSchema`)** | `openapi_inspector.py`  | **Analyzing Individual Endpoints.** The main workhorse. It inspects a single view/method to determine its parameters, request/response bodies, description, operation ID, and other details.                     | For the majority of customizations related to a specific endpoint's representation, like adding custom parameters, modifying descriptions, or adding vendor extensions.  |
| **Extensions**                      | `openapi_extensions.py` | **Handling Custom Components.** Provides explicit schema definitions for custom classes (Authentication, Serializer Fields, Serializers) that `drf-spectacular` cannot introspect automatically.                 | When you have a reusable custom class (e.g., `GenericRelatedField`) that needs a consistent representation across the entire schema.                                     |
| **Post-processing Hooks**           | `schema_hooks.py`       | **Modifying the Final Schema.** Functions that run on the fully generated schema just before it's rendered. They are used for global search-and-replace operations, refactoring, and complex structural changes. | For broad, cross-cutting changes like adding a header to all list endpoints, refactoring common parameters into components, or implementing complex polymorphic schemas. |

The generation process flows like this:
**Enumerator** → **Inspector** (for each endpoint) → **Extensions** (as needed by Inspector) → **Schema Hooks** → **Final OpenAPI YAML/JSON**

---

## 2. The Core Inspector: `WaldurOpenApiInspector`

This class, located in `openapi_inspector.py`, is our custom subclass of `AutoSchema` and contains the most significant logic for tailoring the schema endpoint-by-endpoint.

### Key Methods and Use-Cases

#### `resolve_serializer(...)`

- **Purpose**: To ensure that when a serializer is instantiated during schema generation, it's aware of this context.
- **Mechanism**: It calls the parent method and then sets a flag `_is_generating_schema = True` on the mock request object within the serializer's context.
- **Design Rationale**: Several of our custom serializers change their behavior based on this flag. For example, `RestrictedSerializerMixin` might include all its fields, even optional ones, if this flag is set. This gives API consumers a complete picture of all possible data they *could* receive.

#### `get_operation(...)`

- **Purpose**: To enrich the generated "operation" object with Waldur-specific metadata and logic.
- **Edge Cases Handled**:
    1. **HEAD method for Lists**: We map the `HEAD` HTTP method to a "count" operation for list views. The inspector provides a custom description and a simple `200` response. Crucially, it returns `None` for detail views (`/api/users/{uuid}/`), effectively hiding this non-sensical operation.
    2. **Custom Permissions Metadata**: This is a powerful feature for our frontend developers. If a view action has a `_permissions` attribute (e.g., `create_permissions`), the inspector extracts this data and injects it into the schema under a custom `x-permissions` vendor extension. This allows the frontend to understand the permissions required for an action without hardcoding them.

    ```yaml
    # Example Output
    "/api/projects/":
      post:
        summary: "Create a new project"
        x-permissions:
          - permission: "project.create"
            scopes: ["customer"]
    ```

#### `get_description()`

- **Purpose**: To pull the docstring from the correct viewset *action* (`create`, `retrieve`, `my_action`) rather than from the view class itself.
- **Convention**: **Developers must write clear, concise docstrings on viewset action methods.** These docstrings are what users will see in the API documentation.

#### `get_operation_id()`

- **Purpose**: To generate clean, predictable, and code-generator-friendly operation IDs.
- **Convention**: The default behavior is modified to produce IDs like `projects_list`, `projects_create`, `projects_retrieve`. A special case for non-create `POST` actions (e.g., custom actions) uses a shorter format to avoid redundancy. This consistency is vital for generated API clients.

#### `get_override_parameters()`

- **Purpose**: To dynamically add query parameters based on the response serializer.
- **Use-Case**: Our `RestrictedSerializerMixin` allows users to request a subset of fields via the `field` query parameter (e.g., `?field=name&field=uuid`). This method introspects the response serializer, gets all its possible field names, and automatically generates the `OpenApiParameter` for `field` with a complete `enum` of available values. This provides excellent auto-complete and validation in tools like Swagger UI.

#### `_postprocess_serializer_schema(...)`

- **Purpose**: To modify a serializer's schema *after* it has been generated.
- **Use-Case**: Our serializers can have an `optional_fields` override. This method respects that override by removing those fields from the `required` array in the final schema. This is a clean way to tweak serializer requirements for the API without complex serializer inheritance.

---

## 3. Specialized Handlers: Extensions

Located in `openapi_extensions.py`, these classes provide a modular way to handle custom components.

- **`WaldurTokenScheme`, `WaldurSessionScheme`, `OIDCAuthenticationScheme`**: These extensions map our custom DRF authentication classes to standard OpenAPI security schemes. This is the correct way to document API authentication.
- **`GenericRelatedFieldExtension`**:
    - **Problem**: `drf-spectacular` doesn't know how to represent our custom `GenericRelatedField`.
    - **Solution**: This extension tells the generator to simply represent it as a `string` (which, in our case, is a URL). This avoids schema generation errors and provides a simple, accurate representation.
- **`OpenStackNestedSecurityGroupSerializerExtension`**:
    - **Problem**: A specific nested serializer is overly complex, and for the API schema, we only want to show a simplified version of it.
    - **Solution**: This extension bypasses introspection of the serializer entirely and provides a fixed, hardcoded schema (`{"type": "object", "properties": {"url": ...}}`). This is an excellent technique for simplifying complex nested objects in the API documentation.

---

## 4. Endpoint Discovery: `WaldurEndpointEnumerator`

Located in `openapi_generators.py`, this class controls which endpoints are included in the schema.

- **Purpose**: The default enumerator might include all possible HTTP methods that a view *could* support. Our `WaldurEndpointEnumerator` is smarter.
- **Mechanism**: It respects the `disabled_actions` list property on our viewsets. If an action (e.g., `'destroy'`) is in `disabled_actions`, the corresponding method (`DELETE`) will be excluded from the schema for that endpoint.
- **Convention**: To disable an API endpoint, add its action name to the `disabled_actions` list on the `ViewSet`. The API documentation will automatically update to reflect this.

---

## 5. Global Transformations: Schema Hooks

Located in `schema_hooks.py`, these functions perform powerful, sweeping modifications to the entire generated schema. They are the last step in the process.

- **Design Principle**: Use hooks for cross-cutting concerns that affect many endpoints, or for complex transformations that are difficult to achieve within the inspector.

### Key Hooks and Their Purpose

- **`refactor_pagination_parameters`**:
    - **Best Practice**: This hook implements the DRY (Don't Repeat Yourself) principle. It finds all instances of `page` and `page_size` parameters, moves their definition to the global `#/components/parameters/` section, and replaces the inline definitions with `$ref` pointers. This reduces schema size and improves consistency.
- **`add_result_count_header`**:
    - **Purpose**: To document that all our paginated list endpoints return the `x-result-count` header.
    - **Mechanism**: It identifies list endpoints (by checking if `operationId` ends in `_list`), defines a reusable header in `#/components/headers/`, and adds a reference to it in the `2xx` responses of those endpoints.
- **`make_fields_optional`**:
    - **Problem**: Endpoints using `RestrictedSerializerMixin` can return a variable subset of fields. How do we represent this?
    - **Solution**: This hook finds any operation that has a `field` query parameter. For those operations, it recursively traverses their response schemas and removes the `required` property from all objects. This correctly signals to API consumers that any field might be absent if not explicitly requested.
- **`transform_paginated_arrays`**:
    - **Purpose**: To simplify the schema structure for paginated responses.
    - **Mechanism**: `drf-spectacular` often creates named components like `PaginatedUserList`. This hook finds all such components, inlines their array definition wherever they are referenced, and then removes the original component definition. The result is a slightly more verbose but flatter and often easier-to-understand schema for the end-user.
- **`add_polymorphic_attributes_schema`**:
    - **This is the most advanced and powerful hook in our arsenal.**
    - **Problem**: The `attributes` field on the "Create Order" endpoint is polymorphic. Its structure depends entirely on the `offering_type` of the marketplace offering.
    - **Solution**: We use OpenAPI's `oneOf` keyword to represent this polymorphism.
    - **Mechanism**: The hook acts as a pre-processing step. It dynamically:
        1. Iterates through all registered marketplace plugins (`waldur_mastermind.marketplace.plugins`).
        2. For each plugin, it finds the serializer responsible for validating the `attributes` field.
        3. It uses a temporary `AutoSchema` instance to generate a schema for that specific serializer's fields.
        4. It adds this generated schema to `#/components/schemas/` with a unique name (e.g., `OpenStackInstanceCreateOrderAttributes`).
        5. Finally, it modifies the `OrderCreateRequest` schema to replace the `attributes` field with a `oneOf` that references all the dynamically generated schemas, plus a generic fallback.
    - **Architectural Significance**: This demonstrates how hooks can be used to generate schema fragments dynamically by introspecting parts of the application (in this case, the plugin system) that are outside the immediate scope of a DRF view.
- **Other Hooks**: `postprocess_drop_description`, `postprocess_fix_enum`, `remove_waldur_cookie_auth`, `adjust_request_body_content_types` are utility hooks for cleaning up and standardizing the final output.

---

## 6. Best Practices and Conventions

1. **Docstrings are the Source of Truth**: Write clear docstrings on viewset *action methods*. They become the official API descriptions.
2. **Use the Right Tool for the Job**:
    - **View-specific logic?** Use the `WaldurOpenApiInspector`.
    - **Reusable custom class?** Create an `Extension`.
    - **Global rule for filtering endpoints?** Modify the `WaldurEndpointEnumerator`.
    - **Schema-wide refactoring or complex polymorphism?** Write a `postprocessing_hook`.
3. **Leverage View Attributes for Metadata**: We use view attributes like `create_permissions` and `disabled_actions` to control schema generation. This co-locates API behavior and its documentation, making the code easier to maintain.
4. **Embrace Vendor Extensions (`x-`)**: For custom metadata that doesn't fit the OpenAPI standard (like our `x-permissions`), vendor extensions are the correct and standard way to include it.
5. **Strive for DRY Schemas**: Use hooks like `refactor_pagination_parameters` to create reusable components (`parameters`, `headers`, `schemas`). This keeps the schema clean and consistent.
6. **Handle Polymorphism with Hooks**: For complex conditional schemas (`oneOf`, `anyOf`), post-processing hooks are the most flexible and powerful tool available, as demonstrated by `add_polymorphic_attributes_schema`.
7. **Simplify for the Consumer**: Use extensions (`OpenStackNestedSecurityGroupSerializerExtension`) and hooks (`transform_paginated_arrays`) to simplify complex or deeply nested objects where the full detail is unnecessary for the API consumer. The goal is a schema that is not just accurate, but also usable.

## 7. The OpenAPI Schema in the Broader Workflow

The OpenAPI schema is not merely a documentation artifact; it is a critical, machine-readable contract that drives a significant portion of our development, testing, and release workflows. Our CI/CD pipelines are built around the schema as the single source of truth for the API's structure.

The entire automated process is defined in the GitLab CI configurations for the `waldur-mastermind` and `waldur-docs` repositories.

### 1. Automated Generation

The process begins in the `waldur-mastermind` pipeline in a job named `Generate OpenAPI schema`.

- **Triggers**: This job runs automatically in two scenarios:
    1. **On a schedule for the `develop` branch**: This ensures we always have an up-to-date schema reflecting the latest development state.
    2. **When a version tag is pushed** (e.g., `1.2.3`): This generates a stable, versioned schema for a specific release.
- **Output**: The job produces a versioned `waldur-openapi-schema.yaml` file, which is stored as a CI artifact. This artifact becomes the input for all subsequent steps.

### 2. Automated SDK and Tooling Generation

The generated schema artifact immediately triggers a series of parallel jobs, each responsible for generating a specific client SDK or tool. This "schema-first" approach ensures that our client libraries are always perfectly in sync with the API they are meant to consume.

- `Generate TypeScript SDK`: For Waldur HomePort and other web frontends.
- `Generate Python SDK`: For scripting, integrations, and internal tools.
- `Generate Go SDK`: For command-line tools and backend services.
- `Generate Terraform SDK`: Creates a Terraform provider for infrastructure-as-code management of Waldur resources.
- `Generate Ansible modules`: Creates Ansible collections for configuration management and automation.

### 3. Continuous Delivery of SDKs

For development builds (from the `develop` branch), the newly generated SDKs are automatically committed and pushed to the `main` or `develop` branch of their respective GitHub repositories. This provides a continuous delivery pipeline for our API clients, allowing developers to immediately access and test the latest API changes through their preferred language.

### 4. Release and Versioning Workflow

For tagged releases, the workflow is more extensive:

1. **API Diff Generation**: A job named `Generate OpenAPI schema diff` is triggered. It fetches the schema of the *previous* release from the `waldur-docs` repository and compares it against the newly generated schema using `oasdiff`. It produces a human-readable Markdown file (`openapi-diff.md`) detailing exactly what has changed (endpoints added, fields removed, etc.).
2. **Documentation Deployment**: The new versioned schema (`waldur-openapi-schema-1.2.3.yaml`) and the diff file are automatically committed to the `waldur-docs` repository. The documentation site is then rebuilt, archiving the new schema and making the API changes visible in the release notes.
3. **Changelog Integration**: The main `CHANGELOG.md` in the `waldur-docs` repository is automatically updated with links to the new schema file and the API diff page. This provides unparalleled clarity for integrators, showing them precisely what changed in a new release.
4. **SDK Release**: The tagged version of each SDK is released, often involving bumping the version in configuration files (`pyproject.toml`, `package.json`) and pushing a corresponding version tag to the SDK's repository.

This automated, schema-driven workflow provides immense benefits:

- **Consistency**: All clients and documentation are generated from the same source, eliminating discrepancies.
- **Speed**: Developers get up-to-date SDKs without manual intervention, accelerating the development cycle.
- **Reliability**: The risk of human error in writing client code or documenting changes is significantly reduced.
- **Clarity**: Release notes are precise and automatically generated, giving integrators clear instructions on what to expect.
