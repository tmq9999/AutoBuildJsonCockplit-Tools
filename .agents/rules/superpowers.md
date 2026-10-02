# Superpowers Core Protocol - Always Active

> Repository: https://github.com/obra/superpowers (Core Skills Framework)
> Status: MANDATORY & ALWAYS ACTIVE on every turn and session.

---

## ⚡ Core Directive: Mandatory Skill Invocation

<EXTREMELY-IMPORTANT>
If you think there is even a 1% chance a skill might apply to what you are doing, you ABSOLUTELY MUST invoke the skill.

IF A SKILL APPLIES TO YOUR TASK, YOU DO NOT HAVE A CHOICE. YOU MUST USE IT.

This is not negotiable. You cannot rationalize your way out of this.
</EXTREMELY-IMPORTANT>

### The Rule

1. **Invoke relevant or requested skills BEFORE any response or action** — including clarifying questions, exploring the codebase, or checking files. If it turns out wrong for the situation, you don't have to use it.
2. **Before entering plan mode:** If you haven't already brainstormed, invoke the `brainstorming` skill first.
3. **Skill announcement:** Announce `📚 Using skill: @[skill-name]...` and follow the skill instructions exactly. If it has a checklist, create a todo per item.
4. **Discipline over speed:** True Red-Green-Refactor TDD, rigorous root-cause debugging, subagent task isolation, and verification before completion.

---

## 🎯 Skill Routing & Trigger Rules

| Scenario / Intent | Mandatory Skill Flow |
| :--- | :--- |
| **New Feature / Change / App** ("build", "tạo", "thêm tính năng", "làm chức năng") | `brainstorming` → `writing-plans` → `test-driven-development` / `subagent-driven-development` |
| **Bug / Error / Investigation** ("fix", "lỗi", "sửa", "không chạy được", "failed") | `systematic-debugging` (4-phase root-cause tracing before touching code) → reproduce with test → fix |
| **Multi-step Execution** | `executing-plans` (track tasks with markdown task artifact) |
| **Finishing / Delivery** | `verification-before-completion` (prove code works by running it) → `finishing-a-development-branch` |

---

## 🚫 Red Flags (Rationalizations to STOP Immediately)

These thoughts mean STOP — you are rationalizing:

| Thought | Reality |
| :--- | :--- |
| "This is just a simple question" | Questions are tasks. Check for skills. |
| "I need more context first" | Skill check comes BEFORE clarifying questions. |
| "Let me explore the codebase first" | Skills tell you HOW to explore. Check first. |
| "I can check git/files quickly" | Files lack conversation context. Check for skills. |
| "Let me gather information first" | Skills tell you HOW to gather information. |
| "This doesn't need a formal skill" | If a skill exists, use it. |
| "I remember this skill" | Skills evolve. Read current version. |
| "This doesn't count as a task" | Action = task. Check for skills. |
| "The skill is overkill" | Simple things become complex. Use it. |
| "I'll just do this one thing first" | Check BEFORE doing anything. |
| "This feels productive" | Undisciplined action wastes time. Skills prevent this. |
| "I know what that means" | Knowing the concept ≠ using the skill. Invoke it. |

---

## 🛠️ Tool & Platform Adaptation

- **Task tracking:** Use a task artifact (`write_to_file` in brain artifact directory or markdown checklist) to track each step. Mark `- [x]` as completed.
- **Subagents:** Use `invoke_subagent` (or specialized subagents) for parallel independent tasks.
- **Verification:** Always execute commands or tests to verify changes before marking complete (`verification-before-completion`).
