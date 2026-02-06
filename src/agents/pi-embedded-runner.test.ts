import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import "./test-helpers/fast-coding-tools.js";
import type { OpenClawConfig } from "../config/config.js";
import { ensureOpenClawModelsJson } from "./models-config.js";

vi.mock("@mariozechner/pi-ai", async () => {
  const actual = await vi.importActual<typeof import("@mariozechner/pi-ai")>("@mariozechner/pi-ai");

  const buildAssistantMessage = (model: { api: string; provider: string; id: string }) => ({
    role: "assistant" as const,
    content: [{ type: "text" as const, text: "ok" }],
    stopReason: "stop" as const,
    api: model.api,
    provider: model.provider,
    model: model.id,
    usage: {
      input: 1,
      output: 1,
      cacheRead: 0,
      cacheWrite: 0,
      totalTokens: 2,
      cost: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        total: 0,
      },
    },
    timestamp: Date.now(),
  });

  const buildAssistantToolCall = (params: {
    model: { api: string; provider: string; id: string };
    toolCallId: string;
    name: string;
    args: Record<string, unknown>;
  }) => ({
    role: "assistant" as const,
    content: [
      {
        type: "toolCall" as const,
        id: params.toolCallId,
        name: params.name,
        arguments: params.args,
      },
    ],
    stopReason: "toolUse" as const,
    api: params.model.api,
    provider: params.model.provider,
    model: params.model.id,
    usage: {
      input: 1,
      output: 1,
      cacheRead: 0,
      cacheWrite: 0,
      totalTokens: 2,
      cost: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        total: 0,
      },
    },
    timestamp: Date.now(),
  });

  const buildAssistantErrorMessage = (model: { api: string; provider: string; id: string }) => ({
    role: "assistant" as const,
    content: [] as const,
    stopReason: "error" as const,
    errorMessage: "boom",
    api: model.api,
    provider: model.provider,
    model: model.id,
    usage: {
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
      totalTokens: 0,
      cost: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        total: 0,
      },
    },
    timestamp: Date.now(),
  });

  const findToolNames = (args: unknown[]): string[] => {
    for (const arg of args) {
      if (!arg || typeof arg !== "object") {
        continue;
      }
      const rec = arg as Record<string, unknown>;
      const candidates = [rec.tools, rec.customTools, rec.functions];
      for (const candidate of candidates) {
        if (!Array.isArray(candidate)) {
          continue;
        }
        const names = candidate
          .map((tool) => {
            if (!tool || typeof tool !== "object") {
              return "";
            }
            const t = tool as Record<string, unknown>;
            return typeof t.name === "string"
              ? t.name
              : typeof (t.function as { name?: unknown } | undefined)?.name === "string"
                ? String((t.function as { name?: unknown }).name)
                : "";
          })
          .filter(Boolean);
        if (names.length > 0) {
          return names;
        }
      }
    }
    return [];
  };

  const findMessages = (
    args: unknown[],
  ): Array<{ role?: unknown; content?: unknown; toolName?: unknown }> => {
    for (const arg of args) {
      if (arg && typeof arg === "object" && !Array.isArray(arg)) {
        const rec = arg as { messages?: unknown };
        if (Array.isArray(rec.messages)) {
          return rec.messages as Array<{ role?: unknown; content?: unknown; toolName?: unknown }>;
        }
      }
      if (!Array.isArray(arg)) {
        continue;
      }
      if (arg.some((item) => item && typeof item === "object" && "role" in (item as object))) {
        return arg as Array<{ role?: unknown; content?: unknown; toolName?: unknown }>;
      }
    }
    return [];
  };

  const textFromContent = (content: unknown): string => {
    if (typeof content === "string") {
      return content;
    }
    if (Array.isArray(content)) {
      const first = content.find(
        (c) => c && typeof c === "object" && (c as { type?: unknown }).type === "text",
      ) as { text?: unknown } | undefined;
      if (typeof first?.text === "string") {
        return first.text;
      }
    }
    return "";
  };

  const shouldReturnInitToolCall = (args: unknown[]): { prompt: string } | null => {
    const toolNames = findToolNames(args)
      .map((n) => n.trim().toLowerCase())
      .filter(Boolean);
    if (toolNames.length !== 1 || toolNames[0] !== "init") {
      return null;
    }
    const messages = findMessages(args);
    const hasInitResult = messages.some(
      (m) =>
        m &&
        m.role === "toolResult" &&
        String(m.toolName ?? "")
          .trim()
          .toLowerCase() === "init",
    );
    if (hasInitResult) {
      return null;
    }
    const lastUser = messages
      .slice()
      .toReversed()
      .find((m) => m && m.role === "user");
    const prompt = lastUser ? textFromContent(lastUser.content) : "";
    return { prompt };
  };

  const completeWithToolAwareness = async (
    model: { api: string; provider: string; id: string },
    ...args: unknown[]
  ) => {
    if (model.id === "mock-error") {
      return buildAssistantErrorMessage(model);
    }
    const initCall = shouldReturnInitToolCall(args);
    if (initCall) {
      return buildAssistantToolCall({
        model,
        toolCallId: "call_init",
        name: "init",
        args: { prompt: initCall.prompt },
      });
    }
    return buildAssistantMessage(model);
  };

  return {
    ...actual,
    complete: async (model: { api: string; provider: string; id: string }, ...args: unknown[]) =>
      completeWithToolAwareness(model, ...args),
    completeSimple: async (
      model: { api: string; provider: string; id: string },
      ...args: unknown[]
    ) => completeWithToolAwareness(model, ...args),
    streamSimple: (model: { api: string; provider: string; id: string }, ...args: unknown[]) => {
      const stream = new actual.AssistantMessageEventStream();
      queueMicrotask(async () => {
        const message = await completeWithToolAwareness(model, ...args);
        stream.push({
          type: "done",
          reason:
            (message as { stopReason?: unknown }).stopReason === "toolUse" ? "toolUse" : "stop",
          message,
        });
        stream.end();
      });
      return stream;
    },
  };
});

let runEmbeddedPiAgent: typeof import("./pi-embedded-runner.js").runEmbeddedPiAgent;
let tempRoot: string | undefined;
let agentDir: string;
let workspaceDir: string;
let sessionCounter = 0;

beforeAll(async () => {
  vi.useRealTimers();
  ({ runEmbeddedPiAgent } = await import("./pi-embedded-runner.js"));
  tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "openclaw-embedded-agent-"));
  agentDir = path.join(tempRoot, "agent");
  workspaceDir = path.join(tempRoot, "workspace");
  await fs.mkdir(agentDir, { recursive: true });
  await fs.mkdir(workspaceDir, { recursive: true });
}, 20_000);

afterAll(async () => {
  if (!tempRoot) {
    return;
  }
  await fs.rm(tempRoot, { recursive: true, force: true });
  tempRoot = undefined;
});

const makeOpenAiConfig = (modelIds: string[]) =>
  ({
    models: {
      providers: {
        openai: {
          api: "openai-responses",
          apiKey: "sk-test",
          baseUrl: "https://example.com",
          models: modelIds.map((id) => ({
            id,
            name: `Mock ${id}`,
            reasoning: false,
            input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: 16_000,
            maxTokens: 2048,
          })),
        },
      },
    },
  }) satisfies OpenClawConfig;

const makeAnthropicConfig = (modelIds: string[]) =>
  ({
    models: {
      providers: {
        anthropic: {
          api: "anthropic-messages",
          apiKey: "sk-test",
          baseUrl: "https://example.com",
          models: modelIds.map((id) => ({
            id,
            name: `Mock ${id}`,
            reasoning: false,
            input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            contextWindow: 16_000,
            maxTokens: 2048,
          })),
        },
      },
    },
  }) satisfies OpenClawConfig;

const ensureModels = (cfg: OpenClawConfig) => ensureOpenClawModelsJson(cfg, agentDir) as unknown;

const nextSessionFile = () => {
  sessionCounter += 1;
  return path.join(workspaceDir, `session-${sessionCounter}.jsonl`);
};

const testSessionKey = "agent:test:embedded";
const immediateEnqueue = async <T>(task: () => Promise<T>) => task();

const textFromContent = (content: unknown) => {
  if (typeof content === "string") {
    return content;
  }
  if (Array.isArray(content) && content[0]?.type === "text") {
    return (content[0] as { text?: string }).text;
  }
  return undefined;
};

const readSessionMessages = async (sessionFile: string) => {
  const raw = await fs.readFile(sessionFile, "utf-8");
  return raw
    .split(/\r?\n/)
    .filter(Boolean)
    .map(
      (line) =>
        JSON.parse(line) as {
          type?: string;
          message?: { role?: string; content?: unknown; toolName?: unknown };
        },
    )
    .filter((entry) => entry.type === "message")
    .map((entry) => entry.message as { role?: string; content?: unknown; toolName?: unknown });
};

describe("runEmbeddedPiAgent", () => {
  const itIfNotWin32 = process.platform === "win32" ? it.skip : it;
  it("writes models.json into the provided agentDir", async () => {
    const sessionFile = nextSessionFile();

    const cfg = {
      models: {
        providers: {
          minimax: {
            baseUrl: "https://api.minimax.io/anthropic",
            api: "anthropic-messages",
            apiKey: "sk-minimax-test",
            models: [
              {
                id: "MiniMax-M2.1",
                name: "MiniMax M2.1",
                reasoning: false,
                input: ["text"],
                cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
                contextWindow: 200000,
                maxTokens: 8192,
              },
            ],
          },
        },
      },
    } satisfies OpenClawConfig;

    await expect(
      runEmbeddedPiAgent({
        sessionId: "session:test",
        sessionKey: testSessionKey,
        sessionFile,
        workspaceDir,
        config: cfg,
        prompt: "hi",
        provider: "definitely-not-a-provider",
        model: "definitely-not-a-model",
        timeoutMs: 1,
        agentDir,
        enqueue: immediateEnqueue,
      }),
    ).rejects.toThrow(/Unknown model:/);

    await expect(fs.stat(path.join(agentDir, "models.json"))).resolves.toBeTruthy();
  });

  itIfNotWin32(
    "persists the first user message before assistant output",
    { timeout: 120_000 },
    async () => {
      const sessionFile = nextSessionFile();
      const cfg = makeOpenAiConfig(["mock-1"]);
      await ensureModels(cfg);

      await runEmbeddedPiAgent({
        sessionId: "session:test",
        sessionKey: testSessionKey,
        sessionFile,
        workspaceDir,
        config: cfg,
        prompt: "hello",
        provider: "openai",
        model: "mock-1",
        timeoutMs: 5_000,
        agentDir,
        enqueue: immediateEnqueue,
      });

      const messages = await readSessionMessages(sessionFile);
      const firstUserIndex = messages.findIndex(
        (message) => message?.role === "user" && textFromContent(message.content) === "hello",
      );
      const firstAssistantIndex = messages.findIndex((message) => message?.role === "assistant");
      expect(firstUserIndex).toBeGreaterThanOrEqual(0);
      if (firstAssistantIndex !== -1) {
        expect(firstUserIndex).toBeLessThan(firstAssistantIndex);
      }
    },
  );

  itIfNotWin32("runs init preflight once per session", async () => {
    const sessionFile = nextSessionFile();
    const cfg = makeOpenAiConfig(["mock-1"]);
    await ensureModels(cfg);

    const first = await runEmbeddedPiAgent({
      sessionId: "session:test",
      sessionKey: testSessionKey,
      sessionFile,
      workspaceDir,
      config: cfg,
      prompt: "hello",
      provider: "openai",
      model: "mock-1",
      timeoutMs: 5_000,
      agentDir,
      enqueue: immediateEnqueue,
    });
    expect(first.payloads?.[0]?.text).toBeTruthy();

    const afterFirst = await readSessionMessages(sessionFile);
    const initResultsFirst = afterFirst.filter(
      (m) =>
        m?.role === "toolResult" &&
        String(m.toolName ?? "")
          .trim()
          .toLowerCase() === "init",
    );
    expect(initResultsFirst.length).toBe(1);
    expect(
      afterFirst.some(
        (m) =>
          m?.role === "user" &&
          typeof textFromContent(m.content) === "string" &&
          String(textFromContent(m.content)).includes("[openclaw] Continue."),
      ),
    ).toBe(true);

    await runEmbeddedPiAgent({
      sessionId: "session:test",
      sessionKey: testSessionKey,
      sessionFile,
      workspaceDir,
      config: cfg,
      prompt: "second",
      provider: "openai",
      model: "mock-1",
      timeoutMs: 5_000,
      agentDir,
      enqueue: immediateEnqueue,
    });

    const afterSecond = await readSessionMessages(sessionFile);
    const initResultsSecond = afterSecond.filter(
      (m) =>
        m?.role === "toolResult" &&
        String(m.toolName ?? "")
          .trim()
          .toLowerCase() === "init",
    );
    expect(initResultsSecond.length).toBe(1);
  });

  itIfNotWin32("runs init preflight for anthropic providers too", async () => {
    const sessionFile = nextSessionFile();
    const cfg = makeAnthropicConfig(["mock-a"]);
    await ensureModels(cfg);

    await runEmbeddedPiAgent({
      sessionId: "session:test",
      sessionKey: testSessionKey,
      sessionFile,
      workspaceDir,
      config: cfg,
      prompt: "hello",
      provider: "anthropic",
      model: "mock-a",
      timeoutMs: 5_000,
      agentDir,
      enqueue: immediateEnqueue,
    });

    const messages = await readSessionMessages(sessionFile);
    const initResults = messages.filter(
      (m) =>
        m?.role === "toolResult" &&
        String(m.toolName ?? "")
          .trim()
          .toLowerCase() === "init",
    );
    expect(initResults.length).toBe(1);
  });

  it("persists the user message when prompt fails before assistant output", async () => {
    const sessionFile = nextSessionFile();
    const cfg = makeOpenAiConfig(["mock-error"]);
    await ensureModels(cfg);

    const result = await runEmbeddedPiAgent({
      sessionId: "session:test",
      sessionKey: testSessionKey,
      sessionFile,
      workspaceDir,
      config: cfg,
      prompt: "boom",
      provider: "openai",
      model: "mock-error",
      timeoutMs: 5_000,
      agentDir,
      enqueue: immediateEnqueue,
    });
    expect(result.payloads[0]?.isError).toBe(true);

    const messages = await readSessionMessages(sessionFile);
    const userIndex = messages.findIndex(
      (message) => message?.role === "user" && textFromContent(message.content) === "boom",
    );
    expect(userIndex).toBeGreaterThanOrEqual(0);
  });

  it(
    "appends new user + assistant after existing transcript entries",
    { timeout: 90_000 },
    async () => {
      const { SessionManager } = await import("@mariozechner/pi-coding-agent");
      const sessionFile = nextSessionFile();

      const sessionManager = SessionManager.open(sessionFile);
      sessionManager.appendMessage({
        role: "user",
        content: [{ type: "text", text: "seed user" }],
      });
      sessionManager.appendMessage({
        role: "assistant",
        content: [{ type: "text", text: "seed assistant" }],
        stopReason: "stop",
        api: "openai-responses",
        provider: "openai",
        model: "mock-1",
        usage: {
          input: 1,
          output: 1,
          cacheRead: 0,
          cacheWrite: 0,
          totalTokens: 2,
          cost: {
            input: 0,
            output: 0,
            cacheRead: 0,
            cacheWrite: 0,
            total: 0,
          },
        },
        timestamp: Date.now(),
      });

      const cfg = makeOpenAiConfig(["mock-1"]);
      await ensureModels(cfg);

      await runEmbeddedPiAgent({
        sessionId: "session:test",
        sessionKey: testSessionKey,
        sessionFile,
        workspaceDir,
        config: cfg,
        prompt: "hello",
        provider: "openai",
        model: "mock-1",
        timeoutMs: 5_000,
        agentDir,
        enqueue: immediateEnqueue,
      });

      const messages = await readSessionMessages(sessionFile);
      const seedUserIndex = messages.findIndex(
        (message) => message?.role === "user" && textFromContent(message.content) === "seed user",
      );
      const seedAssistantIndex = messages.findIndex(
        (message) =>
          message?.role === "assistant" && textFromContent(message.content) === "seed assistant",
      );
      const newUserIndex = messages.findIndex(
        (message) => message?.role === "user" && textFromContent(message.content) === "hello",
      );
      const newAssistantIndex = messages.findIndex(
        (message, index) => index > newUserIndex && message?.role === "assistant",
      );
      expect(seedUserIndex).toBeGreaterThanOrEqual(0);
      expect(seedAssistantIndex).toBeGreaterThan(seedUserIndex);
      expect(newUserIndex).toBeGreaterThan(seedAssistantIndex);
      expect(newAssistantIndex).toBeGreaterThan(newUserIndex);
    },
  );

  it("persists multi-turn user/assistant ordering across runs", async () => {
    const sessionFile = nextSessionFile();
    const cfg = makeOpenAiConfig(["mock-1"]);
    await ensureModels(cfg);

    await runEmbeddedPiAgent({
      sessionId: "session:test",
      sessionKey: testSessionKey,
      sessionFile,
      workspaceDir,
      config: cfg,
      prompt: "first",
      provider: "openai",
      model: "mock-1",
      timeoutMs: 5_000,
      agentDir,
      enqueue: immediateEnqueue,
    });

    await runEmbeddedPiAgent({
      sessionId: "session:test",
      sessionKey: testSessionKey,
      sessionFile,
      workspaceDir,
      config: cfg,
      prompt: "second",
      provider: "openai",
      model: "mock-1",
      timeoutMs: 5_000,
      agentDir,
      enqueue: immediateEnqueue,
    });

    const messages = await readSessionMessages(sessionFile);
    const firstUserIndex = messages.findIndex(
      (message) => message?.role === "user" && textFromContent(message.content) === "first",
    );
    const firstAssistantIndex = messages.findIndex(
      (message, index) => index > firstUserIndex && message?.role === "assistant",
    );
    const secondUserIndex = messages.findIndex(
      (message, index) =>
        index > firstAssistantIndex &&
        message?.role === "user" &&
        textFromContent(message.content) === "second",
    );
    const secondAssistantIndex = messages.findIndex(
      (message, index) => index > secondUserIndex && message?.role === "assistant",
    );

    expect(firstUserIndex).toBeGreaterThanOrEqual(0);
    expect(firstAssistantIndex).toBeGreaterThan(firstUserIndex);
    expect(secondUserIndex).toBeGreaterThan(firstAssistantIndex);
    expect(secondAssistantIndex).toBeGreaterThan(secondUserIndex);
  });

  it("repairs orphaned user messages and continues", async () => {
    const { SessionManager } = await import("@mariozechner/pi-coding-agent");
    const sessionFile = nextSessionFile();

    const sessionManager = SessionManager.open(sessionFile);
    sessionManager.appendMessage({
      role: "user",
      content: [{ type: "text", text: "orphaned user" }],
    });

    const cfg = makeOpenAiConfig(["mock-1"]);
    await ensureModels(cfg);

    const result = await runEmbeddedPiAgent({
      sessionId: "session:test",
      sessionKey: testSessionKey,
      sessionFile,
      workspaceDir,
      config: cfg,
      prompt: "hello",
      provider: "openai",
      model: "mock-1",
      timeoutMs: 5_000,
      agentDir,
      enqueue: immediateEnqueue,
    });

    expect(result.meta.error).toBeUndefined();
    expect(result.payloads?.length ?? 0).toBeGreaterThan(0);
  });

  it("repairs orphaned single-user sessions and continues", async () => {
    const { SessionManager } = await import("@mariozechner/pi-coding-agent");
    const sessionFile = nextSessionFile();

    const sessionManager = SessionManager.open(sessionFile);
    sessionManager.appendMessage({
      role: "user",
      content: [{ type: "text", text: "solo user" }],
    });

    const cfg = makeOpenAiConfig(["mock-1"]);
    await ensureModels(cfg);

    const result = await runEmbeddedPiAgent({
      sessionId: "session:test",
      sessionKey: testSessionKey,
      sessionFile,
      workspaceDir,
      config: cfg,
      prompt: "hello",
      provider: "openai",
      model: "mock-1",
      timeoutMs: 5_000,
      agentDir,
      enqueue: immediateEnqueue,
    });

    expect(result.meta.error).toBeUndefined();
    expect(result.payloads?.length ?? 0).toBeGreaterThan(0);
  });
});
