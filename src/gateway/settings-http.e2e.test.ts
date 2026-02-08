import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { getFreePort, installGatewayTestHooks } from "./test-helpers.js";

installGatewayTestHooks({ scope: "suite" });

let server: Awaited<ReturnType<typeof startServer>>;
let port: number;

beforeAll(async () => {
  port = await getFreePort();
  server = await startServer(port);
}, 60_000);

afterAll(async () => {
  await server.close({ reason: "settings http suite done" });
});

async function startServer(port: number) {
  const { startGatewayServer } = await import("./server.js");
  return await startGatewayServer(port, {
    host: "127.0.0.1",
    auth: { mode: "token", token: "secret" },
    controlUiEnabled: false,
    openAiChatCompletionsEnabled: false,
    openResponsesEnabled: false,
  });
}

describe("GET /v1/settings (e2e)", () => {
  it("enforces auth and method", async () => {
    {
      const res = await fetch(`http://127.0.0.1:${port}/v1/settings`, { method: "POST" });
      expect(res.status).toBe(405);
      await res.text();
    }

    {
      const res = await fetch(`http://127.0.0.1:${port}/v1/settings`, { method: "GET" });
      expect(res.status).toBe(401);
      await res.text();
    }
  });

  it("returns capture settings (env-backed)", async () => {
    const prevMode = process.env.OPENCLAW_MEMORY_CAPTURE_MODE;
    const prevDebug = process.env.OPENCLAW_MEMORY_CAPTURE_DEBUG;
    try {
      process.env.OPENCLAW_MEMORY_CAPTURE_MODE = "off";
      process.env.OPENCLAW_MEMORY_CAPTURE_DEBUG = "1";

      const res = await fetch(`http://127.0.0.1:${port}/v1/settings`, {
        method: "GET",
        headers: { authorization: "Bearer secret" },
      });
      expect(res.status).toBe(200);
      const body = (await res.json()) as Record<string, unknown>;
      expect(body.memory_capture_mode).toBe("off");
      expect(body.memory_capture_debug).toBe(true);
    } finally {
      if (prevMode === undefined) {
        delete process.env.OPENCLAW_MEMORY_CAPTURE_MODE;
      } else {
        process.env.OPENCLAW_MEMORY_CAPTURE_MODE = prevMode;
      }
      if (prevDebug === undefined) {
        delete process.env.OPENCLAW_MEMORY_CAPTURE_DEBUG;
      } else {
        process.env.OPENCLAW_MEMORY_CAPTURE_DEBUG = prevDebug;
      }
    }
  });
});
