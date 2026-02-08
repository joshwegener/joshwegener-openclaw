import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let nextSelect: unknown = "save_all";
let nextMultiselect: unknown = [];

vi.mock("@clack/prompts", () => ({
  isCancel: (value: unknown) => typeof value === "symbol",
  select: vi.fn(async () => nextSelect),
  multiselect: vi.fn(async () => nextMultiselect),
}));

vi.mock("./agent.js", () => ({
  agentCommand: vi.fn(),
}));

let savedArgs: { workspaceDir: string; proposals: Array<{ id: string; text: string }> } | null =
  null;

vi.mock("../postflight/save.js", () => ({
  appendPostflightProposalsToDailyMemory: vi.fn(async (params: any) => {
    savedArgs = { workspaceDir: params.workspaceDir, proposals: params.proposals };
    return { filePath: path.join(params.workspaceDir, "memory", "2026-02-08.md"), appended: 999 };
  }),
}));

import { multiselect, select } from "@clack/prompts";
import type { OpenClawConfig } from "../config/config.js";
import type { RuntimeEnv } from "../runtime.js";
import * as configModule from "../config/config.js";
import { agentCliCommand } from "./agent-via-gateway.js";
import { agentCommand } from "./agent.js";

const runtime: RuntimeEnv = {
  log: vi.fn(),
  error: vi.fn(),
  exit: vi.fn(),
};

const configSpy = vi.spyOn(configModule, "loadConfig");

function mockConfig(storePath: string, workspaceDir: string, overrides?: Partial<OpenClawConfig>) {
  configSpy.mockReturnValue({
    agents: {
      defaults: {
        timeoutSeconds: 600,
        workspace: workspaceDir,
        ...overrides?.agents?.defaults,
      },
      ...overrides?.agents,
    },
    session: {
      store: storePath,
      mainKey: "main",
      ...overrides?.session,
    },
    gateway: overrides?.gateway,
  });
}

const prevEnv = { ...process.env };
const prevFetch = globalThis.fetch;

beforeEach(() => {
  vi.clearAllMocks();
  savedArgs = null;
  nextSelect = "save_all";
  nextMultiselect = [];
  process.env = { ...prevEnv };
  delete process.env.OPENCLAW_MEMORY_CAPTURE_MODE;
  delete process.env.OPENCLAW_MEMORY_CAPTURE_DEBUG;
  delete process.env.OPENCLAW_MEMORY_CAPTURE_AUTO_SAVE;
  globalThis.fetch = vi.fn(async () => ({ ok: false }) as any);
});

afterEach(() => {
  process.env = { ...prevEnv };
  globalThis.fetch = prevFetch;
});

describe("postflight capture UI", () => {
  it("defaults to Save all in non-interactive runs", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-postflight-ui-"));
    const store = path.join(dir, "sessions.json");
    const workspace = path.join(dir, "workspace");
    mockConfig(store, workspace);

    vi.mocked(agentCommand).mockImplementationOnce(async (_opts, rt) => {
      rt.log?.("agent output");
      return {
        payloads: [{ text: "agent output" }],
        meta: {
          postflightCapture: {
            kind: "memory_writes",
            rawJson: '{"postflight":{"type":"memory_writes"}}',
            proposals: [
              { id: "p1", text: "First memory" },
              { id: "p2", text: "Second memory" },
            ],
          },
        },
      };
    });

    try {
      await agentCliCommand({ message: "hi", to: "+1555", local: true }, runtime);

      expect(vi.mocked(select)).not.toHaveBeenCalled();
      expect(vi.mocked(multiselect)).not.toHaveBeenCalled();
      expect(savedArgs?.proposals.map((p) => p.id)).toEqual(["p1", "p2"]);
      expect(
        vi.mocked(runtime.log).mock.calls.some(([msg]) => String(msg).includes("```json")),
      ).toBe(false);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("respects gateway settings mode=off (skips capture entirely)", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-postflight-off-"));
    const store = path.join(dir, "sessions.json");
    const workspace = path.join(dir, "workspace");
    mockConfig(store, workspace);

    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => ({ memory_capture_mode: "off", memory_capture_debug: false }),
    })) as any;

    vi.mocked(agentCommand).mockImplementationOnce(async (_opts, rt) => {
      rt.log?.("agent output");
      return {
        payloads: [{ text: "agent output" }],
        meta: {
          postflightCapture: {
            kind: "memory_writes",
            rawJson: '{"postflight":{"type":"memory_writes"}}',
            proposals: [{ id: "p1", text: "First memory" }],
          },
        },
      };
    });

    try {
      await agentCliCommand({ message: "hi", to: "+1555", local: true }, runtime);

      expect(globalThis.fetch).toHaveBeenCalled();
      expect(vi.mocked(select)).not.toHaveBeenCalled();
      expect(savedArgs).toBeNull();
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("prints raw JSON only when debug=true", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-postflight-debug-"));
    const store = path.join(dir, "sessions.json");
    const workspace = path.join(dir, "workspace");
    mockConfig(store, workspace);

    process.env.OPENCLAW_MEMORY_CAPTURE_MODE = "suggest";
    process.env.OPENCLAW_MEMORY_CAPTURE_DEBUG = "1";
    process.env.OPENCLAW_MEMORY_CAPTURE_AUTO_SAVE = "1";

    vi.mocked(agentCommand).mockImplementationOnce(async (_opts, rt) => {
      rt.log?.("agent output");
      return {
        payloads: [{ text: "agent output" }],
        meta: {
          postflightCapture: {
            kind: "memory_writes",
            rawJson: '{"postflight":{"type":"memory_writes","proposals":["x"]}}',
            proposals: [{ id: "p1", text: "First memory" }],
          },
        },
      };
    });

    try {
      await agentCliCommand({ message: "hi", to: "+1555", local: true }, runtime);
      expect(
        vi.mocked(runtime.log).mock.calls.some(([msg]) => String(msg).includes("```json")),
      ).toBe(true);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
