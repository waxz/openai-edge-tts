// OpenAI-compatible TTS API on Cloudflare Workers, backed by Microsoft Edge TTS
// (and, optionally, the openai.fm demo site).
//
//   POST /v1/audio/speech            OpenAI "Create speech"
//   GET  /v1/models                  OpenAI "List models"
//   GET  /v1/models/{model}          OpenAI "Retrieve model"
//   GET  /v1/audio/voices            extension: list available voices
//   POST /openai-fm/v1/audio/speech  legacy route, forces the openai.fm backend
//
// Set the key with `wrangler secret put API_KEY` (comma-separate several keys).
// When API_KEY is empty the API is open to everyone.
//
// Edge TTS is reached through two free endpoints: the Microsoft Translator app's token
// endpoint (primary: every output format and speaking styles) and the Edge browser's
// Read Aloud WebSocket (fallback: mp3 only, no styles). EDGE_ENDPOINT selects
// "auto" (default: translator, then read aloud on failure), "translator" or "readaloud".
//
// Optional: KOKORO_URL points at the Kokoro Space in hf-space/ (e.g. https://user-kokoro.hf.space),
// with KOKORO_API_KEY if that Space has an API_KEY secret. Requests with model "kokoro" or a
// Kokoro voice name (af_heart, zf_xiaoxiao, zf_001, ...) are then forwarded to it.

export interface Env {
    API_KEY?: string;
    EDGE_ENDPOINT?: string;
    KOKORO_URL?: string;
    KOKORO_API_KEY?: string;
}

type EdgeEndpoint = "auto" | "translator" | "readaloud";

type Provider = "edge" | "openai-fm" | "kokoro";
type ResponseFormat = "mp3" | "opus" | "aac" | "flac" | "wav" | "pcm";

interface SpeechRequest {
    model: string;
    provider: Provider;
    input: string;
    voice: string;
    instructions?: string;
    response_format: ResponseFormat;
    speed: number;
    stream_format: "audio" | "sse";
    edgeEndpoint: EdgeEndpoint;
    kokoro: { url: string; key?: string } | null;
    // Edge-only extensions, kept for the web UI and existing callers
    volume: number;
    pitch: number;
    style: string;
}

type ChunkSynthesizer = (text: string, req: SpeechRequest) => Promise<Uint8Array>;

// Keeps a request well under the 50-subrequest limit of the Workers free plan.
const MAX_INPUT_CHARS = 50_000;
const EDGE_CHUNK_CHARS = 2000;
const OPENAI_FM_CHUNK_CHARS = 900;
// Small pieces get the first audio back sooner from a CPU-only Space.
const KOKORO_CHUNK_CHARS = 300;
// Kokoro voice ids: language letter, gender letter, name (af_heart, zf_xiaoxiao, zm_100, ...).
// Edge voice names never contain an underscore.
const KOKORO_VOICE_RE = /^[abefhijpz][fm]_[a-z0-9]+$/;
const SYNTHESIS_CONCURRENCY = 3;
const TOKEN_REFRESH_BEFORE_EXPIRY = 3 * 60;
const UPSTREAM_TIMEOUT_MS = 30_000;
const VOICES_CACHE_SECONDS = 6 * 3600;
const USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0";

// Edge Read Aloud constants, kept in sync with https://github.com/rany2/edge-tts (constants.py)
const READALOUD_BASE = "speech.platform.bing.com/consumer/speech/synthesize/readaloud";
const READALOUD_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4";
const READALOUD_CHROMIUM_VERSION = "143.0.3650.75";
const READALOUD_MAJOR = READALOUD_CHROMIUM_VERSION.split(".")[0];
const READALOUD_HEADERS: Record<string, string> = {
    "User-Agent": `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${READALOUD_MAJOR}.0.0.0 Safari/537.36 Edg/${READALOUD_MAJOR}.0.0.0`,
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9",
    Pragma: "no-cache",
    "Cache-Control": "no-cache",
    Origin: "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
};
const READALOUD_FORMAT = "audio-24khz-48kbitrate-mono-mp3";
// The service drops SSML messages over ~4 KB; 1000 chars stays below that even for CJK text.
const READALOUD_CHUNK_CHARS = 1000;

const MODELS: Record<string, Provider> = {
    "tts-1": "edge",
    "tts-1-hd": "edge",
    "edge-tts": "edge",
    "gpt-4o-mini-tts": "openai-fm",
    "openai-fm": "openai-fm",
    kokoro: "kokoro",
};
const MODEL_OWNERS: Record<Provider, string> = { edge: "microsoft-edge-tts", "openai-fm": "openai-fm", kokoro: "hexgrad" };
const MODEL_CREATED = 1699046015;

// OpenAI voice names -> Edge voices. The multilingual ones also read Chinese and other languages.
const OPENAI_VOICE_TO_EDGE: Record<string, string> = {
    alloy: "en-US-AvaMultilingualNeural",
    ash: "en-US-AndrewMultilingualNeural",
    ballad: "en-GB-RyanNeural",
    coral: "en-US-EmmaMultilingualNeural",
    echo: "en-US-BrianMultilingualNeural",
    fable: "en-GB-SoniaNeural",
    onyx: "en-US-ChristopherNeural",
    nova: "en-US-JennyNeural",
    sage: "en-US-MichelleNeural",
    shimmer: "en-US-AriaNeural",
    verse: "en-US-GuyNeural",
    marin: "en-US-AvaNeural",
    cedar: "en-US-AndrewNeural",
};
const OPENAI_VOICES = Object.keys(OPENAI_VOICE_TO_EDGE);
const DEFAULT_VOICE = "alloy";

// response_format -> Edge output format. Edge has no AAC or FLAC encoder.
// WAV is synthesized as raw PCM so multi-chunk audio gets one correct header.
const EDGE_FORMATS: Partial<Record<ResponseFormat, string>> = {
    mp3: "audio-24khz-48kbitrate-mono-mp3",
    opus: "ogg-24khz-16bit-mono-opus",
    wav: "raw-24khz-16bit-mono-pcm",
    pcm: "raw-24khz-16bit-mono-pcm",
};
const EDGE_HD_MP3 = "audio-48khz-192kbitrate-mono-mp3";
const PCM_SAMPLE_RATE = 24000;

const CONTENT_TYPES: Record<ResponseFormat, string> = {
    mp3: "audio/mpeg",
    opus: "audio/ogg",
    aac: "audio/aac",
    flac: "audio/flac",
    wav: "audio/wav",
    pcm: "audio/pcm",
};

const OPENAI_FM_DEFAULT_PROMPT = `Voice Affect: Calm, composed, and reassuring; project quiet authority and confidence, BBC reporter host accent.

Tone: Sincere, empathetic, and gently authoritative—express genuine apology while conveying competence.

Pacing: Steady and moderate; unhurried enough to communicate care, yet efficient enough to demonstrate professionalism.

Emotion: Genuine empathy and understanding; speak with warmth, especially during apologies ("I'm very sorry for any disruption...").

Pronunciation: Clear and precise, emphasizing key reassurances ("smoothly," "quickly," "promptly") to reinforce confidence.

Pauses: Brief pauses after offering assistance or requesting details, highlighting willingness to listen and support.`;

let tokenInfo: { endpoint: any; token: string | null; expiredAt: number | null } = {
    endpoint: null,
    token: null,
    expiredAt: null,
};
let voicesCache: { at: number; voices: any[] } | null = null;

class ApiError extends Error {
    constructor(
        public status: number,
        message: string,
        public type = "invalid_request_error",
        public param: string | null = null,
        public code: string | null = null,
    ) {
        super(message);
    }
}

export default {
    async fetch(request: Request, env: Env): Promise<Response> {
        try {
            return await handleRequest(request, env);
        } catch (error) {
            if (error instanceof ApiError) return errorResponse(error);
            console.error("Unhandled error:", error);
            return errorResponse(new ApiError(500, String((error as Error)?.message ?? error), "server_error"));
        }
    },
};

// ---------------------------------------------------------------- routing

async function handleRequest(request: Request, env: Env): Promise<Response> {
    if (request.method === "OPTIONS") {
        return handleOptions(request);
    }

    const path = new URL(request.url).pathname.replace(/\/+$/, "");
    const method = request.method === "HEAD" ? "GET" : request.method;

    const routes: Array<[string, RegExp, (m: RegExpMatchArray) => Promise<Response>]> = [
        ["POST", /^\/v1\/audio\/speech$/, () => handleSpeech(request, env, null)],
        ["POST", /^\/openai-fm\/v1\/audio\/speech$/, () => handleSpeech(request, env, "openai-fm")],
        ["GET", /^\/v1\/models$/, async () => jsonResponse({ object: "list", data: Object.keys(MODELS).map(modelObject) })],
        ["GET", /^\/v1\/models\/([^/]+)$/, async (m) => handleRetrieveModel(decodeURIComponent(m[1]))],
        ["GET", /^\/v1\/audio\/voices$/, () => handleListVoices(request, env)],
    ];

    let pathMatched = false;
    for (const [routeMethod, pattern, handler] of routes) {
        const m = path.match(pattern);
        if (!m) continue;
        pathMatched = true;
        if (routeMethod === method) {
            authenticate(request, env);
            return handler(m);
        }
    }
    if (pathMatched) {
        throw new ApiError(405, `Method ${request.method} is not allowed for ${path}.`, "invalid_request_error", null, "method_not_allowed");
    }
    throw new ApiError(404, `Invalid URL (${request.method} ${path})`, "invalid_request_error", null, "unknown_url");
}

function authenticate(request: Request, env: Env) {
    const keys = (env.API_KEY || "").split(",").map((k) => k.trim()).filter(Boolean);
    if (keys.length === 0) return;

    const auth = request.headers.get("authorization") || "";
    const provided = auth.match(/^Bearer\s+(.+)$/i)?.[1]?.trim() || request.headers.get("x-api-key")?.trim();
    if (!provided) {
        throw new ApiError(401, "You didn't provide an API key. You need to provide your API key in an Authorization header using Bearer auth (i.e. Authorization: Bearer YOUR_KEY).", "invalid_request_error", null, "missing_api_key");
    }
    if (!keys.includes(provided)) {
        throw new ApiError(401, "Incorrect API key provided.", "invalid_request_error", null, "invalid_api_key");
    }
}

function handleOptions(request: Request): Response {
    return new Response(null, {
        status: 204,
        headers: {
            ...corsHeaders(),
            "Access-Control-Allow-Headers": request.headers.get("Access-Control-Request-Headers") || "Authorization, Content-Type, x-api-key",
        },
    });
}

// ---------------------------------------------------------------- models & voices

function parseEdgeEndpoint(env: Env): EdgeEndpoint {
    const value = (env.EDGE_ENDPOINT || "auto").toLowerCase();
    return value === "translator" || value === "readaloud" ? value : "auto";
}

function modelObject(id: string) {
    return { id, object: "model", created: MODEL_CREATED, owned_by: MODEL_OWNERS[MODELS[id]] };
}

async function handleRetrieveModel(id: string): Promise<Response> {
    if (!(id in MODELS)) {
        throw new ApiError(404, `The model '${id}' does not exist or you do not have access to it.`, "invalid_request_error", "model", "model_not_found");
    }
    return jsonResponse(modelObject(id));
}

// GET /v1/audio/voices?model=tts-1&locale=zh-CN
async function handleListVoices(request: Request, env: Env): Promise<Response> {
    const params = new URL(request.url).searchParams;
    const model = params.get("model") || "tts-1";
    const locale = params.get("locale")?.toLowerCase();

    if (MODELS[model] === "kokoro") {
        const kokoro = kokoroConfig(env);
        const response = await fetchWithTimeout(`${kokoro.url}/v1/audio/voices?${params}`, { headers: kokoroHeaders(kokoro) }, KOKORO_TIMEOUT_MS);
        if (!response.ok) await throwKokoroError(response);
        return jsonResponse(await response.json());
    }
    if (MODELS[model] === "openai-fm") {
        return jsonResponse({ object: "list", data: OPENAI_VOICES.map((id) => ({ id, object: "voice", name: id })) });
    }

    const aliases = Object.entries(OPENAI_VOICE_TO_EDGE).map(([id, target]) => ({ id, object: "voice", name: id, alias_of: target }));
    let voices = (await getEdgeVoices(parseEdgeEndpoint(env))).map((v) => ({
        id: v.ShortName,
        object: "voice",
        name: v.LocalName || v.DisplayName || v.FriendlyName,
        gender: v.Gender?.toLowerCase(),
        locale: v.Locale,
        locale_name: v.LocaleName,
        styles: v.StyleList || [],
    }));
    if (locale) {
        voices = voices.filter((v) => v.locale?.toLowerCase().startsWith(locale));
    }
    return jsonResponse({ object: "list", data: locale ? voices : [...aliases, ...voices] });
}

async function getEdgeVoices(edgeEndpoint: EdgeEndpoint): Promise<any[]> {
    const now = Date.now() / 1000;
    if (voicesCache && now - voicesCache.at < VOICES_CACHE_SECONDS) return voicesCache.voices;

    let voices: any[];
    try {
        if (edgeEndpoint === "readaloud") throw new Error("translator endpoint disabled");
        const endpoint = await getEndpoint();
        const response = await fetchWithTimeout(`https://${endpoint.r}.tts.speech.microsoft.com/cognitiveservices/voices/list`, {
            headers: { Authorization: endpoint.t, "User-Agent": USER_AGENT },
        });
        if (!response.ok) {
            throw new ApiError(502, `Edge TTS voice list error: ${response.status} ${await response.text()}`, "server_error", null, "upstream_error");
        }
        voices = await response.json();
    } catch (error) {
        if (edgeEndpoint === "translator") throw error;
        console.warn("Translator voice list failed, falling back to Edge Read Aloud:", error);
        voices = await getReadAloudVoices();
    }
    voicesCache = { at: now, voices };
    return voices;
}

// ---------------------------------------------------------------- speech

async function handleSpeech(request: Request, env: Env, forcedProvider: Provider | null): Promise<Response> {
    const req = await parseSpeechRequest(request, forcedProvider);
    req.edgeEndpoint = parseEdgeEndpoint(env);
    if (req.provider === "kokoro") req.kokoro = kokoroConfig(env);

    const synthesizers: Record<Provider, [ChunkSynthesizer, number]> = {
        edge: [getEdgeAudioChunk, EDGE_CHUNK_CHARS],
        "openai-fm": [getOpenaiFmAudioChunk, OPENAI_FM_CHUNK_CHARS],
        // FLAC streams can't be concatenated, so FLAC is synthesized in one piece.
        kokoro: [getKokoroAudioChunk, req.response_format === "flac" ? MAX_INPUT_CHARS : KOKORO_CHUNK_CHARS],
    };
    const [synthesize, maxChunk] = synthesizers[req.provider];
    const chunks = splitText(req.input, maxChunk);

    // Synthesize the first chunk before responding, so upstream failures still produce a proper error status.
    const first = await synthesize(chunks[0], req);
    const rest = chunks.slice(1);
    // Later chunks are synthesized a few at a time ahead of playback, but emitted in order.
    const remaining = async function* () {
        yield first;
        const queue: Promise<Uint8Array>[] = [];
        let next = 0;
        const fill = () => {
            while (queue.length < SYNTHESIS_CONCURRENCY && next < rest.length) {
                const p = synthesize(rest[next++], req);
                p.catch(() => {}); // rejection is surfaced when awaited below
                queue.push(p);
            }
        };
        fill();
        while (queue.length) {
            const p = queue.shift()!;
            fill();
            yield await p;
        }
    };

    if (req.stream_format === "sse") {
        return sseResponse(remaining(), req.input.length);
    }

    const headers: Record<string, string> = { ...corsHeaders(), "Content-Type": CONTENT_TYPES[req.response_format] };
    if (req.provider === "openai-fm") {
        // openai.fm decides the container itself; just pass audio through
        headers["Content-Type"] = "audio/mpeg";
    }

    if (req.response_format === "wav") {
        // Buffer so the RIFF header carries the real length.
        const parts: Uint8Array[] = [];
        for await (const part of remaining()) parts.push(part);
        const pcm = concatBytes(parts);
        return new Response(concatBytes([wavHeader(pcm.length), pcm]), { headers });
    }

    // Stream every other format chunk by chunk.
    const { readable, writable } = new TransformStream<Uint8Array, Uint8Array>();
    (async () => {
        const writer = writable.getWriter();
        try {
            for await (const part of remaining()) await writer.write(part);
            await writer.close();
        } catch (error) {
            console.error("Streaming synthesis failed:", error);
            await writer.abort(error);
        }
    })();
    return new Response(readable, { headers });
}

async function parseSpeechRequest(request: Request, forcedProvider: Provider | null): Promise<SpeechRequest> {
    let body: any;
    try {
        body = await request.json();
    } catch {
        throw new ApiError(400, "We could not parse the JSON body of your request. (HINT: This likely means you aren't using your HTTP library correctly. The OpenAI API expects a JSON payload.)");
    }
    if (!body || typeof body !== "object") {
        throw new ApiError(400, "Request body must be a JSON object.");
    }

    const model = body.model ?? "tts-1";
    if (typeof model !== "string") {
        throw new ApiError(400, "'model' must be a string.", "invalid_request_error", "model");
    }
    // Unknown model names fall back to Edge, since many clients hard-code their own.
    // A Kokoro voice name selects Kokoro whatever the model.
    const isKokoroVoice = typeof body.voice === "string" && KOKORO_VOICE_RE.test(body.voice.trim());
    const provider: Provider = forcedProvider ?? (isKokoroVoice ? "kokoro" : MODELS[model] ?? "edge");

    const input = body.input;
    if (typeof input !== "string" || !input.trim()) {
        throw new ApiError(400, "Missing required parameter: 'input'.", "invalid_request_error", "input", "missing_required_parameter");
    }
    if (input.length > MAX_INPUT_CHARS) {
        throw new ApiError(400, `'input' is too long: ${input.length} characters, maximum is ${MAX_INPUT_CHARS}.`, "invalid_request_error", "input", "string_above_max_length");
    }

    const voice = body.voice ?? DEFAULT_VOICE;
    if (typeof voice !== "string" || !voice.trim()) {
        throw new ApiError(400, "'voice' must be a non-empty string.", "invalid_request_error", "voice");
    }
    if (provider === "openai-fm" && !OPENAI_VOICES.includes(voice.toLowerCase())) {
        throw new ApiError(400, `Invalid value for 'voice': '${voice}'. Supported values are: ${OPENAI_VOICES.map((v) => `'${v}'`).join(", ")}.`, "invalid_request_error", "voice", "invalid_value");
    }

    const response_format = (body.response_format ?? "mp3") as ResponseFormat;
    if (!(response_format in CONTENT_TYPES)) {
        throw new ApiError(400, `Invalid value for 'response_format': '${response_format}'. Supported values are: 'mp3', 'opus', 'wav' and 'pcm'.`, "invalid_request_error", "response_format", "invalid_value");
    }
    if (provider === "kokoro" && response_format === "aac") {
        throw new ApiError(400, "response_format 'aac' is not supported by Kokoro. Use 'mp3', 'opus', 'flac', 'wav' or 'pcm'.", "invalid_request_error", "response_format", "unsupported_value");
    }
    if (provider === "edge" && !EDGE_FORMATS[response_format]) {
        throw new ApiError(400, `response_format '${response_format}' is not supported by this server. Use 'mp3', 'opus', 'wav' or 'pcm'.`, "invalid_request_error", "response_format", "unsupported_value");
    }
    if (provider === "openai-fm" && response_format !== "mp3") {
        throw new ApiError(400, "The openai.fm backend only returns 'mp3'.", "invalid_request_error", "response_format", "unsupported_value");
    }

    const speed = Number(body.speed ?? 1.0);
    if (!Number.isFinite(speed) || speed < 0.25 || speed > 4.0) {
        throw new ApiError(400, `Invalid 'speed': ${body.speed}. Expected a number between 0.25 and 4.0.`, "invalid_request_error", "speed", "invalid_value");
    }

    const stream_format = body.stream_format ?? "audio";
    if (stream_format !== "audio" && stream_format !== "sse") {
        throw new ApiError(400, `Invalid value for 'stream_format': '${stream_format}'. Supported values are: 'audio' and 'sse'.`, "invalid_request_error", "stream_format", "invalid_value");
    }

    const instructions = body.instructions;
    if (instructions != null && typeof instructions !== "string") {
        throw new ApiError(400, "'instructions' must be a string.", "invalid_request_error", "instructions");
    }

    const volume = Number(body.volume ?? 0);
    const pitch = Number(body.pitch ?? 0);
    if (!Number.isFinite(volume) || !Number.isFinite(pitch)) {
        throw new ApiError(400, "'volume' and 'pitch' must be numbers.");
    }
    const style = typeof body.style === "string" && /^[\w-]+$/.test(body.style) ? body.style : "general";

    return { model, provider, input, voice: voice.trim(), instructions, response_format, speed, stream_format, edgeEndpoint: "auto", kokoro: null, volume, pitch, style };
}

// Server-sent events in the shape of OpenAI's `stream_format: "sse"`.
function sseResponse(parts: AsyncGenerator<Uint8Array>, inputChars: number): Response {
    const encoder = new TextEncoder();
    const { readable, writable } = new TransformStream<Uint8Array, Uint8Array>();
    (async () => {
        const writer = writable.getWriter();
        const send = (event: object) => writer.write(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
        try {
            for await (const part of parts) await send({ type: "speech.audio.delta", audio: bytesToBase64(part) });
            await send({ type: "speech.audio.done", usage: { input_tokens: inputChars, output_tokens: 0, total_tokens: inputChars } });
            await writer.close();
        } catch (error) {
            await send({ type: "error", error: { message: String((error as Error)?.message ?? error), type: "server_error" } }).catch(() => {});
            await writer.close().catch(() => {});
        }
    })();
    return new Response(readable, {
        headers: { ...corsHeaders(), "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    });
}

// Split text into chunks of at most maxLen characters, preferring line, then sentence, then word boundaries.
function splitText(text: string, maxLen: number): string[] {
    const pieces: string[] = [];
    for (const line of text.trim().split("\n")) {
        if (line.length <= maxLen) {
            pieces.push(line);
            continue;
        }
        for (const sentence of line.match(/[^。！？!?；;.]+[。！？!?；;.]*\s*/g) || [line]) {
            for (let i = 0; i < sentence.length; i += maxLen) pieces.push(sentence.slice(i, i + maxLen));
        }
    }

    const chunks: string[] = [];
    let current = "";
    for (const piece of pieces) {
        if (current && current.length + 1 + piece.length > maxLen) {
            chunks.push(current);
            current = piece;
        } else {
            current = current ? `${current}\n${piece}` : piece;
        }
    }
    if (current.trim()) chunks.push(current);
    return chunks.filter((c) => c.trim());
}

// ---------------------------------------------------------------- Edge TTS backend

async function getEdgeAudioChunk(text: string, req: SpeechRequest): Promise<Uint8Array> {
    const canReadAloud = req.response_format === "mp3";
    if (req.edgeEndpoint === "readaloud") {
        if (!canReadAloud) {
            throw new ApiError(400, "The Edge Read Aloud endpoint only returns 'mp3'.", "invalid_request_error", "response_format", "unsupported_value");
        }
        return getReadAloudAudioChunk(text, req);
    }
    try {
        return await getTranslatorAudioChunk(text, req);
    } catch (error) {
        // Client mistakes (bad voice, ...) would fail on the fallback too.
        const upstreamFailure = !(error instanceof ApiError) || error.status >= 500;
        if (req.edgeEndpoint === "translator" || !canReadAloud || !upstreamFailure) throw error;
        console.warn("Translator endpoint failed, falling back to Edge Read Aloud:", error);
        return getReadAloudAudioChunk(text, req);
    }
}

function edgeVoiceName(voice: string): string {
    return OPENAI_VOICE_TO_EDGE[voice.toLowerCase()] || voice;
}

function edgeProsody(req: SpeechRequest) {
    const signed = (n: number, unit: string) => `${n >= 0 ? "+" : ""}${n}${unit}`;
    return {
        rate: signed(Math.round((req.speed - 1.0) * 100), "%"),
        pitch: signed(Math.round(req.pitch), "Hz"),
        volume: signed(Math.round(req.volume * 100), "%"),
    };
}

// A line ending in "[500]" inserts a 500 ms pause there.
function textToSsmlBody(text: string): string {
    return escapeXml(text).replace(/\[(\d+)\][ \t]*$/gm, (_, ms) => `<break time="${Math.min(parseInt(ms), 20000)}ms"/>`);
}

async function getTranslatorAudioChunk(text: string, req: SpeechRequest): Promise<Uint8Array> {
    const endpoint = await getEndpoint();
    const voice = edgeVoiceName(req.voice);
    const outputFormat = req.model === "tts-1-hd" && req.response_format === "mp3" ? EDGE_HD_MP3 : EDGE_FORMATS[req.response_format]!;

    const response = await fetchWithTimeout(`https://${endpoint.r}.tts.speech.microsoft.com/cognitiveservices/v1`, {
        method: "POST",
        headers: {
            Authorization: endpoint.t,
            "Content-Type": "application/ssml+xml",
            "User-Agent": USER_AGENT,
            "X-Microsoft-OutputFormat": outputFormat,
        },
        body: getSsml(text, voice, req),
    });

    if (!response.ok) {
        const detail = await response.text();
        if (response.status === 400) {
            throw new ApiError(400, `Edge TTS rejected the request (is '${voice}' a valid voice? See GET /v1/audio/voices). ${detail}`, "invalid_request_error", "voice", "invalid_value");
        }
        throw new ApiError(502, `Edge TTS API error: ${response.status} ${detail}`, "server_error", null, "upstream_error");
    }
    return new Uint8Array(await response.arrayBuffer());
}

function getSsml(text: string, voice: string, req: SpeechRequest): string {
    const { rate, pitch, volume } = edgeProsody(req);
    const lang = voice.split("-").slice(0, 2).join("-") || "en-US";
    const body = textToSsmlBody(text);

    return `<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" version="1.0" xml:lang="${escapeXml(lang)}">
    <voice name="${escapeXml(voice)}">
        <mstts:express-as style="${req.style}" styledegree="2.0" role="default">
            <prosody rate="${rate}" pitch="${pitch}" volume="${volume}">${body}</prosody>
        </mstts:express-as>
    </voice>
</speak>`;
}

async function getEndpoint() {
    const now = Date.now() / 1000;
    if (tokenInfo.token && tokenInfo.expiredAt && now < tokenInfo.expiredAt - TOKEN_REFRESH_BEFORE_EXPIRY) {
        return tokenInfo.endpoint;
    }

    const endpointUrl = "https://dev.microsofttranslator.com/apps/endpoint?api-version=1.0";
    try {
        const response = await fetchWithTimeout(endpointUrl, {
            method: "POST",
            headers: {
                "Accept-Language": "zh-Hans",
                "X-ClientVersion": "4.0.530a 5fe1dc6c",
                "X-UserId": "0f04d16a175c411e",
                "X-HomeGeographicRegion": "zh-Hans-CN",
                "X-ClientTraceId": uuid(),
                "X-MT-Signature": await sign(endpointUrl),
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": "0",
                "Accept-Encoding": "gzip",
            },
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data: any = await response.json();
        const jwt = JSON.parse(atob(data.t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
        tokenInfo = { endpoint: data, token: data.t, expiredAt: jwt.exp };
        return data;
    } catch (error) {
        console.error("Failed to get Edge TTS endpoint:", error);
        // Fall back to a cached token, even if it is about to expire
        if (tokenInfo.token) return tokenInfo.endpoint;
        throw new ApiError(502, `Failed to get Edge TTS endpoint: ${(error as Error).message}`, "server_error", null, "upstream_error");
    }
}

async function sign(urlStr: string): Promise<string> {
    const url = urlStr.split("://")[1];
    const uuidStr = uuid();
    const formattedDate = new Date().toUTCString().replace(/GMT/, "").trim().toLowerCase() + " gmt";
    const bytesToSign = `MSTranslatorAndroidApp${encodeURIComponent(url)}${formattedDate}${uuidStr}`.toLowerCase();
    const key = base64ToBytes("oik6PdDdMnOXemTbwvMn9de/h9lFnfBaCWbGMMZqqoSaQaqUOqjVGm5NqsmjcBI1x+sS9ugjB55HEJWRiFXYFw==");
    const signature = await hmacSha256(key, bytesToSign);
    return `MSTranslatorAndroidApp::${bytesToBase64(signature)}::${formattedDate}::${uuidStr}`;
}

// ---------------------------------------------------------------- Edge Read Aloud (fallback)

// Sec-MS-GEC: SHA-256 of the current Windows file time, rounded down to 5 minutes, plus the client token.
async function readAloudSecMsGec(): Promise<string> {
    let seconds = Math.floor(Date.now() / 1000) + 11644473600;
    seconds -= seconds % 300;
    const ticks = BigInt(seconds) * 10_000_000n;
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${ticks}${READALOUD_TOKEN}`));
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("").toUpperCase();
}

async function readAloudUrl(path: string, params: Record<string, string>): Promise<string> {
    const query = new URLSearchParams({
        ...params,
        "Sec-MS-GEC": await readAloudSecMsGec(),
        "Sec-MS-GEC-Version": `1-${READALOUD_CHROMIUM_VERSION}`,
    });
    return `https://${READALOUD_BASE}${path}?${query}`;
}

function readAloudTimestamp(): string {
    return new Date().toUTCString().replace(/^(\w+), (\d+) (\w+) (\d+) (.+) GMT$/, "$1 $3 $2 $4 $5 GMT+0000 (Coordinated Universal Time)");
}

async function getReadAloudAudioChunk(text: string, req: SpeechRequest): Promise<Uint8Array> {
    const parts: Uint8Array[] = [];
    for (const piece of splitText(text, READALOUD_CHUNK_CHARS)) {
        try {
            parts.push(await readAloudSynthesize(piece, req));
        } catch {
            // The service intermittently closes without audio; one retry usually succeeds.
            parts.push(await readAloudSynthesize(piece, req));
        }
    }
    return concatBytes(parts);
}

async function readAloudSynthesize(text: string, req: SpeechRequest): Promise<Uint8Array> {
    const voice = edgeVoiceName(req.voice);
    const { rate, pitch, volume } = edgeProsody(req);
    // The service only accepts the SSML shape Edge itself sends (one voice, one prosody, no <break>),
    // so "[500]" pause markers are dropped here.
    const body = escapeXml(text.replace(/\[\d+\][ \t]*$/gm, ""));
    const ssml = `<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'><voice name='${escapeXml(voice)}'><prosody pitch='${pitch}' rate='${rate}' volume='${volume}'>${body}</prosody></voice></speak>`;

    const url = await readAloudUrl("/edge/v1", { TrustedClientToken: READALOUD_TOKEN, ConnectionId: uuid() });
    const response = await fetchWithTimeout(url, {
        headers: { ...READALOUD_HEADERS, Upgrade: "websocket", Cookie: `muid=${uuid().toUpperCase()};` },
    });
    const ws = response.webSocket;
    if (!ws) {
        throw new ApiError(502, `Edge Read Aloud connection failed: ${response.status} ${(await response.text()).slice(0, 300)}`, "server_error", null, "upstream_error");
    }
    ws.accept();

    return new Promise<Uint8Array>((resolve, reject) => {
        const parts: Uint8Array[] = [];
        const fail = (message: string) => {
            clearTimeout(timer);
            try { ws.close(); } catch {}
            reject(new ApiError(502, `Edge Read Aloud error: ${message}`, "server_error", null, "upstream_error"));
        };
        const timer = setTimeout(() => fail("timed out"), UPSTREAM_TIMEOUT_MS);

        ws.addEventListener("message", (event) => {
            if (typeof event.data === "string") {
                if (/\r\nPath:turn\.end\r\n|^Path:turn\.end\r\n/.test(event.data)) {
                    clearTimeout(timer);
                    try { ws.close(); } catch {}
                    if (parts.length === 0) return fail("no audio received (check the voice name)");
                    resolve(concatBytes(parts));
                }
                return;
            }
            // Binary frame: 2-byte big-endian header length, headers, then audio.
            const data = new Uint8Array(event.data as ArrayBuffer);
            if (data.length < 2) return;
            const headerLength = (data[0] << 8) | data[1];
            const headers = new TextDecoder().decode(data.subarray(2, 2 + headerLength));
            if (/(^|\r\n)Path:audio(\r\n|$)/.test(headers) && data.length > 2 + headerLength) {
                parts.push(data.slice(2 + headerLength));
            }
        });
        ws.addEventListener("close", () => fail(`connection closed before the audio finished (is '${voice}' a valid voice? See GET /v1/audio/voices)`));
        ws.addEventListener("error", () => fail("connection error"));

        const ts = readAloudTimestamp();
        ws.send(
            `X-Timestamp:${ts}\r\nContent-Type:application/json; charset=utf-8\r\nPath:speech.config\r\n\r\n` +
                `{"context":{"synthesis":{"audio":{"metadataoptions":{"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},"outputFormat":"${READALOUD_FORMAT}"}}}}\r\n`,
        );
        ws.send(`X-RequestId:${uuid()}\r\nContent-Type:application/ssml+xml\r\nX-Timestamp:${ts}Z\r\nPath:ssml\r\n\r\n${ssml}`);
    });
}

async function getReadAloudVoices(): Promise<any[]> {
    const url = await readAloudUrl("/voices/list", { trustedclienttoken: READALOUD_TOKEN });
    const response = await fetchWithTimeout(url, { headers: { ...READALOUD_HEADERS, Accept: "*/*" } });
    if (!response.ok) {
        throw new ApiError(502, `Edge Read Aloud voice list error: ${response.status}`, "server_error", null, "upstream_error");
    }
    return response.json();
}

// ---------------------------------------------------------------- Kokoro backend (Hugging Face Space)

// A sleeping Space takes a minute or two to wake up, so allow more time than other upstreams.
const KOKORO_TIMEOUT_MS = 180_000;

function kokoroConfig(env: Env): { url: string; key?: string } {
    const url = env.KOKORO_URL?.trim().replace(/\/+$/, "");
    if (!url) {
        throw new ApiError(400, "The Kokoro backend is not configured on this server (set KOKORO_URL).", "invalid_request_error", "model", "model_not_available");
    }
    return { url, key: env.KOKORO_API_KEY?.trim() || undefined };
}

function kokoroHeaders(kokoro: { key?: string }): Record<string, string> {
    return { "Content-Type": "application/json", ...(kokoro.key ? { Authorization: `Bearer ${kokoro.key}` } : {}) };
}

async function throwKokoroError(response: Response): Promise<never> {
    const text = await response.text();
    let error: any = null;
    try {
        error = JSON.parse(text).error;
    } catch {}
    if (error?.message && response.status < 500 && response.status !== 401) {
        // The Space's own validation errors are already in OpenAI format; pass them on.
        throw new ApiError(response.status, error.message, error.type, error.param, error.code);
    }
    const status = response.status === 503 ? 503 : 502;
    throw new ApiError(status, `Kokoro Space error: ${response.status} ${(error?.message ?? text).slice(0, 300)}`, "server_error", null, "upstream_error");
}

async function getKokoroAudioChunk(text: string, req: SpeechRequest): Promise<Uint8Array> {
    const kokoro = req.kokoro!;
    const response = await fetchWithTimeout(
        `${kokoro.url}/v1/audio/speech`,
        {
            method: "POST",
            headers: kokoroHeaders(kokoro),
            body: JSON.stringify({
                model: "kokoro",
                input: text,
                voice: req.voice,
                // WAV is assembled from raw PCM here, like the Edge path
                response_format: req.response_format === "wav" ? "pcm" : req.response_format,
                speed: req.speed,
            }),
        },
        KOKORO_TIMEOUT_MS,
    );
    if (!response.ok) await throwKokoroError(response);
    return new Uint8Array(await response.arrayBuffer());
}

// ---------------------------------------------------------------- openai.fm backend

async function getOpenaiFmAudioChunk(text: string, req: SpeechRequest): Promise<Uint8Array> {
    const params = new URLSearchParams({
        input: text.replace(/\[\d+\][ \t]*$/gm, ""),
        voice: req.voice.toLowerCase(),
        prompt: req.instructions?.trim() || OPENAI_FM_DEFAULT_PROMPT,
        generation: crypto.randomUUID(),
    });
    const response = await fetchWithTimeout(`https://www.openai.fm/api/generate?${params}`, {
        headers: { "User-Agent": USER_AGENT, Referer: "https://www.openai.fm/" },
    });
    if (!response.ok) {
        const status = response.status === 429 ? 429 : 502;
        throw new ApiError(status, `openai.fm API error: ${response.status} ${(await response.text()).slice(0, 500)}`, status === 429 ? "rate_limit_error" : "server_error", null, "upstream_error");
    }
    return new Uint8Array(await response.arrayBuffer());
}

// ---------------------------------------------------------------- helpers

function jsonResponse(data: unknown, status = 200): Response {
    return new Response(JSON.stringify(data), {
        status,
        headers: { "Content-Type": "application/json", ...corsHeaders() },
    });
}

function errorResponse(error: ApiError): Response {
    return jsonResponse({ error: { message: error.message, type: error.type, param: error.param, code: error.code } }, error.status);
}

function corsHeaders(): Record<string, string> {
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, x-api-key",
        "Access-Control-Max-Age": "86400",
    };
}

function escapeXml(s: string): string {
    return s.replace(/[<>&"']/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" })[c]!);
}

function wavHeader(dataLength: number, sampleRate = PCM_SAMPLE_RATE, channels = 1, bitsPerSample = 16): Uint8Array {
    const header = new ArrayBuffer(44);
    const view = new DataView(header);
    const writeString = (offset: number, s: string) => [...s].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
    const byteRate = (sampleRate * channels * bitsPerSample) / 8;
    writeString(0, "RIFF");
    view.setUint32(4, 36 + dataLength, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, channels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, byteRate, true);
    view.setUint16(32, (channels * bitsPerSample) / 8, true);
    view.setUint16(34, bitsPerSample, true);
    writeString(36, "data");
    view.setUint32(40, dataLength, true);
    return new Uint8Array(header);
}

function concatBytes(parts: Uint8Array[]): Uint8Array {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let offset = 0;
    for (const p of parts) {
        out.set(p, offset);
        offset += p.length;
    }
    return out;
}

async function hmacSha256(key: Uint8Array, data: string): Promise<Uint8Array> {
    const cryptoKey = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: { name: "SHA-256" } }, false, ["sign"]);
    return new Uint8Array(await crypto.subtle.sign("HMAC", cryptoKey, new TextEncoder().encode(data)));
}

function base64ToBytes(base64: string): Uint8Array {
    return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
}

function bytesToBase64(bytes: Uint8Array): string {
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
        binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
}

function uuid(): string {
    return crypto.randomUUID().replace(/-/g, "");
}

async function fetchWithTimeout(url: string, options: RequestInit = {}, timeout = UPSTREAM_TIMEOUT_MS): Promise<Response> {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeout);
    try {
        return await fetch(url, { ...options, signal: controller.signal });
    } finally {
        clearTimeout(id);
    }
}
