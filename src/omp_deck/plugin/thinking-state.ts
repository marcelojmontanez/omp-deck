import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

type JsonObject = Record<string, unknown>;
const THINKING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max"] as const;
const THINKING_LEVELS = ["off", ...THINKING_EFFORTS] as const;
type DeckThinkingLevel = (typeof THINKING_LEVELS)[number];

export interface ThinkingState {
  thinking: DeckThinkingLevel | null;
  thinkingLevels: DeckThinkingLevel[];
}

export function readThinkingState(pi: ExtensionAPI, context: ExtensionContext): ThinkingState {
  const model = context.model;
  const efforts: readonly string[] | undefined = model?.reasoning ? model.thinking?.efforts : undefined;
  const thinkingLevels: DeckThinkingLevel[] = efforts
    ? ["off", ...THINKING_EFFORTS.filter(level => efforts.includes(level))]
    : [];
  const current = pi.getThinkingLevel();
  const thinking = THINKING_LEVELS.includes(current as DeckThinkingLevel) ? current as DeckThinkingLevel : null;
  return { thinking, thinkingLevels };
}

export function setThinking(pi: ExtensionAPI, context: ExtensionContext, value: unknown): ThinkingState {
  const before = readThinkingState(pi, context);
  if (typeof value !== "string" || !before.thinkingLevels.includes(value as DeckThinkingLevel)) {
    throw new Error("unsupported thinking level");
  }
  pi.setThinkingLevel(value as Parameters<ExtensionAPI["setThinkingLevel"]>[0]);
  return readThinkingState(pi, context);
}

export function degradedSnapshot(data: unknown): JsonObject {
  const snapshot = data && typeof data === "object" ? data as JsonObject : {};
  const advertised = Array.isArray(snapshot.thinkingLevels) ? snapshot.thinkingLevels : [];
  const thinkingLevels = THINKING_LEVELS.filter(level => advertised.includes(level));
  const thinking = THINKING_LEVELS.includes(snapshot.thinking as DeckThinkingLevel)
    ? snapshot.thinking as DeckThinkingLevel
    : null;
  return {
    idle: snapshot.idle === true,
    pendingMessages: snapshot.pendingMessages === true,
    model: null,
    async: {
      running: [],
      recent: [],
      delivery: { queued: 0, delivering: false, pendingJobIds: [] },
    },
    thinking,
    thinkingLevels,
  };
}
