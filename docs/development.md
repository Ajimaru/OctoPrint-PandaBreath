# Development

## Local preview

Run a local docs server:

```bash
mkdocs serve
```

Build static files:

```bash
mkdocs build --strict
```

## Workflow notes

The docs workflow runs on:

- push to main when docs files changed
- pull requests to dev when docs files changed
- manual workflow dispatch
