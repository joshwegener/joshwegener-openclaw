import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { appendPostflightProposalsToDailyMemory } from "./save.js";

describe("appendPostflightProposalsToDailyMemory", () => {
  it("creates daily file and appends proposals", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "openclaw-postflight-"));
    try {
      const now = new Date(2026, 1, 8, 9, 10, 11);
      const res = await appendPostflightProposalsToDailyMemory({
        workspaceDir: dir,
        proposals: [
          { id: "a", text: "Remember A" },
          { id: "b", text: "Remember B" },
        ],
        now,
      });
      expect(res.appended).toBe(2);
      const file = await fs.readFile(res.filePath, "utf-8");
      expect(file).toContain("# 2026-02-08");
      expect(file).toContain("## Postflight Capture");
      expect(file).toContain("- Remember A");
      expect(file).toContain("- Remember B");
    } finally {
      await fs.rm(dir, { recursive: true, force: true });
    }
  });
});
