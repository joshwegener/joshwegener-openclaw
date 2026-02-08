import fs from "node:fs/promises";
import path from "node:path";
import type { PostflightCaptureProposal } from "./types.js";

function formatLocalDateYmd(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function formatLocalTimeHms(d: Date): string {
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

async function ensureDailyMemoryFile(workspaceDir: string, dateStr: string): Promise<string> {
  const memoryDir = path.join(workspaceDir, "memory");
  await fs.mkdir(memoryDir, { recursive: true });
  const filePath = path.join(memoryDir, `${dateStr}.md`);
  try {
    await fs.stat(filePath);
  } catch {
    await fs.writeFile(filePath, `# ${dateStr}\n\n`, "utf-8");
  }
  return filePath;
}

export async function appendPostflightProposalsToDailyMemory(params: {
  workspaceDir: string;
  proposals: PostflightCaptureProposal[];
  now?: Date;
}): Promise<{ filePath: string; appended: number }> {
  const now = params.now ?? new Date();
  const dateStr = formatLocalDateYmd(now);
  const timeStr = formatLocalTimeHms(now);
  const filePath = await ensureDailyMemoryFile(params.workspaceDir, dateStr);

  const existing = await fs.readFile(filePath, "utf-8").catch(() => "");
  const seen = new Set(existing.split("\n").map((line) => line.trim()));

  const lines: string[] = [];
  lines.push("");
  lines.push(`## Postflight Capture (${timeStr})`);
  let appended = 0;
  for (const p of params.proposals) {
    const text = p.text.trim();
    if (!text) {
      continue;
    }
    const bullet = `- ${text}`;
    if (seen.has(bullet)) {
      continue;
    }
    lines.push(bullet);
    appended += 1;
  }
  lines.push("");

  if (appended === 0) {
    return { filePath, appended: 0 };
  }

  await fs.appendFile(filePath, lines.join("\n"), "utf-8");
  return { filePath, appended };
}
