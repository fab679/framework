#!/usr/bin/env node
import { resolve } from "node:path";
import { loadFrameworkConfig, serveFramework } from "./index.js";
import { logError } from "../observability/index.js";

const HELP = `framework — launch Framework agents as local servers.

Usage:
  framework-serve [command] [options]

Commands:
  dev [-c framework.json] [--host H] [--port N]   local dev server (default)
  serve|up [-c framework.json]                     serve agents
  validate [-c framework.json]                     validate config and print agents
  --help                                           this help

framework.json keys: server{host,basePort}, envFile, env{}, checkpointer{dir},
http{corsOrigins}, agents[{name, model, file?, envFile?, skills?, filesystem?,
todos?, maxSteps?, sessionDir?, port?}]. Agent files + relative paths resolve
against their own file's directory; inline overrides against framework.json.
`;

function arg(flag: string, short?: string): string | undefined {
  const i = process.argv.findIndex((a) => a === flag || (short && a === short));
  return i >= 0 ? process.argv[i + 1] : undefined;
}

const command = process.argv[2] ?? "dev";
if (command === "--help" || command === "-h" || process.argv.includes("--help")) {
  console.log(HELP);
  process.exit(0);
}

if (!["dev", "serve", "up", "validate"].includes(command)) {
  console.error(`Unknown command: ${command}\n${HELP}`);
  process.exit(1);
}

const configPath = resolve(arg("-c", "--config") ?? "framework.json");
let file;
try {
  file = loadFrameworkConfig(configPath);
} catch (err) {
  logError("[framework]", err);
  process.exit(1);
}

if (command === "validate") {
  console.log(`OK: ${configPath}`);
  for (const a of file.agents) {
    console.log(`- ${a.name} [${a.model}]${a.port ? ` :${a.port}` : ""}${a.envFile ? ` (env: ${a.envFile})` : ""}`);
  }
  process.exit(0);
}

const host = arg("--host") ?? file.server.host;
const portArg = arg("--port");
if (portArg) {
  const port = Number.parseInt(portArg, 10);
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    console.error(`Invalid --port: ${portArg}`);
    process.exit(1);
  }
  file.server.basePort = port;
}

const { agents, stop } = await serveFramework({ ...file, server: { ...file.server, host } });
for (const a of agents) {
  console.log(`[framework] ${a.name} -> ${a.url} (card: ${a.cardUrl})`);
}
console.log(`[framework] ${command === "dev" ? "dev server" : "serving"} ${agents.length} agent(s). Ctrl-C to stop.`);

const shutdown = async (): Promise<void> => {
  await stop();
  process.exit(0);
};
process.on("SIGINT", () => void shutdown());
process.on("SIGTERM", () => void shutdown());
