#!/usr/bin/env python3
"""Generate LLM-friendly documentation from an OpenAPI schema.

Produces three files in the output directory:
  - llms.txt       — compact index for LLM orientation
  - llms-full.txt  — flat reference of every endpoint with parameters
  - api-map.md     — endpoints grouped by domain/tag

Usage:
    python scripts/generate_llm_docs.py <openapi-schema.yaml> <output-dir>
"""

import argparse
import os
import sys
from collections import defaultdict

import yaml

HTTP_METHOD_ORDER = ["get", "head", "post", "put", "patch", "delete"]


def load_schema(path):
    with open(path) as f:
        return yaml.safe_load(f)


def collect_operations(spec):
    """Return a list of operation dicts grouped by tag."""
    tag_ops = defaultdict(list)

    for path, methods in sorted(spec.get("paths", {}).items()):
        for method in HTTP_METHOD_ORDER:
            op = methods.get(method)
            if not isinstance(op, dict):
                continue

            operation_id = op.get("operationId", "")
            tags = op.get("tags", ["untagged"])
            summary = op.get("summary", "").strip()
            description = op.get("description", "").strip()
            # Use summary, fall back to first sentence of description
            short_desc = summary
            if not short_desc and description:
                short_desc = description.split(".")[0].strip()

            params = []
            for p in op.get("parameters", []):
                name = p.get("name")
                if not name:
                    continue
                params.append(
                    {
                        "name": name,
                        "in": p.get("in", ""),
                        "required": p.get("required", False),
                        "description": p.get("description", "").strip(),
                    }
                )

            has_body = "requestBody" in op

            entry = {
                "operation_id": operation_id,
                "method": method.upper(),
                "path": path,
                "summary": short_desc,
                "description": description,
                "params": params,
                "has_body": has_body,
            }

            for tag in tags:
                tag_ops[tag].append(entry)

    return tag_ops


def format_param_list(params, has_body):
    """Return a compact parameter summary string."""
    path_params = [p for p in params if p["in"] == "path"]
    query_params = [p for p in params if p["in"] == "query"]

    parts = []
    if path_params:
        names = ", ".join(p["name"] for p in path_params)
        parts.append(f"path: {names}")
    if query_params:
        count = len(query_params)
        parts.append(f"{count} query param{'s' if count != 1 else ''}")
    if has_body:
        parts.append("request body")

    return " | ".join(parts) if parts else "no params"


def generate_llms_txt(tag_ops, spec_info, output_dir):
    """Generate llms.txt — a compact index file."""
    title = spec_info.get("title", "API Client SDK")
    version = spec_info.get("version", "")

    lines = [
        f"# {title}",
        "",
        f"> Auto-generated Python SDK for the Waldur API{f' (version {version})' if version else ''}.",
        "> This SDK is generated from an OpenAPI specification using openapi-python-client.",
        "",
        "## Quick Start",
        "",
        "```python",
        "from waldur_api_client import AuthenticatedClient",
        "",
        'client = AuthenticatedClient(base_url="https://your-waldur-instance.com/", token="your-api-token")',
        "",
        "# Example: list customers",
        "from waldur_api_client.api.customers import customers_list",
        "customers = customers_list.sync(client=client)",
        "",
        "# Each endpoint module provides: sync(), sync_detailed(), sync_all(),",
        "#   asyncio(), asyncio_detailed(), asyncio_all()",
        "```",
        "",
        "## Documentation Files",
        "",
        "- [api-map.md](api-map.md) — Endpoints organized by domain with descriptions",
        "- [llms-full.txt](llms-full.txt) — Complete flat reference of every endpoint with parameters",
        "",
        "## SDK Structure",
        "",
        "```",
        "waldur_api_client/",
        "  client.py              # AuthenticatedClient and Client classes",
        "  api/<domain>/          # One directory per API domain (tag)",
        "    <operation_id>.py    # One file per endpoint",
        "  models/                # Request/response data models",
        "```",
        "",
        "## API Domains",
        "",
    ]

    for tag in sorted(tag_ops.keys()):
        ops = tag_ops[tag]
        count = len(ops)
        lines.append(f"- **{tag}** ({count} endpoint{'s' if count != 1 else ''})")

    lines.append("")

    with open(os.path.join(output_dir, "llms.txt"), "w") as f:
        f.write("\n".join(lines))


def generate_api_map(tag_ops, spec_info, output_dir):
    """Generate api-map.md — endpoints grouped by domain with descriptions."""
    title = spec_info.get("title", "API Client SDK")

    lines = [
        f"# {title} — API Map",
        "",
        "Endpoints organized by domain. Each endpoint is available as:",
        "`waldur_api_client.api.<domain>.<operation_id>.sync(client=client, ...)`",
        "",
        "---",
        "",
    ]

    for tag in sorted(tag_ops.keys()):
        ops = tag_ops[tag]
        module_name = tag.replace("-", "_")
        lines.append(f"## {tag}")
        lines.append(f"Module: `waldur_api_client.api.{module_name}`")
        lines.append("")

        for op in ops:
            summary_part = f" — {op['summary']}" if op["summary"] else ""
            params_part = format_param_list(op["params"], op["has_body"])
            lines.append(
                f"- `{op['operation_id']}` "
                f"{op['method']} `{op['path']}`{summary_part} "
                f"({params_part})"
            )

        lines.append("")

    with open(os.path.join(output_dir, "api-map.md"), "w") as f:
        f.write("\n".join(lines))


def generate_llms_full(tag_ops, spec_info, output_dir):
    """Generate llms-full.txt — detailed flat reference of every endpoint."""
    title = spec_info.get("title", "API Client SDK")
    version = spec_info.get("version", "")

    lines = [
        f"# {title} — Full Endpoint Reference",
        f"# Version: {version}" if version else "",
        "#",
        "# Import pattern:",
        "#   from waldur_api_client.api.<domain> import <operation_id>",
        "#   result = <operation_id>.sync(client=client, ...)",
        "#",
        "# Each endpoint provides: sync(), sync_detailed(), sync_all(),",
        "#   asyncio(), asyncio_detailed(), asyncio_all()",
        "",
    ]

    for tag in sorted(tag_ops.keys()):
        ops = tag_ops[tag]
        module_name = tag.replace("-", "_")
        lines.append(f"{'=' * 60}")
        lines.append(f"## {tag}  (module: waldur_api_client.api.{module_name})")
        lines.append(f"{'=' * 60}")
        lines.append("")

        for op in ops:
            lines.append(f"### {op['operation_id']}")
            lines.append(f"{op['method']} {op['path']}")
            if op["summary"]:
                lines.append(f"Summary: {op['summary']}")
            if op["description"] and op["description"] != op["summary"]:
                # Truncate very long descriptions
                desc = op["description"]
                if len(desc) > 300:
                    desc = desc[:297] + "..."
                lines.append(f"Description: {desc}")

            path_params = [p for p in op["params"] if p["in"] == "path"]
            query_params = [p for p in op["params"] if p["in"] == "query"]

            if path_params:
                lines.append("Path parameters:")
                for p in path_params:
                    desc = f" — {p['description']}" if p["description"] else ""
                    lines.append(f"  - {p['name']}{desc}")

            if query_params:
                lines.append(f"Query parameters ({len(query_params)}):")
                for p in query_params:
                    req = " (required)" if p["required"] else ""
                    desc = f" — {p['description']}" if p["description"] else ""
                    lines.append(f"  - {p['name']}{req}{desc}")

            if op["has_body"]:
                lines.append("Request body: yes")

            lines.append("")

    with open(os.path.join(output_dir, "llms-full.txt"), "w") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(
        description="Generate LLM-friendly docs from an OpenAPI schema."
    )
    parser.add_argument("schema", help="Path to the OpenAPI YAML schema file")
    parser.add_argument("output_dir", help="Output directory for generated files")
    args = parser.parse_args()

    if not os.path.isfile(args.schema):
        print(f"Error: schema file not found: {args.schema}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)

    spec = load_schema(args.schema)
    spec_info = spec.get("info", {})
    tag_ops = collect_operations(spec)

    total_ops = sum(len(ops) for ops in tag_ops.values())
    print(f"Parsed {len(tag_ops)} tags, {total_ops} operations")

    generate_llms_txt(tag_ops, spec_info, args.output_dir)
    generate_api_map(tag_ops, spec_info, args.output_dir)
    generate_llms_full(tag_ops, spec_info, args.output_dir)

    for name in ("llms.txt", "api-map.md", "llms-full.txt"):
        path = os.path.join(args.output_dir, name)
        size = os.path.getsize(path)
        lines_count = sum(1 for _ in open(path))
        print(f"  {name}: {lines_count} lines, {size:,} bytes")

    print("Done.")


if __name__ == "__main__":
    main()
