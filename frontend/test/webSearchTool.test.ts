/**
 * B2: Boson's server-side web_search on the Realtime answer path. Declared for textbooks, and for novels only with
 * spoilers on; never for a spoiler-safe novel. A response that ends in a server-run call is not a finished turn
 * (latent bug #4): the server adds the result and starts the answer itself. A "Searching…" signal and source links.
 */
import { describe, expect, it } from "vitest";
import { TalkClient, type MicLike, type PlayerLike, type SocketLike } from "../src/talk/TalkClient";
import type { SessionEvent } from "../src/talk/TalkController";
import type { SentenceSpeaker } from "../src/talk/sentencePlayer";
import { WEB_SEARCH_TOOL, localeCountry, sessionTools, sourcesFromSearchOutput, webSearchAllowed, webSearchTool } from "../src/talk/webSearch";

class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: Record<string, any>[] = [];
  constructor(public url: string, public protocols: string[]) {}
  send(data: string) { this.sent.push(JSON.parse(data)); }
  close(code = 1000, reason = "") { this.readyState = 3; this.onclose?.({ code, reason }); }
  open() { this.readyState = 1; this.onopen?.(); }
  recv(evt: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(evt) }); }
  types() { return this.sent.map((e) => e.type); }
}
class FakePlayer implements PlayerLike {
  playedMs = 0;
  enqueued: string[] = [];
  onSpeaking: (s: boolean) => void = () => {};
  async resume() {}
  enqueueBase64(b: string) { this.enqueued.push(b); }
  async flush() { return this.playedMs; }
  reset() {}
  async close() {}
}
class FakeSpeaker implements SentenceSpeaker {
  played: string[] = [];
  onPlaying: (on: boolean) => void = () => {};
  private settlers: (() => void)[] = [];
  prefetch() {}
  play(text: string) { this.played.push(text); this.onPlaying(true); return new Promise<void>((r) => this.settlers.push(() => { this.onPlaying(false); r(); })); }
  end() { this.settlers.shift()?.(); }
  stop() { this.settlers.splice(0).forEach((f) => f()); }
}
const flush = () => new Promise((r) => setTimeout(r, 0));

async function openClient(extra: Record<string, unknown> = {}) {
  const sockets: FakeSocket[] = [];
  const mic: MicLike = { start: async () => {}, stop: async () => {} };
  const player = new FakePlayer();
  const speaker = new FakeSpeaker();
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
    createPlayer: (onSpeaking) => { player.onSpeaking = onSpeaking; return player; }, createMic: () => mic, cancelWaitMs: 20,
    createSpeaker: (onPlaying) => { speaker.onPlaying = onPlaying; return speaker; },
    ...extra,
  });
  const events: SessionEvent[] = [];
  client.on((e) => events.push(e));
  const p = client.ensureOpen("marcus");
  await flush();
  sockets[0].open();
  await p;
  return { client, ws: sockets[0], events, player, speaker, sockets };
}
const types = (events: SessionEvent[]) => events.map((e) => e.type);

/** The wire as captured live (2026-10-24): preamble, server call, result item, server-started answer. */
function serverSearchTurn(ws: FakeSocket, output: string) {
  ws.recv({ type: "response.created", response: { id: "r1" } });
  ws.recv({ type: "response.output_item.added", response_id: "r1", output_index: 0, item: { id: "m1", type: "message", role: "assistant" } });
  ws.recv({ type: "response.output_text.delta", response_id: "r1", item_id: "m1", delta: "[neutral] Let me dig that up. " });
  ws.recv({ type: "response.output_item.added", response_id: "r1", output_index: 1, item: { id: "fc1", type: "function_call", status: "in_progress", name: "web_search", call_id: "call_1", arguments: "", executor: "server" } });
  ws.recv({ type: "response.function_call_arguments.done", response_id: "r1", item_id: "fc1", call_id: "call_1", name: "web_search", arguments: "{\"queries\":[\"tf-idf origin\"]}" });
  ws.recv({ type: "response.output_text.done", response_id: "r1", text: "[neutral] Let me dig that up. " });
  ws.recv({ type: "response.done", response: { id: "r1", status: "completed", output: [
    { type: "message", role: "assistant", status: "completed" },
    { type: "function_call", status: "completed", name: "web_search", call_id: "call_1", arguments: "{\"queries\":[\"tf-idf origin\"]}", executor: "server" }] } });
  ws.recv({ type: "conversation.item.added", item: { id: "fo1", type: "function_call_output", call_id: "call_1", output } });
}
const SEARCH_OUTPUT = `[1] Tf-idf weighting
    https://nlp.stanford.edu/IR-book/html/htmledition/tf-idf-weighting-1.html
    We now combine the definitions of term frequency and inverse document frequency...
[2] IDF term weighting and IR research lessons Karen Sparck Jones Computer Laboratory, University of Cambridge This paper appeared in Journal
    https://www.cl.cam.ac.uk/archive/ksj21/ksjdigipapers/jdoc04.pdf
    **IDF term weighting and IR research lessons** ...
[3] The Vector Space Model in Information Retrieval - Term Weighting Problem
    https://wiki.eecs.yorku.ca/course_archive/2014-15/W/6339/_media/polettini_information_retrieval.pdf
    This formula was proposed by Croft [4]...`;

describe("web_search: when it may be declared", () => {
  it("textbooks always; novels only with spoilers on; an unknown kind is a novel", () => {
    expect(webSearchAllowed("textbook", false)).toBe(true);
    expect(webSearchAllowed("textbook", true)).toBe(true);
    expect(webSearchAllowed("novel", false)).toBe(false);
    expect(webSearchAllowed("novel", true)).toBe(true);
    expect(webSearchAllowed(undefined, false)).toBe(false);
    expect(webSearchAllowed(undefined, true)).toBe(true);
  });
  it("the tool list carries exactly one web_search entry when allowed, the plain open-web form, and never otherwise", () => {
    expect(WEB_SEARCH_TOOL).toEqual({ type: "web_search" }); // no allowed_domains: the docs warn a scoped search often returns nothing
    const tb = sessionTools("textbook", false, "en");
    expect(tb.filter((t) => (t as { type: string }).type === "web_search")).toEqual([WEB_SEARCH_TOOL]);
    expect(tb.some((t) => (t as { name?: string }).name === "find_topic")).toBe(true); // the textbook function tools are kept
    expect(JSON.stringify(sessionTools("novel", false, "en"))).not.toContain("web_search");
    expect(JSON.stringify(sessionTools("novel", true, "en"))).toContain("web_search");
    expect(sessionTools("novel", false, "en").some((t) => (t as { name?: string }).name === "show_cast")).toBe(true);
  });
});

describe("web_search: the reader's country", () => {
  it("reads the region from the browser locale, never a full name", () => {
    expect(localeCountry("en-CA")).toBe("CA");
    expect(localeCountry("zh-Hant-TW")).toBe("TW");
    expect(localeCountry("fr_fr")).toBe("FR");
    expect(localeCountry("en")).toBeNull();
    expect(localeCountry(undefined)).toBeNull();
    expect(localeCountry("es-419")).toBeNull(); // a UN region code, not a country
  });
  it("adds location.country only when there is one", () => {
    expect(webSearchTool("en-CA")).toEqual({ type: "web_search", location: { country: "CA" } });
    expect(webSearchTool("en")).toEqual({ type: "web_search" });
    expect(sessionTools("textbook", false, "en-GB").filter((x) => (x as { type: string }).type === "web_search"))
      .toEqual([{ type: "web_search", location: { country: "GB" } }]);
  });
});

describe("web_search: source links from the result item", () => {
  it("reads the numbered title / URL blocks Boson returns, at most five, and ignores garbage", () => {
    expect(sourcesFromSearchOutput(SEARCH_OUTPUT)).toEqual([
      { uri: "https://nlp.stanford.edu/IR-book/html/htmledition/tf-idf-weighting-1.html", title: "Tf-idf weighting" },
      { uri: "https://www.cl.cam.ac.uk/archive/ksj21/ksjdigipapers/jdoc04.pdf", title: "IDF term weighting and IR research lessons Karen Sparck Jones Computer Laboratory, University of Cambridge This paper ap" }, // titles are capped at 120 (cleanSources)
      { uri: "https://wiki.eecs.yorku.ca/course_archive/2014-15/W/6339/_media/polettini_information_retrieval.pdf", title: "The Vector Space Model in Information Retrieval - Term Weighting Problem" },
    ]);
    expect(sourcesFromSearchOutput("The search failed or timed out.")).toEqual([]);
    expect(sourcesFromSearchOutput(42)).toEqual([]);
    expect(sourcesFromSearchOutput("see https://a.example/x and https://a.example/x again\nftp://nope.example")).toEqual([{ uri: "https://a.example/x", title: "a.example" }]);
    const many = Array.from({ length: 8 }, (_, i) => `[${i + 1}] T${i}\n    https://s${i}.example/p`).join("\n");
    expect(sourcesFromSearchOutput(many)).toHaveLength(5);
  });
});

describe("web_search on the wire (TalkClient, text mode)", () => {
  it("setTools changes the declared tools of an open session and is remembered for the next one", async () => {
    const { client, ws, sockets } = await openClient({ tools: sessionTools("novel", false, "en") });
    expect(JSON.stringify(ws.sent[0].session.tools)).not.toContain("web_search");
    client.setTools(sessionTools("novel", true, "en")); // spoilers switched on
    expect(ws.sent.at(-1)).toMatchObject({ type: "session.update", session: { tools: expect.arrayContaining([WEB_SEARCH_TOOL]) } });
    const n = ws.sent.length;
    client.setTools(sessionTools("novel", true, "en")); // unchanged: nothing sent
    expect(ws.sent.length).toBe(n);
    client.resetConversation();
    const p = client.ensureOpen("marcus");
    await flush(); sockets[1].open(); await p;
    expect(JSON.stringify(sockets[1].sent[0].session.tools)).toContain("web_search");
  });

  it("a response ending in a server-run web_search is not a finished turn: no response-done, searching chip, sources, the server's own answer plays", async () => {
    const { client, ws, events, speaker } = await openClient({ tools: sessionTools("textbook", false, "en") });
    client.respond("Be the Tutor.", "who first proposed tf-idf?");
    const creates = () => ws.types().filter((t) => t === "response.create").length;
    serverSearchTurn(ws, SEARCH_OUTPUT);
    await flush();
    expect(speaker.played).toEqual(["Let me dig that up."]); // the preamble is spoken while the search runs
    expect(types(events)).not.toContain("response-done");
    expect(types(events)).not.toContain("playback-drained");
    expect(events).toContainEqual({ type: "searching", active: true });
    expect(events.find((e) => e.type === "sources")).toEqual({ type: "sources", sources: sourcesFromSearchOutput(SEARCH_OUTPUT) });
    expect(creates()).toBe(1); // never a response.create or a function_call_output for a server tool (the docs forbid both)
    expect(ws.types().filter((t) => t === "conversation.item.create")).toEqual([]);
    // the server starts the answer itself
    ws.recv({ type: "response.created", response: { id: "r2" } });
    expect(ws.types().at(-1)).not.toBe("response.cancel"); // not treated as an unasked-for response
    expect(events.at(-1)).toEqual({ type: "searching", active: false, completed: true });
    ws.recv({ type: "response.output_text.delta", response_id: "r2", item_id: "m2", delta: "[enthusiasm] Karen Sparck Jones, in 1972. " });
    ws.recv({ type: "response.done", response: { id: "r2", status: "completed", output: [{ type: "message", role: "assistant" }] } });
    await flush();
    expect(types(events)).toContain("response-done");
    speaker.end(); await flush();
    speaker.end(); await flush();
    expect(speaker.played).toEqual(["Let me dig that up.", "Karen Sparck Jones, in 1972."]);
    expect(types(events).filter((t) => t === "playback-drained")).toHaveLength(1);
  });

  it("hands-free (server turns): the server-started answer after a search is not cancelled as a stray response", async () => {
    const h = await openServerMode();
    h.ws.recv({ type: "response.created", response: { id: "stray" } }); // the server answering a turn by itself is still cancelled (#135)
    expect(h.ws.types().at(-1)).toBe("response.cancel");
    h.client.respond("Be the Tutor.", "q");
    serverSearchTurn(h.ws, SEARCH_OUTPUT);
    await flush();
    const cancels = h.ws.types().filter((t) => t === "response.cancel").length;
    h.ws.recv({ type: "response.created", response: { id: "r2" } }); // the answer the server starts after its search
    expect(h.ws.types().filter((t) => t === "response.cancel")).toHaveLength(cancels);
    h.ws.recv({ type: "response.output_text.delta", response_id: "r2", item_id: "m2", delta: "[neutral] Answer. " });
    await flush();
    h.speaker.end(); await flush(); // the preamble finishes; the server's answer follows
    expect(h.speaker.played).toEqual(["Let me dig that up.", "Answer."]);
  });

  it("a barge-in while the search runs cancels the answer the server then starts, and ends the searching chip", async () => {
    const { client, ws, events, speaker } = await openClient({ tools: sessionTools("textbook", false, "en") });
    client.respond("Be the Tutor.", "q");
    serverSearchTurn(ws, SEARCH_OUTPUT);
    await flush();
    const p = client.interrupt();
    await flush();
    expect(ws.types().at(-1)).toBe("response.cancel");
    ws.recv({ type: "error", error: { type: "invalid_request_error", code: "response_not_active", message: "none" } });
    await p;
    expect(events.at(-1)).toEqual({ type: "searching", active: false, completed: false });
    ws.recv({ type: "response.created", response: { id: "r2" } }); // the search finished anyway
    expect(ws.types().at(-1)).toBe("response.cancel");
    ws.recv({ type: "response.output_text.delta", response_id: "r2", item_id: "m2", delta: "[neutral] Too late. " });
    await flush();
    expect(speaker.played).toEqual(["Let me dig that up."]);
  });

  it("audio mode: the same server call does not end the turn either, and the server's answer audio plays", async () => {
    const { client, ws, events, player } = await openClient({ answerMode: "audio", tools: sessionTools("textbook", false, "en") });
    client.respond("x", "q");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_audio.delta", response_id: "r1", item_id: "m1", delta: "PRE" });
    ws.recv({ type: "response.done", response: { id: "r1", status: "completed", output: [
      { type: "message", role: "assistant" }, { type: "function_call", name: "web_search", call_id: "call_1", arguments: "{}", executor: "server" }] } });
    player.onSpeaking(false); // the preamble finished playing
    expect(types(events)).not.toContain("response-done");
    expect(types(events)).not.toContain("playback-drained");
    ws.recv({ type: "response.created", response: { id: "r2" } });
    ws.recv({ type: "response.output_audio.delta", response_id: "r2", item_id: "m2", delta: "ANS" });
    ws.recv({ type: "response.done", response: { id: "r2", status: "completed", output: [{ type: "message" }] } });
    player.onSpeaking(false);
    expect(player.enqueued).toEqual(["PRE", "ANS"]);
    expect(types(events).filter((t) => t === "playback-drained")).toHaveLength(1);
  });
});

/** A client whose open session uses Boson's turn detection (hands-free), ack'd. */
async function openServerMode() {
  const sockets: FakeSocket[] = [];
  const mic: MicLike = { start: async () => {}, stop: async () => {} };
  const player = new FakePlayer();
  const speaker = new FakeSpeaker();
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
    createPlayer: () => player, createMic: () => mic, cancelWaitMs: 20,
    createSpeaker: (onPlaying) => { speaker.onPlaying = onPlaying; return speaker; },
    tools: sessionTools("textbook", false, "en"),
  });
  const events: SessionEvent[] = [];
  client.on((e) => events.push(e));
  client.setServerTurns(true);
  const p = client.ensureOpen("marcus");
  await flush();
  sockets[0].open();
  await p;
  sockets[0].recv({ type: "session.created", session: { audio: { input: { turn_detection: { type: "semantic_vad", create_response: false } } } } });
  return { client, ws: sockets[0], events, speaker };
}

describe("web_search: the server's WEB_SEARCH switch (review)", () => {
  it("off on the server: never declared, whatever the book or the spoilers switch", () => {
    expect(webSearchAllowed("textbook", false, false)).toBe(false);
    expect(webSearchAllowed("novel", true, false)).toBe(false);
    expect(JSON.stringify(sessionTools("textbook", true, "en", false))).not.toContain("web_search");
    expect(webSearchAllowed("textbook", false)).toBe(true); // default: on
  });
});
