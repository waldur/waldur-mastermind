# Waldur Subagent Usage Guide

## Overview

Subagents are defined in `.claude/agents/` and are automatically available to Claude. They provide specialized expertise for specific tasks.

## How to Use Subagents

### Automatic Delegation
Claude will automatically use the appropriate subagent based on your request. You don't need to specify which one.

### Explicit Requests
You can also explicitly request a specific subagent:

- "Use the **code-reviewer** subagent to review my changes"
- "Use the **test-generator** subagent to create tests for this ViewSet"
- "Use the **implementation** subagent to build this feature"
- "Use the **performance-analyzer** subagent to optimize this endpoint"
- "Use the **docs-writer** subagent to document this API"

## Available Subagents

Check `.claude/agents/` directory for current subagents and their capabilities. Each subagent has:
- Specialized knowledge for its domain
- Access to specific tools
- Optimized prompts for its tasks

## Workflow Examples

### Feature Development
1. "Implement user notification system" → implementation subagent
2. "Create tests for notifications" → test-generator subagent
3. "Review the notification code" → code-reviewer subagent
4. "Document the notification API" → docs-writer subagent

### Performance Issues
1. "Why is /api/projects/ slow?" → performance-analyzer subagent
2. "Fix the query optimization" → implementation subagent
3. "Test the performance improvements" → test-generator subagent

### Code Quality
1. "Review this pull request" → code-reviewer subagent
2. "Fix any issues found" → implementation subagent

## Best Practices

- Be specific about what you want analyzed or implemented
- Mention specific files or components when relevant
- Chain requests for complex workflows
- Let Claude choose the right subagent when unsure
