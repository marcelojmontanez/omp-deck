import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { MAX_IMAGE_BYTES, MAX_IMAGES } from "./image-content.ts";

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
