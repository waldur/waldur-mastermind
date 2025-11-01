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

## Multi-Agent Coordination Patterns

### Sequential Workflows
For complex tasks requiring multiple expertise areas:

```
1. implementation → "Create user notification system"
2. test-generator → "Generate comprehensive tests for notifications"
3. code-reviewer → "Review notification implementation for quality"
4. docs-writer → "Document the notification API endpoints"
```

### Parallel Analysis
For comprehensive system analysis:

```
Parallel execution:
- performance-analyzer → "Analyze /api/projects/ endpoint performance"
- code-reviewer → "Review recent changes to project serializers"
- docs-writer → "Update API documentation for project endpoints"
```

### Validation Chains
For ensuring quality at each step:

```
1. implementation → Creates feature + requests test generation
2. test-generator → Creates tests + requests code review
3. code-reviewer → Reviews code + tests + requests performance analysis
4. performance-analyzer → Validates performance + requests documentation
5. docs-writer → Documents feature
```

### Error Recovery Workflows
When issues are found:

```
1. code-reviewer → Identifies issues in JSON format
2. implementation → Fixes specific issues from review
3. test-generator → Updates tests for fixes
4. code-reviewer → Re-reviews fixes
```

### Cross-Agent Communication
Agents should reference each other's outputs:

- **implementation** creates response with `test_files: []` for test-generator
- **test-generator** responds with `missing_coverage: []` for implementation
- **code-reviewer** provides `next_steps: []` for other agents
- **performance-analyzer** identifies `optimization_opportunities: []`

## Best Practices

- Be specific about what you want analyzed or implemented
- Mention specific files or components when relevant
- Chain requests for complex workflows using response templates
- Use JSON outputs for coordination between agents
- Let Claude choose the right subagent when unsure
- Request validation after major changes
