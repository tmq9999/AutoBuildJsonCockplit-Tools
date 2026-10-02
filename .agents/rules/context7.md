# Context7 Documentation Lookup Protocol

> Package: https://github.com/upstash/context7
> Tooling: `ctx7` CLI & Context7 MCP Server

---

## When to Use Context7

Always use Context7 to retrieve up-to-date documentation and code examples whenever you or the user work with a specific library, framework, SDK, API, or cloud service (e.g., FastAPI, SQLAlchemy, Alembic, Next.js, React, Tailwind, Supabase, etc.).

**Use Context7 for:**
- API syntax, function signatures, and method options
- Configuration parameters and setup guides
- Version migration and breaking changes
- Library-specific debugging and error resolution

**Do NOT use Context7 for:**
- Writing general logic from scratch or general computer science concepts
- Debugging custom business logic unrelated to external libraries

---

## How to Query Docs

### 1. Via CLI (`ctx7`)
```bash
# Step 1: Resolve Library ID
ctx7 library <libraryName> "<what to look up>"

# Step 2: Fetch Documentation
ctx7 docs <libraryId> "<specific query>"
```
*(Example: `ctx7 library "FastAPI" "how to declare response model"` → `ctx7 docs "/tiangolo/fastapi" "response model examples"`)*

### 2. Via MCP Tools (if active)
- `resolve-library-id`: resolves library name to Context7 ID
- `query-docs`: retrieves targeted documentation snippets
