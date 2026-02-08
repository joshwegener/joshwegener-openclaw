import type { IncomingMessage, ServerResponse } from "node:http";
import { isTruthyEnvValue } from "../infra/env.js";
import { authorizeGatewayConnect, type ResolvedGatewayAuth } from "./auth.js";
import { sendJson, sendMethodNotAllowed, sendUnauthorized } from "./http-common.js";
import { getBearerToken } from "./http-utils.js";

type SettingsHttpOptions = {
  auth: ResolvedGatewayAuth;
  trustedProxies?: string[];
};

type MemoryCaptureMode = "off" | "suggest";

function normalizeMode(raw: unknown): MemoryCaptureMode {
  const val = typeof raw === "string" ? raw.trim().toLowerCase() : "";
  if (val === "off") {
    return "off";
  }
  if (val === "suggest") {
    return "suggest";
  }
  return "suggest";
}

export async function handleSettingsHttpRequest(
  req: IncomingMessage,
  res: ServerResponse,
  opts: SettingsHttpOptions,
): Promise<boolean> {
  const url = new URL(req.url ?? "/", `http://${req.headers.host || "localhost"}`);
  if (url.pathname !== "/v1/settings") {
    return false;
  }

  if (req.method !== "GET") {
    sendMethodNotAllowed(res, "GET");
    return true;
  }

  const token = getBearerToken(req);
  const authResult = await authorizeGatewayConnect({
    auth: opts.auth,
    connectAuth: { token, password: token },
    req,
    trustedProxies: opts.trustedProxies,
  });
  if (!authResult.ok) {
    sendUnauthorized(res);
    return true;
  }

  const mode = normalizeMode(process.env.OPENCLAW_MEMORY_CAPTURE_MODE);
  const debug = isTruthyEnvValue(process.env.OPENCLAW_MEMORY_CAPTURE_DEBUG);

  sendJson(res, 200, {
    memory_capture_mode: mode,
    memory_capture_debug: debug,
  });
  return true;
}
