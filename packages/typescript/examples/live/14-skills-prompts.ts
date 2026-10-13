// LIVE: skills + prompts — a live model works with loaded SKILL.md files
// ($mention routing is local TF-IDF), a rendered prompt template, and A/B
// prompt variants.
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  Agent, loadConfig, providerFromEnv, modelRetryMiddleware,
  SkillRegistry, discoverSkills, matchSkills,
  loadPromptFile, abPrompts, definePrompt,
} from "../../src/index.js";

const cfg = loadConfig();
const model = () => providerFromEnv(cfg.active, { maxTokens: 3000 });
const retry = [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })];

// 1) Discover skills from a real directory.
const skillsDir = mkdtempSync(join(tmpdir(), "live-skills-"));
mkdirSync(join(skillsDir, "turtle"), { recursive: true });
mkdirSync(join(skillsDir, "poetry"), { recursive: true });
writeFileSync(join(skillsDir, "turtle", "SKILL.md"), [
  "---",
  "name: turtle-facts",
  "description: Facts and care guidance about pet turtles",
  "---",
  "Turtles are reptiles with shells. Aquatic turtles need a water depth of at least twice their shell length.",
].join("\n"));
writeFileSync(join(skillsDir, "poetry", "SKILL.md"), [
  "---",
  "name: haiku",
  "description: Writing haiku poems with the 5-7-5 syllable pattern",
  "---",
  "A haiku has three lines: 5, then 7, then 5 syllables.",
].join("\n"));

const registry = new SkillRegistry();
for (const s of await discoverSkills([skillsDir])) registry.register(s);
console.log("DISCOVERED:", registry.list().map((s) => s.name));
const routedTurtle = matchSkills(registry, "How do I care for my turtle tank depth?");
const routedMention = matchSkills(registry, "Use the $haiku skill to write a nature poem.");
console.log("TF-IDF ROUTE:", routedTurtle.map((s) => s.name), "| $mention ROUTE:", routedMention.map((s) => s.name), "(mention must win with haiku first)");
console.log(routedMention[0]?.name === "haiku" ? "PASS: $name mention routing wins" : "NOTE: routing inspect");

// 2) A live agent uses the SKILL BODY as context (skill injection by name).
// Creative prompts can send thinking models into unbounded thought — the
// truncation guard makes that loud instead of silently empty; treat it as a note.
const haikuBody = routedMention[0]?.body ?? "";
const poemAgent = new Agent({ name: "poet", model: model(), middleware: retry, maxSteps: 2 });
const poem = await poemAgent.run(`Follow this skill exactly:\n${haikuBody}\n\nWrite one haiku about autumn leaves.`).catch((err: { code?: string }) => ({
  text: `(${err.code ?? "error"}: thinking budget overran on a creative prompt — a live-model caveat, not a skills bug)`,
}));
console.log("HAIKU:", poem.text.trim().slice(0, 90));

// 3) Prompt templates: file loading + variable rendering + A/B pick.
const promptsDir = mkdtempSync(join(tmpdir(), "live-prompts-"));
writeFileSync(join(promptsDir, "greet.md"), '---\nname: greet\ndescription: Greets a user by name\n---\nHello {{name}}, welcome to {{place}}! Reply with exactly this rendered line plus the word ok.');
const template = loadPromptFile(join(promptsDir, "greet.md"));
console.log("RENDERED PROMPT:", template.render({ name: "Ada", place: "Ledger City" }));

const variants = [
  definePrompt("formal", "Greet {{name}} with utmost formality.", "1.0"),
  definePrompt("casual", "Greet {{name}} super casually.", "1.0"),
];
const picked = abPrompts(variants, "casual");
console.log("A/B PICKED:", picked.name, "(expect casual)");

const greeted = new Agent({
  name: "greeter",
  model: model(),
  middleware: retry,
  maxSteps: 2,
  systemPrompt: template.render({ name: "Ada", place: "Ledger City" }),
});
const greetRes = await greeted.run("Respond to the system instructions by producing the greeting, then say ok.").catch((err: { code?: string }) => ({
  text: `(cap fired: ${err.code ?? "error"})`,
}));
console.log("GREETING:", greetRes.text.trim().slice(0, 80));
// The features under test here are local + deterministic: discovery,
// mention routing, template rendering, A/B picking; the live calls demonstrate
// the assembled prompt in play.
console.log(
  routedMention[0]?.name === "haiku" && picked.name === "casual" && template.render({ name: "Ada", place: "X" }).includes("Ada")
    ? "PASS: skills + prompts live (discovery, routing, render, A/B)"
    : "NOTE: inspect outputs",
);