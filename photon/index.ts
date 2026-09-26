import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Spectrum } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";
import { terminal } from "spectrum-ts/providers/terminal";

function loadEnv(path: string): void {
  try {
    for (const line of readFileSync(path, "utf8").split("\n")) {
      const stripped = line.trim();
      if (!stripped || stripped.startsWith("#") || !stripped.includes("=")) continue;
      const eq = stripped.indexOf("=");
      const key = stripped.slice(0, eq).trim();
      const value = stripped.slice(eq + 1).trim().replace(/^["']|["']$/g, "");
      if (!(key in process.env)) process.env[key] = value;
    }
  } catch {
    /* optional local env */
  }
}

const here = dirname(fileURLToPath(import.meta.url));
loadEnv(resolve(here, ".env"));

type IncomingContent = {
  type: string;
  text?: string;
  mimeType?: string;
  name?: string;
  raw?: unknown;
  read?: () => Promise<Buffer>;
};

type ImagePart = { bytes: Buffer; mimeType: string; name: string };

const API = (process.env.SAFE_PATH_API ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const pending = new Map<string, { image_base64: string; caption: string }>();

async function collect(content: IncomingContent, acc: { text: string[]; images: ImagePart[] }): Promise<void> {
  if (content.type === "text" && content.text) {
    acc.text.push(content.text);
    return;
  }
  if (content.type === "attachment" && content.mimeType?.startsWith("image/") && content.read) {
    acc.images.push({
      bytes: await content.read(),
      mimeType: content.mimeType,
      name: content.name ?? "photo.jpg",
    });
    return;
  }
  if (content.type === "custom" && content.raw && typeof content.raw === "object") {
    const raw = content.raw as Record<string, unknown>;
    if (typeof raw.text === "string") acc.text.push(raw.text);
  }
}

async function postReport(body: {
  caption?: string;
  message_id?: string;
  image_base64?: string;
}): Promise<{ reply?: string; status?: string }> {
  const response = await fetch(`${API}/reports`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.text();
    return { status: "error", reply: `Could not log that report (${response.status}): ${detail.slice(0, 180)}` };
  }
  return (await response.json()) as { reply?: string; status?: string };
}

async function onMessage(
  space: { id: string; send: (...content: [string, ...string[]]) => Promise<void> },
  message: { id: string; direction?: string; content: IncomingContent },
): Promise<void> {
  if (message.direction === "outbound") return;

  const acc = { text: [] as string[], images: [] as ImagePart[] };
  await collect(message.content, acc);
  const caption = acc.text.join("\n").trim();
  const held = pending.get(space.id);

  if (acc.images.length === 0 && caption && held) {
    const result = await postReport({
      caption,
      message_id: message.id,
      image_base64: held.image_base64,
    });
    if (result.status === "stored") pending.delete(space.id);
    await space.send(result.reply ?? "Thanks.");
    return;
  }

  if (acc.images.length === 0) {
    if (caption) {
      const result = await postReport({ caption, message_id: message.id });
      await space.send(
        result.reply ??
          "Send a photo of a street lamp out, crash, or crime. Add a cross-street if the picture has no GPS.",
      );
      return;
    }
    await space.send(
      "Send a photo of a broken street lamp, crash, or something in our seven crime categories, plus a cross-street if you can.",
    );
    return;
  }

  const image = acc.images[0];
  const image_base64 = image.bytes.toString("base64");
  const result = await postReport({
    caption: caption || undefined,
    message_id: message.id,
    image_base64,
  });
  if (result.status === "need_location") {
    pending.set(space.id, { image_base64, caption });
  } else if (result.status === "stored") {
    pending.delete(space.id);
  }
  await space.send(result.reply ?? "Thanks.");
}

async function main(): Promise<void> {
  const projectId = process.env.SPECTRUM_PROJECT_ID;
  const projectSecret = process.env.SPECTRUM_PROJECT_SECRET;
  const app =
    projectId && projectSecret
      ? await Spectrum({
          projectId,
          projectSecret,
          providers: [imessage.config({ local: false }), terminal.config()],
        })
      : await Spectrum({
          providers: [terminal.config()],
        });

  console.log(
    projectId
      ? "Photon agent listening for iMessage photos (terminal is also on)."
      : "Photon agent in terminal-only mode. Set SPECTRUM_PROJECT_ID and SPECTRUM_PROJECT_SECRET for iMessage.",
  );

  for await (const [space, message] of app.messages) {
    try {
      await onMessage(space, message);
    } catch (error) {
      console.error(error);
      try {
        await space.send("Something went wrong logging that. Try again in a moment.");
      } catch {
        /* ignore send failures */
      }
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
