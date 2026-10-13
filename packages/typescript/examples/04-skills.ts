import { parseSkillFile, SkillRegistry } from "../src/index.js";

const raw = `---
name: geo-coder
description: Geocode place names to coordinates
version: 0.1.0
---
Use web_search to resolve a place, then return lat/lon.
Prefer authoritative sources; cite provenance for every fact.
`;

const skill = parseSkillFile(raw, "skills/geo-coder/SKILL.md");
const registry = new SkillRegistry();
registry.register({ ...skill, tools: [] });

console.log("skills:", registry.list().map((s) => `${s.name}@${s.version}`));
console.log("body:", skill.body.slice(0, 80));
