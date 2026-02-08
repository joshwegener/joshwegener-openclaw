import crypto from "node:crypto";
import type { PostflightCapturePayload, PostflightCaptureProposal } from "./types.js";
import { parseFenceSpans } from "../markdown/fences.js";

function stringifyJson(value: unknown): string | null {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return null;
  }
}

function normalizeReason(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

function normalizeProposalText(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const trimmed = value.trim().replace(/\s+/g, " ");
  return trimmed ? trimmed : undefined;
}

function normalizeProposals(value: unknown): PostflightCaptureProposal[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const proposals: PostflightCaptureProposal[] = [];
  for (const entry of value) {
    if (typeof entry === "string") {
      const text = normalizeProposalText(entry);
      if (text) {
        proposals.push({ id: crypto.randomUUID(), text });
      }
      continue;
    }
    if (!entry || typeof entry !== "object") {
      continue;
    }
    const obj = entry as Record<string, unknown>;
    const text =
      normalizeProposalText(obj.text) ??
      normalizeProposalText(obj.memory) ??
      normalizeProposalText(obj.content) ??
      normalizeProposalText(obj.summary);
    if (!text) {
      continue;
    }
    proposals.push({ id: crypto.randomUUID(), text });
  }
  return proposals;
}

function parseCapturePayload(parsed: unknown): PostflightCapturePayload | null {
  if (!parsed || typeof parsed !== "object") {
    return null;
  }
  const root = parsed as Record<string, unknown>;
  const postflight = root.postflight;
  if (!postflight || typeof postflight !== "object") {
    return null;
  }
  const pf = postflight as Record<string, unknown>;
  const type = typeof pf.type === "string" ? pf.type.trim() : "";
  const rawJson = stringifyJson(root);
  if (!rawJson) {
    return null;
  }

  if (type === "no_memory_capture") {
    return {
      kind: "no_memory_capture",
      reason: normalizeReason(pf.reason) ?? normalizeReason(pf.message),
      rawJson,
    };
  }
  if (type === "memory_writes") {
    const candidates = [
      normalizeProposals(pf.proposals),
      normalizeProposals(pf.memories),
      normalizeProposals(pf.writes),
    ];
    const proposals = candidates.find((list) => list.length > 0) ?? [];
    if (proposals.length === 0) {
      return null;
    }
    return {
      kind: "memory_writes",
      proposals,
      rawJson,
    };
  }
  return null;
}

function resolveFenceBody(text: string, span: { start: number; end: number; openLine: string }) {
  const openLineEnd = text.indexOf("\n", span.start);
  if (openLineEnd === -1) {
    return { body: "", bodyStart: span.end, bodyEnd: span.end };
  }
  const bodyStart = openLineEnd + 1;
  const closeLineStart = (() => {
    const idx = text.lastIndexOf("\n", Math.max(span.start, span.end - 1));
    return idx === -1 ? span.start : idx + 1;
  })();
  const bodyEnd = Math.max(bodyStart, closeLineStart);
  const body = text.slice(bodyStart, bodyEnd).trim();
  return { body, bodyStart, bodyEnd };
}

function hasCaptureLabel(lead: string, body: string, info: string): boolean {
  const leadLc = lead.toLowerCase();
  const infoLc = info.toLowerCase();
  if (infoLc.includes("memory_capture_json") || infoLc.includes("postflight_capture_json")) {
    return true;
  }
  if (leadLc.includes("memory_capture_json") || leadLc.includes("postflight_capture_json")) {
    return true;
  }
  if (infoLc.includes("memory_capture") && infoLc.includes("json")) {
    return true;
  }
  if (
    body.includes('"postflight"') &&
    (body.includes('"memory_writes"') || body.includes('"no_memory_capture"'))
  ) {
    return true;
  }
  return false;
}

function resolvePrevLine(text: string, idx: number): { line: string; start: number; end: number } {
  const before = text.slice(0, Math.max(0, idx));
  const lastNewline = before.lastIndexOf("\n");
  const end = lastNewline === -1 ? 0 : lastNewline;
  const prevEnd = end;
  const prevStart = before.lastIndexOf("\n", Math.max(0, prevEnd - 1));
  const start = prevStart === -1 ? 0 : prevStart + 1;
  const line = before.slice(start, prevEnd).trim();
  return { line, start, end: prevEnd };
}

export type ExtractPostflightCaptureResult = {
  cleanedText: string;
  capture: PostflightCapturePayload | null;
};

export function extractPostflightCaptureFromText(text: string): ExtractPostflightCaptureResult {
  const spans = parseFenceSpans(text);
  const removals: Array<{ start: number; end: number; capture: PostflightCapturePayload }> = [];

  for (const span of spans) {
    const info = (span.openLine.replace(/^( {0,3})(`{3,}|~{3,})/, "") ?? "").trim();
    const prev = resolvePrevLine(text, span.start);
    const { body } = resolveFenceBody(text, span);
    if (!body) {
      continue;
    }
    if (!hasCaptureLabel(prev.line, body, info)) {
      continue;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(body);
    } catch {
      continue;
    }
    const capture = parseCapturePayload(parsed);
    if (!capture) {
      continue;
    }

    // Remove optional "MEMORY_CAPTURE_JSON" label line immediately above the fence.
    let start = span.start;
    if (
      prev.line &&
      (prev.line.toLowerCase() === "memory_capture_json" ||
        prev.line.toLowerCase() === "postflight_capture_json" ||
        prev.line.toLowerCase().startsWith("memory_capture_json:"))
    ) {
      start = prev.start;
    }

    const end = span.end < text.length && text[span.end] === "\n" ? span.end + 1 : span.end;
    removals.push({ start, end, capture });
  }

  if (removals.length === 0) {
    return { cleanedText: text, capture: null };
  }

  removals.sort((a, b) => a.start - b.start);
  const chosen = removals[removals.length - 1]?.capture ?? null;

  let out = "";
  let cursor = 0;
  for (const removal of removals) {
    if (removal.start < cursor) {
      continue;
    }
    out += text.slice(cursor, removal.start);
    cursor = removal.end;
  }
  out += text.slice(cursor);

  // Trim excessive blank lines left behind.
  out = out.replace(/\n{3,}/g, "\n\n").trimEnd();

  return { cleanedText: out, capture: chosen };
}
