# Taste Skill Integration Memory

## Overview
- **Repository**: `Leonxlnx/taste-skill` (https://github.com/Leonxlnx/taste-skill)
- **Role**: Anti-slop frontend design framework for landing pages, portfolios, brand kits, and redesigns.
- **Locations**:
  - Global Plugin: `~/.gemini/config/plugins/taste-skill/`
  - Workspace Plugin: `.agents/plugins/taste-skill/`
  - Workspace Skills: `.agents/skills/` (`taste-skill`, `redesign-skill`, `soft-skill`, `minimalist-skill`, `brutalist-skill`, `output-skill`, `image-to-code-skill`, `imagegen-frontend-web`, `imagegen-frontend-mobile`, `brandkit`, `stitch-skill`, `gpt-tasteskill`)
- **Key Rules**:
  - State a 1-line "Design Read" before generating frontend code.
  - Tune the 3 dials: `DESIGN_VARIANCE`, `MOTION_INTENSITY`, `VISUAL_DENSITY`.
  - Ban AI-slop defaults (no generic purple gradients, no 3 identical cards, no dark mesh hero).
  - Scope: Landing pages, portfolios, marketing sites, redesigns (NOT complex dashboards or data tables).
