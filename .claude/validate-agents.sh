#!/bin/bash
# Basic validation script for Claude Code subagents

echo "Validating Claude Code subagents..."

AGENT_DIR=".claude/agents"
ERRORS=0

if [ ! -d "$AGENT_DIR" ]; then
    echo "❌ No agents directory found: $AGENT_DIR"
    exit 1
fi

for agent_file in "$AGENT_DIR"/*.md; do
    if [ ! -f "$agent_file" ]; then
        continue
    fi

    filename=$(basename "$agent_file")
    echo "Checking $filename..."

    # Check YAML frontmatter exists
    if ! grep -q "^---$" "$agent_file"; then
        echo "  ❌ Missing YAML frontmatter"
        ((ERRORS++))
        continue
    fi

    # Extract and validate YAML
    yaml_content=$(awk '/^---$/{flag=1;next}/^---$/{flag=0}flag' "$agent_file")

    # Check required fields
    if ! echo "$yaml_content" | grep -q "^name:"; then
        echo "  ❌ Missing required 'name' field"
        ((ERRORS++))
    fi

    if ! echo "$yaml_content" | grep -q "^description:"; then
        echo "  ❌ Missing required 'description' field"
        ((ERRORS++))
    fi

    # Check name format (lowercase, hyphens only)
    agent_name=$(echo "$yaml_content" | grep "^name:" | cut -d: -f2- | xargs)
    if [[ ! "$agent_name" =~ ^[a-z-]+$ ]]; then
        echo "  ❌ Invalid name format: '$agent_name' (use lowercase letters and hyphens only)"
        ((ERRORS++))
    fi

    # Check if file exists and has content after frontmatter
    content_lines=$(awk '/^---$/{if(count==1){flag=1} count++; next} flag' "$agent_file" | wc -l)
    if [ "$content_lines" -lt 5 ]; then
        echo "  ⚠️  Agent prompt seems very short (less than 5 lines)"
    fi

    echo "  ✅ $filename validated"
done

echo ""
if [ $ERRORS -eq 0 ]; then
    echo "✅ All subagents are valid!"
    exit 0
else
    echo "❌ Found $ERRORS validation errors"
    exit 1
fi
