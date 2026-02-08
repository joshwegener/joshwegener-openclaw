export type PostflightCaptureMode = "off" | "suggest";

export type PostflightCaptureSettings = {
  mode: PostflightCaptureMode;
  debug: boolean;
  source: "gateway" | "env" | "default";
};

export type PostflightCaptureProposal = {
  id: string;
  text: string;
};

export type PostflightCapturePayload =
  | {
      kind: "no_memory_capture";
      reason?: string;
      rawJson: string;
    }
  | {
      kind: "memory_writes";
      proposals: PostflightCaptureProposal[];
      rawJson: string;
    };
