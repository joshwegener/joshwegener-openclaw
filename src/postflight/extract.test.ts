import { describe, expect, it } from "vitest";
import { extractPostflightCaptureFromText } from "./extract.js";

describe("extractPostflightCaptureFromText", () => {
  it("extracts labeled MEMORY_CAPTURE_JSON blocks and strips them from output", () => {
    const input = [
      "Answer text.",
      "",
      "MEMORY_CAPTURE_JSON",
      "```json",
      JSON.stringify(
        {
          postflight: {
            type: "memory_writes",
            proposals: [{ text: "Remember X" }, { text: "Prefer Y" }],
          },
        },
        null,
        2,
      ),
      "```",
      "",
      "Tail.",
    ].join("\n");

    const res = extractPostflightCaptureFromText(input);
    expect(res.cleanedText).toContain("Answer text.");
    expect(res.cleanedText).toContain("Tail.");
    expect(res.cleanedText).not.toContain("MEMORY_CAPTURE_JSON");
    expect(res.cleanedText).not.toContain('"postflight"');
    expect(res.capture?.kind).toBe("memory_writes");
    expect(
      res.capture && res.capture.kind === "memory_writes" ? res.capture.proposals.length : 0,
    ).toBe(2);
  });

  it("ignores unrelated json fences", () => {
    const input = ["Answer", "", "```json", '{\"ok\":true}', "```"].join("\n");
    const res = extractPostflightCaptureFromText(input);
    expect(res.capture).toBeNull();
    expect(res.cleanedText).toContain("```json");
  });
});
