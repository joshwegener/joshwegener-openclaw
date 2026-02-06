import { Type } from "@sinclair/typebox";
import type { OpenClawConfig } from "../../config/config.js";
import type { AnyAgentTool } from "./common.js";
import { resolveSessionAgentId } from "../agent-scope.js";
import { resolveMemorySearchConfig } from "../memory-search.js";
import { jsonResult, readNumberParam, readStringParam } from "./common.js";

export const INIT_TOOL_NAME = "init";

export type InitNextCall = {
  tool: string;
  args: Record<string, unknown>;
};

export type InitToolOutputV1 = {
  type: "openclaw_init";
  version: 1;
  prompt: string;
  next_calls: InitNextCall[][];
};

const InitToolSchema = Type.Object({
  prompt: Type.Optional(
    Type.String({
      description:
        "User prompt for preflight (used to seed memory search). If omitted, OpenClaw will still initialize but may skip prompt-based retrieval.",
    }),
  ),
  memoryMaxResults: Type.Optional(
    Type.Number({
      description:
        "Override max memory_search results (defaults to configured memorySearch.query).",
    }),
  ),
  memoryMinScore: Type.Optional(
    Type.Number({
      description: "Override min memory_search score (defaults to configured memorySearch.query).",
    }),
  ),
});

function resolveMemorySearchNextCalls(params: {
  cfg?: OpenClawConfig;
  sessionKey?: string;
  prompt: string;
  memoryMaxResults?: number;
  memoryMinScore?: number;
}): InitNextCall[] {
  if (!params.cfg) {
    return [];
  }
  const agentId = resolveSessionAgentId({
    sessionKey: params.sessionKey,
    config: params.cfg,
  });
  const memoryCfg = resolveMemorySearchConfig(params.cfg, agentId);
  if (!memoryCfg?.enabled) {
    return [];
  }
  if (!params.prompt.trim()) {
    return [];
  }
  return [
    {
      tool: "memory_search",
      args: {
        query: params.prompt,
        maxResults: params.memoryMaxResults ?? memoryCfg.query.maxResults,
        minScore: params.memoryMinScore ?? memoryCfg.query.minScore,
      },
    },
  ];
}

export function createInitTool(options?: {
  config?: OpenClawConfig;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Init",
    name: INIT_TOOL_NAME,
    description:
      "Mandatory session preflight. Returns init payload + next_calls (parallel groups) that OpenClaw will execute before exposing the full toolset.",
    parameters: InitToolSchema,
    execute: async (_toolCallId, params) => {
      const prompt = readStringParam(params, "prompt", { allowEmpty: true }) ?? "";
      const memoryMaxResults = readNumberParam(params, "memoryMaxResults");
      const memoryMinScore = readNumberParam(params, "memoryMinScore");
      const next_calls: InitNextCall[][] = [];
      const memoryCalls = resolveMemorySearchNextCalls({
        cfg: options?.config,
        sessionKey: options?.agentSessionKey,
        prompt,
        memoryMaxResults: memoryMaxResults ?? undefined,
        memoryMinScore: memoryMinScore ?? undefined,
      });
      if (memoryCalls.length > 0) {
        next_calls.push(memoryCalls);
      }
      const payload: InitToolOutputV1 = {
        type: "openclaw_init",
        version: 1,
        prompt,
        next_calls,
      };
      return jsonResult(payload);
    },
  };
}
