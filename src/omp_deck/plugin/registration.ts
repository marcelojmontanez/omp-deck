import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { MAX_IMAGE_BYTES, MAX_IMAGES } from "./image-content.ts";
import { readThinkingState } from "./thinking-state.ts";

export const REGISTRATION_ATTACHMENT_LIMITS = {
  maxImages: MAX_IMAGES,
  maxTotalBytes: MAX_IMAGE_BYTES,
} as const;

export const REGISTRATION_CAPABILITIES = {
  sendMessage: true,
  followUp: true,
  interrupt: true,
  shutdown: true,
  todos: true,
  subagents: true,
  attachments: true,
  setThinking: true,
  answerUiRequest: false,
  answer_ui_request: false,
} as const;

export const sessionIdentity = (context: ExtensionContext): { id: string; file: string | null; title: string | null } => ({
  id: context.sessionManager.getSessionId(),
  file: context.sessionManager.getSessionFile() || null,
  title: context.sessionManager.getSessionName() || null,
});

export function extensionRegistration(
  pi: ExtensionAPI,
  context: ExtensionContext,
  connectionId: string,
): Record<string, unknown> {
  const session = sessionIdentity(context);
  return {
    protocol: "omp-deck-fallback-v1",
    version: 1,
    type: "registration",
    ompVersion: pi.pi.VERSION,
    compatible: true,
    connectionId,
    session,
    sessionId: session.id,
    title: session.title,
    project: context.cwd,
    thinkingLevels: readThinkingState(pi, context).thinkingLevels,
    attachmentLimits: REGISTRATION_ATTACHMENT_LIMITS,
    capabilities: REGISTRATION_CAPABILITIES,
  };
}
