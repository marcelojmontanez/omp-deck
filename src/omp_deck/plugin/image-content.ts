import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const MAX_TEXT_BYTES = 512 * 1024;
export const MAX_IMAGES = 4;
export const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const MAX_ENCODED_IMAGE_CHARS = Math.ceil(MAX_IMAGE_BYTES / 3) * 4;
const MIME_TYPE = /^image\/[A-Za-z0-9.+-]{1,64}$/;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

type UserMessageContent = Parameters<ExtensionAPI["sendUserMessage"]>[0];
type UserMessagePart = Exclude<UserMessageContent, string>[number];

export interface NormalizedInlineImage {
  content: UserMessagePart;
  data: Buffer;
  bytes: number;
}

function detectedMimeType(data: Buffer): string | undefined {
  if (data.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)) {
    return "image/png";
  }
  if (data[0] === 0xff && data[1] === 0xd8 && data[2] === 0xff) return "image/jpeg";
  const prefix = data.subarray(0, 6).toString("ascii");
  if (prefix === "GIF87a" || prefix === "GIF89a") return "image/gif";
  if (data.length >= 12 && data.subarray(0, 4).toString("ascii") === "RIFF"
    && data.subarray(8, 12).toString("ascii") === "WEBP") return "image/webp";
  return undefined;
}

export function normalizeInlineImage(value: unknown): NormalizedInlineImage {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid image");
  const image = value as Record<string, unknown>;
  if (image.type !== "image" || typeof image.data !== "string" || typeof image.mimeType !== "string") {
    throw new Error("invalid image");
  }
  if (
    image.data.length === 0
    || image.data.length > MAX_ENCODED_IMAGE_CHARS
    || !MIME_TYPE.test(image.mimeType)
  ) {
    throw new Error("invalid image");
  }
  const decoded = Buffer.from(image.data, "base64");
  if (decoded.toString("base64") !== image.data) throw new Error("invalid image");
  if (detectedMimeType(decoded) !== image.mimeType) throw new Error("invalid image");
  return {
    content: { type: "image", data: image.data, mimeType: image.mimeType },
    data: decoded,
    bytes: decoded.length,
  };
}

function imageContent(value: unknown): NormalizedInlineImage {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid images");
  const keys = Object.keys(value);
  if (keys.length !== 3 || !keys.includes("type") || !keys.includes("data") || !keys.includes("mimeType")) {
    throw new Error("invalid image");
  }
  return normalizeInlineImage(value);
}

function userMessageContent(message: unknown, images: unknown): UserMessageContent {
  if (typeof message !== "string" || Buffer.byteLength(message) > MAX_TEXT_BYTES) {
    throw new Error("invalid message");
  }
  let imageValues: unknown[] = [];
  if (images !== undefined) {
    if (!Array.isArray(images) || images.length > MAX_IMAGES) throw new Error("invalid images");
    imageValues = images;
  }
  const content: UserMessagePart[] = [];
  if (message.length > 0) content.push({ type: "text", text: message });
  let decodedBytes = 0;
  for (const value of imageValues) {
    const image = imageContent(value);
    decodedBytes += image.bytes;
    if (decodedBytes > MAX_IMAGE_BYTES) throw new Error("images exceed 20 MiB");
    content.push(image.content);
  }
  if (content.length === 0) throw new Error("message content is empty");
  return content.length === 1 && content[0].type === "text" ? message : content;
}

export function deliverUserMessage(
  pi: ExtensionAPI,
  context: ExtensionContext,
  request: Record<string, unknown>,
): { acceptance: "accepted" } {
  const content = userMessageContent(request.message, request.images);
  const options = request.action === "followUp" && !context.isIdle()
    ? { deliverAs: "followUp" as const }
    : undefined;
  pi.sendUserMessage(content, options);
  return { acceptance: "accepted" };
}
