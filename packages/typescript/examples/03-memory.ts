import { Agent, InMemoryMemory, MockModelProvider, serializeMessages, deserializeMessages } from "../src/index.js";

const memory = new InMemoryMemory();
const agent = new Agent({ model: new MockModelProvider(), memory });

await agent.run("My name is Ada.");
await agent.run("Do you remember my name? (mock: check memory below)");

// Persist + resume: history is plain JSON with validated round-trip.
const snapshot = JSON.stringify(serializeMessages(await memory.load()));
const resumed = new InMemoryMemory();
await resumed.save(deserializeMessages(JSON.parse(snapshot)));
console.log("memory:", await memory.load());
console.log("resumed ok:", (await resumed.load()).length === (await memory.load()).length);
