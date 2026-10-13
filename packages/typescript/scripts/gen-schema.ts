import { writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { zodToJsonSchema } from "zod-to-json-schema";
import { FrameworkFileSchema } from "../src/serve/index.js";

/** Regenerate framework.schema.json from the zod source of truth. */
const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const schema = {
  $schema: "http://json-schema.org/draft-07/schema#",
  title: "framework.json",
  ...(zodToJsonSchema(FrameworkFileSchema, { $refStrategy: "none" }) as Record<string, unknown>),
};
writeFileSync(join(root, "framework.schema.json"), `${JSON.stringify(schema, null, 2)}\n`);
console.log("wrote framework.schema.json");
