import { existsSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { homedir } from "node:os";
import { basename, dirname, extname, resolve, resolve as resolvePath } from "node:path";
import { fileURLToPath } from "node:url";
import { Spectrum, cloud } from "spectrum-ts";
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
loadEnv(resolve(process.cwd(), ".env"));

type IncomingContent = {
  type: string;
  text?: string;
  mimeType?: string;
  name?: string;
  raw?: unknown;
  read?: () => Promise<Buffer>;
};

type ImagePart = { bytes: Buffer; mimeType: string; name: string };

const IMAGE_EXTS = new Set([".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"]);

function mimeFor(path: string): string {
  const ext = extname(path).toLowerCase();
  if (ext === ".png") return "image/png";
  if (ext === ".webp") return "image/webp";
  if (ext === ".gif") return "image/gif";
  if (ext === ".heic") return "image/heic";
  return "image/jpeg";
}

function expandPath(value: string): string {
  const trimmed = value.replace(/^['"]|['"]$/g, "");
  if (trimmed.startsWith("~")) return resolvePath(homedir(), trimmed.slice(1));
  return resolvePath(trimmed);
}

function takeLocalImage(caption: string): { caption: string; image?: ImagePart } {
  const tokens = caption.match(/"[^"]+"|'[^']+'|\S+/g) ?? [];
  for (const token of tokens) {
    const filePath = expandPath(token);
    if (!IMAGE_EXTS.has(extname(filePath).toLowerCase()) || !existsSync(filePath)) continue;
    return {
      caption: caption.replace(token, " ").replace(/\s+/g, " ").trim(),
      image: {
        bytes: readFileSync(filePath),
        mimeType: mimeFor(filePath),
        name: basename(filePath),
      },
    };
  }
  return { caption };
}

const API = (process.env.SAFE_PATH_API ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const pending = new Map<string, { image_base64?: string; caption: string }>();

type SpectrumApp = Awaited<ReturnType<typeof Spectrum>>;
let spectrumApp: SpectrumApp | null = null;
let notifyFriend: string | null = process.env.PHOTON_NOTIFY_FRIEND?.trim() || null;
let tripDestination = "";
let awaitingFriendNumber = false;
const travelerName = process.env.PHOTON_TRAVELER_NAME?.trim() || "Your friend";

const GREETING = /^(hi+|hii+|hey+|hello+|howdy|yo|sup|good (morning|afternoon|evening)|thanks|thank you|thx|ok|okay|cool)[.!?]*$/i;
const HELP = /^(help|what can you do|how (does this|do you) work)\b/i;
const LEAVING = /\b(leaving|i'?m heading (out|home)|on my way|tell my friend|notify my friend)\b/i;

function isGreeting(text: string): boolean {
  return GREETING.test(text.trim());
}

function normalizePhone(raw: string): string | null {
  const digits = raw.replace(/\D/g, "");
  if (digits.length === 10) return `+1${digits}`;
  if (digits.length === 11 && digits.startsWith("1")) return `+${digits}`;
  if (raw.trim().startsWith("+") && digits.length >= 10 && digits.length <= 15) return `+${digits}`;
  return null;
}

function phoneFromText(text: string): string | null {
  const match = text.match(/(\+?\d[\d\s().-]{8,}\d)/);
  return match ? normalizePhone(match[1]) : null;
}

function rememberFriend(raw: unknown): string | null {
  if (typeof raw !== "string" || !raw.trim()) return null;
  const phone = normalizePhone(raw) ?? phoneFromText(raw);
  if (phone) {
    notifyFriend = phone;
    awaitingFriendNumber = false;
  }
  return phone;
}

async function textFriend(phone: string, body: string): Promise<void> {
  if (!spectrumApp) throw new Error("Photon is not connected");
  const im = imessage(spectrumApp);
  const user = await im.user(phone);
  const dm = await im.space.create(user);
  await dm.send(body);
}

async function notifyFriendArrived(destination: string): Promise<{ ok: boolean; reason?: string }> {
  const phone = notifyFriend;
  if (!phone) return { ok: false, reason: "no_friend" };
  const where = destination || tripDestination;
  const place = where ? ` to ${where}` : " home";
  await textFriend(
    phone,
    `Hi — ${travelerName} made it${place}. They asked me to text you when they arrived.`,
  );
  return { ok: true };
}

function json(res: import("node:http").ServerResponse, status: number, payload: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(payload));
}

function listenForArrivals(): void {
  const port = Number(process.env.PHOTON_HOOK_PORT ?? 8788);
  const server = createServer(async (req, res) => {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type");
    if (req.method === "OPTIONS") {
      res.writeHead(204);
      res.end();
      return;
    }
    const path = req.url?.split("?")[0] ?? "";
    if (req.method !== "POST" || (path !== "/arrived" && path !== "/leaving" && path !== "/checkin")) {
      res.writeHead(404);
      res.end();
      return;
    }
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    let body: { destination?: unknown; friend?: unknown; name?: unknown } = {};
    try {
      body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}") as typeof body;
    } catch {
      /* empty body is fine */
    }
    if (typeof body.destination === "string" && body.destination.trim()) {
      tripDestination = body.destination.trim();
    }
    rememberFriend(body.friend);
    if (path === "/leaving") {
      json(res, 200, { ok: true, friend: notifyFriend, destination: tripDestination });
      return;
    }
    try {
      const result = await notifyFriendArrived(tripDestination);
      json(res, result.ok ? 200 : 409, result);
    } catch (error) {
      console.error("arrive-home text to friend failed", error);
      json(res, 502, { ok: false, reason: "send_failed" });
    }
  });
  server.on("error", (error: NodeJS.ErrnoException) => {
    if (error.code === "EADDRINUSE") {
      console.warn(
        `Port ${port} is already in use (another Photon process). iMessage still works; stop the other process if you need this one to own the check-in hook.`,
      );
      return;
    }
    console.error("Photon check-in hook failed:", error);
  });
  server.listen(port, "127.0.0.1", () => {
    console.log(`Photon friend check-in hook on http://127.0.0.1:${port}/leaving and /arrived`);
  });
}

async function collect(content: IncomingContent, acc: { text: string[]; images: ImagePart[] }): Promise<void> {
  if (content.type === "text" && content.text) {
    acc.text.push(content.text);
    return;
  }
  const mime = content.mimeType?.toLowerCase() ?? "";
  const name = content.name ?? "photo.jpg";
  const looksLikeImage =
    mime.startsWith("image/") ||
    mime.includes("jpeg") ||
    mime.includes("heic") ||
    mime.includes("png") ||
    mime.includes("webp") ||
    IMAGE_EXTS.has(extname(name).toLowerCase());
  if (content.type === "attachment" && looksLikeImage && content.read) {
    acc.images.push({
      bytes: await content.read(),
      mimeType: mime.startsWith("image/") ? content.mimeType! : mimeFor(name),
      name,
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
  let caption = acc.text.join("\n").trim();
  if (acc.images.length === 0 && caption) {
    const local = takeLocalImage(caption);
    caption = local.caption;
    if (local.image) acc.images.push(local.image);
  }
  const held = pending.get(space.id);

  if (acc.images.length === 0 && caption && awaitingFriendNumber) {
    const phone = phoneFromText(caption);
    if (phone) {
      notifyFriend = phone;
      awaitingFriendNumber = false;
      await space.send(`Got it — I'll text ${phone} when you arrive. I won't message you at the destination.`);
      return;
    }
    await space.send("I need their number, like 6465550100.");
    return;
  }

  if (acc.images.length === 0 && caption && LEAVING.test(caption)) {
    const phone = phoneFromText(caption) ?? notifyFriend;
    if (!phone) {
      awaitingFriendNumber = true;
      await space.send("Who should I text when you get there? Reply with their number.");
      return;
    }
    notifyFriend = phone;
    awaitingFriendNumber = false;
    await space.send(`Safe walk. I'll text ${phone} when you reach your destination.`);
    return;
  }

  if (acc.images.length === 0 && caption && held) {
    const combined = [held.caption, caption].filter(Boolean).join("\n");
    const result = await postReport({
      caption: combined,
      message_id: message.id,
      ...(held.image_base64 ? { image_base64: held.image_base64 } : {}),
    });
    if (result.status === "need_location") pending.set(space.id, { ...held, caption: combined });
    else pending.delete(space.id);
    await space.send(result.reply ?? "Thanks.");
    return;
  }

  if (acc.images.length === 0) {
    if (caption && (isGreeting(caption) || HELP.test(caption))) {
      await space.send(
        "Hi, is there something you want to report, or are you heading out? Text “leaving” plus a friend’s number and I’ll ping them when you arrive. For a street lamp, crash, or anything unsafe, send a photo.",
      );
      return;
    }
    if (caption) {
      const result = await postReport({ caption, message_id: message.id });
      if (result.status === "need_location") pending.set(space.id, { caption });
      await space.send(
        result.reply ??
          "Got it. If you can, send a photo and a cross-street so I can pin it.",
      );
      return;
    }
    await space.send(
      "Hi, how can I help you? Text “leaving 6465550100” and I’ll tell that friend when you get there. Or send a photo to report a street lamp, crash, or something unsafe.",
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
  if (result.status === "need_location" || result.status === "vision_failed" || result.status === "need_vision") {
    pending.set(space.id, { image_base64, caption });
  } else if (result.status === "stored") {
    pending.delete(space.id);
  }
  await space.send(result.reply ?? "Thanks.");
}

async function main(): Promise<void> {
  const envPath = resolve(here, ".env");
  const envHint = existsSync(envPath) ? `${statSync(envPath).size} bytes` : "missing";
  const projectId = process.env.SPECTRUM_PROJECT_ID?.trim();
  const projectSecret = process.env.SPECTRUM_PROJECT_SECRET?.trim();

  const providers = [];
  if (projectId && projectSecret) {
    try {
      await cloud.togglePlatform(projectId, projectSecret, "imessage", true);
      const tokens = await cloud.issueImessageTokens(projectId, projectSecret);
      if (tokens.type === "dedicated") {
        const lines = Object.values(tokens.numbers).filter((n): n is string => Boolean(n));
        console.log(
          lines.length
            ? `Photon dedicated iMessage line(s): ${lines.join(", ")}`
            : "Photon minted dedicated iMessage tokens, but no phone numbers were attached.",
        );
      } else {
        console.log("Photon iMessage is on the shared number pool (not a dedicated line).");
      }
    } catch (error) {
      console.warn("Could not mint Photon iMessage tokens:", error);
    }
    providers.push(imessage.config());
  }
  if (!providers.length || process.env.PHOTON_TERMINAL === "1") {
    providers.push(terminal.config());
  }

  const app =
    projectId && projectSecret
      ? await Spectrum({
          projectId,
          projectSecret,
          providers,
          options: { logLevel: "info" },
        })
      : await Spectrum({
          providers: [terminal.config()],
        });
  spectrumApp = app;

  listenForArrivals();
  console.log(
    projectId
      ? "Photon agent listening for iMessage. Keep this process running, then text +1 (628) 267-9185 again."
      : `Photon agent in terminal-only mode. photon/.env is ${envHint}. Save SPECTRUM_PROJECT_ID and SPECTRUM_PROJECT_SECRET, then restart.`,
  );

  for await (const [space, message] of app.messages) {
    const content = (message as { content?: { type?: string } }).content;
    console.log(
      `inbound ${message.platform ?? "unknown"} ${content?.type ?? "?"} ${message.id}`,
    );
    try {
      await onMessage(space, message);
    } catch (error) {
      console.error(error);
      try {
        await space.send("Sorry — something went wrong on my side. Mind sending that again in a moment?");
      } catch (sendError) {
        console.error("reply failed", sendError);
      }
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
