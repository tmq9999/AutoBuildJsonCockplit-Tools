# Superpowers Integration Memory

## Decision & Configuration
- **Package**: `obra/superpowers` (https://github.com/obra/superpowers)
- **Status**: Installed and permanently active.
- **Locations**:
  - Global Plugin: `~/.gemini/config/plugins/superpowers/`
  - Workspace Plugin: `.agents/plugins/superpowers/`
  - Workspace Skills: `.agents/skills/` (synced with superpowers skills: `using-superpowers`, `subagent-driven-development`, `verification-before-completion`, `test-driven-development`, `systematic-debugging`, `brainstorming`, `writing-plans`, etc.)
  - Workspace Rules: `.agents/rules/superpowers.md` (Always-on Tier 0 rule)
  - Global Rules: `~/.gemini/config/rules/superpowers.md`
  - Instructions: `GEMINI.md` (Workspace root & `~/.gemini/GEMINI.md`)
- **Behavioral Enforcement**:
  - The agent must invoke relevant skills BEFORE any code modifications or answers.
  - Socratic brainstorming before plan mode.
  - TDD and systematic verification before closing tasks.
