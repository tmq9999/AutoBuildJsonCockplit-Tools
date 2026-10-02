# MemOS (Memory Operating System) Protocol

> Repository: https://github.com/MemTensor/MemOS
> Core: MemOS 2.0 (Stardust)

---

## When to Use MemOS Memory Capabilities

Activate `memos-memory-guide` whenever:
1. The user refers to past conversations, previous decisions, or historical preferences across sessions.
2. The user asks to store, recall, search, or update long-term knowledge/context.
3. Multi-agent workflows need shared vs. private memory isolation ("Memory Cubes").
4. Inspecting or managing past task summaries and crystallized skills.

## Core Memory Tools & API
- `memory_search`: Search long-term conversation history by natural language.
- `memory_get`: Retrieve full original memory chunk.
- `memory_write_public`: Create persistent shared memory for agents in the workspace.
- `memory_share` / `memory_unshare`: Manage sharing scopes.
- `task_summary`: Retrieve structured summary of completed past tasks.
- `skill_get` / `skill_search`: Re-use crystallized operational guides from past successful runs.
- `memory_viewer`: Access web dashboard for memory inspection.
