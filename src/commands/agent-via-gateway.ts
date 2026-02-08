import { isCancel, multiselect, select } from "@clack/prompts";
import type { CliDeps } from "../cli/deps.js";
import type { PostflightCapturePayload } from "../postflight/types.js";
import type { PostflightCaptureSettings } from "../postflight/types.js";
import type { RuntimeEnv } from "../runtime.js";
import { listAgentIds } from "../agents/agent-scope.js";
import { resolveAgentWorkspaceDir } from "../agents/agent-scope.js";
import { DEFAULT_CHAT_CHANNEL } from "../channels/registry.js";
import { formatCliCommand } from "../cli/command-format.js";
import { withProgress } from "../cli/progress.js";
import { loadConfig } from "../config/config.js";
import { resolveAgentIdFromSessionKey } from "../config/sessions.js";
import { resolveGatewayAuth } from "../gateway/auth.js";
import { callGateway, randomIdempotencyKey } from "../gateway/call.js";
import { isTruthyEnvValue } from "../infra/env.js";
import { extractPostflightCaptureFromText } from "../postflight/extract.js";
import { appendPostflightProposalsToDailyMemory } from "../postflight/save.js";
import { normalizeAgentId } from "../routing/session-key.js";
import { shortenHomePath } from "../utils.js";
import {
  GATEWAY_CLIENT_MODES,
  GATEWAY_CLIENT_NAMES,
  normalizeMessageChannel,
} from "../utils/message-channel.js";
import { agentCommand } from "./agent.js";
import { resolveSessionKeyForRequest } from "./agent/session.js";

type AgentGatewayResult = {
  payloads?: Array<{
    text?: string;
    mediaUrl?: string | null;
    mediaUrls?: string[];
  }>;
  meta?: unknown;
};

type GatewayAgentResponse = {
  runId?: string;
  status?: string;
  summary?: string;
  result?: AgentGatewayResult;
};

type AgentCliRunResult = GatewayAgentResponse & {
  postflightCapture?: PostflightCapturePayload | null;
};

export type AgentCliOpts = {
  message: string;
  agent?: string;
  to?: string;
  sessionId?: string;
  thinking?: string;
  verbose?: string;
  json?: boolean;
  timeout?: string;
  deliver?: boolean;
  channel?: string;
  replyTo?: string;
  replyChannel?: string;
  replyAccount?: string;
  bestEffortDeliver?: boolean;
  lane?: string;
  runId?: string;
  extraSystemPrompt?: string;
  local?: boolean;
};

function normalizeCaptureMode(raw: unknown): "off" | "suggest" {
  const val = typeof raw === "string" ? raw.trim().toLowerCase() : "";
  if (val === "off") return "off";
  if (val === "suggest") return "suggest";
  return "suggest";
}

function resolveCaptureSettingsFromEnv(): PostflightCaptureSettings | null {
  const hasMode =
    typeof process.env.OPENCLAW_MEMORY_CAPTURE_MODE === "string" &&
    process.env.OPENCLAW_MEMORY_CAPTURE_MODE.trim().length > 0;
  const hasDebug =
    typeof process.env.OPENCLAW_MEMORY_CAPTURE_DEBUG === "string" &&
    process.env.OPENCLAW_MEMORY_CAPTURE_DEBUG.trim().length > 0;
  if (!hasMode && !hasDebug) {
    return null;
  }
  return {
    mode: normalizeCaptureMode(process.env.OPENCLAW_MEMORY_CAPTURE_MODE),
    debug: isTruthyEnvValue(process.env.OPENCLAW_MEMORY_CAPTURE_DEBUG),
    source: "env",
  };
}

async function fetchCaptureSettingsViaGatewayHttp(
  cfg: ReturnType<typeof loadConfig>,
): Promise<PostflightCaptureSettings | null> {
  try {
    const { buildGatewayConnectionDetails } = await import("../gateway/call.js");
    const ws = new URL(buildGatewayConnectionDetails({ config: cfg }).url);
    const httpProto =
      ws.protocol === "wss:" ? "https:" : ws.protocol === "ws:" ? "http:" : ws.protocol;
    const base = new URL(ws.toString());
    base.protocol = httpProto;
    base.pathname = "/";
    base.search = "";
    base.hash = "";
    const url = new URL("/v1/settings", base);

    const isRemoteMode = cfg.gateway?.mode === "remote";
    const remote = isRemoteMode ? cfg.gateway?.remote : undefined;
    const auth = resolveGatewayAuth({
      authConfig: cfg.gateway?.auth,
      env: process.env,
      tailscaleMode: cfg.gateway?.tailscale?.mode,
    });
    const token = isRemoteMode ? remote?.token?.trim() : auth.token?.trim();
    const password = isRemoteMode ? remote?.password?.trim() : auth.password?.trim();
    const bearer = auth.mode === "password" ? password : token;
    const headers: Record<string, string> = {};
    if (bearer) {
      headers.authorization = `Bearer ${bearer}`;
    }

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1200);
    try {
      const res = await fetch(url, { method: "GET", headers, signal: ctrl.signal });
      if (!res.ok) {
        return null;
      }
      const body = (await res.json()) as Record<string, unknown>;
      return {
        mode: normalizeCaptureMode(body.memory_capture_mode),
        debug: Boolean(body.memory_capture_debug),
        source: "gateway",
      };
    } finally {
      clearTimeout(timer);
    }
  } catch {
    return null;
  }
}

function parseTimeoutSeconds(opts: { cfg: ReturnType<typeof loadConfig>; timeout?: string }) {
  const raw =
    opts.timeout !== undefined
      ? Number.parseInt(String(opts.timeout), 10)
      : (opts.cfg.agents?.defaults?.timeoutSeconds ?? 600);
  if (Number.isNaN(raw) || raw <= 0) {
    throw new Error("--timeout must be a positive integer (seconds)");
  }
  return raw;
}

function formatPayloadForLog(payload: {
  text?: string;
  mediaUrls?: string[];
  mediaUrl?: string | null;
}) {
  const lines: string[] = [];
  if (payload.text) {
    lines.push(payload.text.trimEnd());
  }
  const mediaUrl =
    typeof payload.mediaUrl === "string" && payload.mediaUrl.trim()
      ? payload.mediaUrl.trim()
      : undefined;
  const media = payload.mediaUrls ?? (mediaUrl ? [mediaUrl] : []);
  for (const url of media) {
    lines.push(`MEDIA:${url}`);
  }
  return lines.join("\n").trimEnd();
}

function formatPostflightProposalSummary(proposals: Array<{ text: string }>): string {
  const lines: string[] = [];
  lines.push("Postflight capture:");
  for (const [idx, p] of proposals.entries()) {
    const text = p.text.trim().replace(/\s+/g, " ");
    const clipped = text.length > 96 ? `${text.slice(0, 93)}…` : text;
    lines.push(`[x] ${idx + 1}. ${clipped}`);
  }
  return lines.join("\n");
}

export async function agentViaGatewayCommand(opts: AgentCliOpts, runtime: RuntimeEnv) {
  const body = (opts.message ?? "").trim();
  if (!body) {
    throw new Error("Message (--message) is required");
  }
  if (!opts.to && !opts.sessionId && !opts.agent) {
    throw new Error("Pass --to <E.164>, --session-id, or --agent to choose a session");
  }

  const cfg = loadConfig();
  const agentIdRaw = opts.agent?.trim();
  const agentId = agentIdRaw ? normalizeAgentId(agentIdRaw) : undefined;
  if (agentId) {
    const knownAgents = listAgentIds(cfg);
    if (!knownAgents.includes(agentId)) {
      throw new Error(
        `Unknown agent id "${agentIdRaw}". Use "${formatCliCommand("openclaw agents list")}" to see configured agents.`,
      );
    }
  }
  const timeoutSeconds = parseTimeoutSeconds({ cfg, timeout: opts.timeout });
  const gatewayTimeoutMs = Math.max(10_000, (timeoutSeconds + 30) * 1000);

  const sessionKey = resolveSessionKeyForRequest({
    cfg,
    agentId,
    to: opts.to,
    sessionId: opts.sessionId,
  }).sessionKey;

  const channel = normalizeMessageChannel(opts.channel) ?? DEFAULT_CHAT_CHANNEL;
  const idempotencyKey = opts.runId?.trim() || randomIdempotencyKey();

  const response = await withProgress(
    {
      label: "Waiting for agent reply…",
      indeterminate: true,
      enabled: opts.json !== true,
    },
    async () =>
      await callGateway<GatewayAgentResponse>({
        method: "agent",
        params: {
          message: body,
          agentId,
          to: opts.to,
          replyTo: opts.replyTo,
          sessionId: opts.sessionId,
          sessionKey,
          thinking: opts.thinking,
          deliver: Boolean(opts.deliver),
          channel,
          replyChannel: opts.replyChannel,
          replyAccountId: opts.replyAccount,
          timeout: timeoutSeconds,
          lane: opts.lane,
          extraSystemPrompt: opts.extraSystemPrompt,
          idempotencyKey,
        },
        expectFinal: true,
        timeoutMs: gatewayTimeoutMs,
        clientName: GATEWAY_CLIENT_NAMES.CLI,
        mode: GATEWAY_CLIENT_MODES.CLI,
      }),
  );

  if (opts.json) {
    runtime.log(JSON.stringify(response, null, 2));
    return response;
  }

  const result = response?.result;
  let postflightCapture: PostflightCapturePayload | null = null;
  const payloads =
    (result?.payloads ?? []).map((payload) => {
      const text = typeof payload.text === "string" ? payload.text : "";
      if (!text.trim()) {
        return payload;
      }
      const extracted = extractPostflightCaptureFromText(text);
      if (extracted.capture) {
        postflightCapture = extracted.capture;
      }
      return extracted.cleanedText !== text ? { ...payload, text: extracted.cleanedText } : payload;
    }) ?? [];

  if (payloads.length === 0) {
    runtime.log(response?.summary ? String(response.summary) : "No reply from agent.");
    return response;
  }

  for (const payload of payloads) {
    const out = formatPayloadForLog(payload);
    if (out) {
      runtime.log(out);
    }
  }

  const nextResponse: AgentCliRunResult = postflightCapture
    ? { ...response, postflightCapture }
    : response;
  return nextResponse;
}

export async function agentCliCommand(opts: AgentCliOpts, runtime: RuntimeEnv, deps?: CliDeps) {
  const cfg = loadConfig();
  const localOpts = {
    ...opts,
    agentId: opts.agent,
    replyAccountId: opts.replyAccount,
  };
  if (opts.local === true) {
    const result = await agentCommand(localOpts, runtime, deps);
    await runPostflightCapture({
      cfg,
      runtime,
      opts,
      capture: (result.meta as any)?.postflightCapture ?? null,
    });
    return result;
  }

  try {
    const result = (await agentViaGatewayCommand(opts, runtime)) as AgentCliRunResult;
    await runPostflightCapture({ cfg, runtime, opts, capture: result.postflightCapture ?? null });
    return result;
  } catch (err) {
    runtime.error?.(`Gateway agent failed; falling back to embedded: ${String(err)}`);
    const result = await agentCommand(localOpts, runtime, deps);
    await runPostflightCapture({
      cfg,
      runtime,
      opts,
      capture: (result.meta as any)?.postflightCapture ?? null,
    });
    return result;
  }
}

async function runPostflightCapture(params: {
  cfg: ReturnType<typeof loadConfig>;
  runtime: RuntimeEnv;
  opts: AgentCliOpts;
  capture: PostflightCapturePayload | null;
}) {
  if (params.opts.json) {
    return;
  }
  const capture = params.capture;
  if (!capture || capture.kind !== "memory_writes" || capture.proposals.length === 0) {
    return;
  }

  const settings = (await fetchCaptureSettingsViaGatewayHttp(params.cfg)) ??
    resolveCaptureSettingsFromEnv() ?? { mode: "suggest", debug: false, source: "default" };
  if (settings.mode === "off") {
    return;
  }

  if (settings.debug) {
    params.runtime.log(
      ["", "MEMORY_CAPTURE_JSON (debug):", "```json", capture.rawJson, "```"].join("\n"),
    );
  }

  const sessionKey = resolveSessionKeyForRequest({
    cfg: params.cfg,
    agentId: params.opts.agent,
    to: params.opts.to,
    sessionId: params.opts.sessionId,
  }).sessionKey;
  const agentId = resolveAgentIdFromSessionKey(sessionKey);
  const workspaceDir = resolveAgentWorkspaceDir(params.cfg, agentId);

  const proposals = capture.proposals.slice(0, 20);
  const allValues = proposals.map((p) => p.id);

  let selected = proposals;
  const canPrompt = Boolean(process.stdout.isTTY && process.stdin.isTTY);
  const autoSave = isTruthyEnvValue(process.env.OPENCLAW_MEMORY_CAPTURE_AUTO_SAVE);
  if (autoSave || !canPrompt) {
    // Low-friction defaults:
    // - orchestrated runs usually have no TTY; default to "Save all"
    // - OPENCLAW_MEMORY_CAPTURE_AUTO_SAVE forces "Save all" even when interactive
    selected = proposals;
  } else {
    params.runtime.log(formatPostflightProposalSummary(proposals));
    const action = await select({
      message: "Save postflight capture to memory?",
      options: [
        { value: "save_all", label: "Save all" },
        { value: "pick", label: "Choose items" },
        { value: "skip", label: "Skip" },
      ],
      initialValue: "save_all",
    });
    if (isCancel(action)) {
      return;
    }
    if (action === "skip") {
      return;
    }
    if (action === "pick") {
      const picked = await multiselect({
        message: "Pick items to save",
        options: proposals.map((p, idx) => ({
          value: p.id,
          label: `${idx + 1}. ${p.text.length > 96 ? `${p.text.slice(0, 93)}…` : p.text}`,
        })),
        initialValues: allValues,
      });
      if (isCancel(picked)) {
        return;
      }
      const pickedSet = new Set(picked);
      selected = proposals.filter((p) => pickedSet.has(p.id));
    } else {
      selected = proposals;
    }
  }

  if (selected.length === 0) {
    return;
  }

  const saved = await appendPostflightProposalsToDailyMemory({
    workspaceDir,
    proposals: selected,
  });
  const rel = shortenHomePath(saved.filePath);
  params.runtime.log(`Saved ${saved.appended}/${selected.length} postflight memories to ${rel}.`);
}
