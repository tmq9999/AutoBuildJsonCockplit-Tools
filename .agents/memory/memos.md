# MemOS Integration Memory

## Overview
- **Repository**: `MemTensor/MemOS` (https://github.com/MemTensor/MemOS)
- **Role**: Memory Operating System (MemOS 2.0 Stardust) providing long-term agent memory, memory cubes, and skill crystallization.
- **Components**:
  - Workspace Plugin: `.agents/plugins/memos/`
  - Global Plugin: `~/.gemini/config/plugins/memos/`
  - Workspace Skill: `.agents/skills/memos-memory-guide/`
  - Rule: `.agents/rules/memos.md`
  - Architecture: Multi-Cube Knowledge Base, SQLite / Neo4j / Qdrant backends, FastMCP Server support.
