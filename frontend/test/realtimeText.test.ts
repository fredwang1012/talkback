/**
 * B1: Realtime answers in TEXT mode. The model writes the answer (output_modalities ["text"], a `[feeling]` marker per
 * sentence); the browser splits it into sentences as it streams, voices each with Higgs TTS (emotion tag, the
 * speaker's voice) through the same sentence player as the Gemini path, and lip-syncs it. Fake socket, fake speaker.
 */
import { describe, expect, it, vi } from "vitest";
import { TalkClient, type MicLike, type PlayerLike, type SocketLike } from "../src/talk/TalkClient";
import type { SessionEvent } from "../src/talk/TalkController";
import type { SentenceSpeaker } from "../src/talk/sentencePlayer";
import { answerMode } from "../src/talk/answerMode";
import { splitMarker, stripTags } from "../src/talk/emotionMarker";
import { MAX_SENTENCE_CHARS, takeSentences } from "../src/talk/sentences";

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
  async resume() {}
  enqueueBase64(b: string) { this.enqueued.push(b); }
  async flush() { return this.playedMs; }
  reset() {}
  async close() {}
}
/** The sentence player: `end()` finishes the sentence being spoken; `fail()` makes it reject (TTS failed). */
class FakeSpeaker implements SentenceSpeaker {
  played: string[] = [];
  prefetched: string[] = [];
  stops = 0;
  pauses = 0;
  resumes = 0;
  onPlaying: (on: boolean) => void = () => {};
  private settlers: { resolve: () => void; reject: (e: Error) => void }[] = [];
  prefetch(text: string) { this.prefetched.push(text); }
  play(text: string) {
    this.played.push(text);
    this.onPlaying(true);
    return new Promise<void>((resolve, reject) => this.settlers.push({ resolve: () => { this.onPlaying(false); resolve(); }, reject }));
  }
  end() { this.settlers.shift()?.resolve(); }
  fail(msg: string) { this.settlers.shift()?.reject(new Error(msg)); }
  /** Counts only stops that cut a sentence short (the client also stops an idle speaker before each new answer). */
  stop() { const s = this.settlers.splice(0); if (s.length) this.stops++; s.forEach((x) => x.resolve()); }
  pause() { this.pauses++; }
  resume() { this.resumes++; return true; }
}
const flush = () => new Promise((r) => setTimeout(r, 0));

async function openClient(extra: Record<string, unknown> = {}) {
  const sockets: FakeSocket[] = [];
  const mic: MicLike = { start: async () => {}, stop: async () => {} };
  const player = new FakePlayer();
  const speaker = new FakeSpeaker();
  const sentences: [string, string | null, boolean][] = [];
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
    createPlayer: () => player, createMic: () => mic, cancelWaitMs: 20,
    createSpeaker: (onPlaying) => { speaker.onPlaying = onPlaying; return speaker; },
    onSentence: (text, emotion, first) => sentences.push([text, emotion, first]),
    ...extra,
  });
  const events: SessionEvent[] = [];
  client.on((e) => events.push(e));
  const p = client.ensureOpen("marcus");
  await flush();
  sockets[0].open();
  await p;
  return { client, ws: sockets[0], events, player, speaker, sentences, sockets };
}
const types = (events: SessionEvent[]) => events.map((e) => e.type);
const captions = (events: SessionEvent[]) => events.filter((e) => e.type === "caption").map((e) => [(e as { text: string }).text, (e as { done: boolean }).done]);

describe("emotion markers (frontend port of emotion.split_marker)", () => {
  it("takes a leading marker in its many shapes, keeps only known emotions", () => {
    expect(splitMarker("[sadness] I miss him.")).toEqual({ text: "I miss him.", emotion: "sadness" });
    expect(splitMarker("[Emotion: Fear] Run!")).toEqual({ text: "Run!", emotion: "fear" });
    expect(splitMarker("［surprise］Oh!")).toEqual({ text: "Oh!", emotion: "surprise" });
    expect(splitMarker("<|emotion:awe|> Look.")).toEqual({ text: "Look.", emotion: "awe" });
    expect(splitMarker("[neutral] Fine.")).toEqual({ text: "Fine.", emotion: null });
    expect(splitMarker("[tristesse] Il est parti.")).toEqual({ text: "Il est parti.", emotion: null });
    expect(splitMarker("[sadness I miss him.")).toEqual({ text: "I miss him.", emotion: null });
    expect(splitMarker("No marker here.")).toEqual({ text: "No marker here.", emotion: null });
    expect(splitMarker("[sadness]")).toEqual({ text: "", emotion: "sadness" });
  });
  it("strips stray inner markers and any Boson tag, so neither is ever spoken", () => {
    expect(splitMarker("[fear] He ran [sadness] and fell.").text).toBe("He ran and fell.");
    expect(splitMarker("Hi <|sfx:laughter|>there <|<|x|>sfx:laughter|>friend.").text).toBe("Hi there friend.");
    expect(stripTags("a <|b|> c")).toBe("a c");
    expect(stripTags("<|unclosed")).not.toContain("<|");
  });
});

describe("streaming sentence split", () => {
  it("returns complete sentences and keeps the unfinished tail (markers start the next sentence)", () => {
    expect(takeSentences("[sadness] I miss him. [neutral] He le")).toEqual({ done: ["[sadness] I miss him."], rest: "[neutral] He le" });
    expect(takeSentences("Is it 3.5 or 4? Yes! Maybe")).toEqual({ done: ["Is it 3.5 or 4?", "Yes!"], rest: "Maybe" });
    expect(takeSentences("He said \"go.\" Then left.")).toEqual({ done: ['He said "go."'], rest: "Then left." });
    expect(takeSentences("One.[neutral] Two.")).toEqual({ done: ["One."], rest: "[neutral] Two." });
    expect(takeSentences("Wait.")).toEqual({ done: [], rest: "Wait." }); // the next delta may continue it ("Wait... no")
    expect(takeSentences("Wait.", true)).toEqual({ done: ["Wait."], rest: "" });
    expect(takeSentences("  ", true)).toEqual({ done: [], rest: "" });
    expect(takeSentences("第一句。第二句！第三")).toEqual({ done: ["第一句。", "第二句！"], rest: "第三" });
  });
  it("cuts an endless sentence at a space before the TTS limit", () => {
    const long = Array.from({ length: 120 }, (_, i) => `word${i}`).join(" ");
    const { done, rest } = takeSentences(long);
    expect(done.length).toBeGreaterThan(0);
    expect(done.every((s) => s.length <= MAX_SENTENCE_CHARS)).toBe(true);
    expect(rest.length).toBeLessThan(MAX_SENTENCE_CHARS);
    expect([...done, rest].join(" ")).toBe(long);
  });
});

describe("answer mode switch", () => {
  it("direct audio by default; ?answers=text or localStorage talkback:answers=text turns the expressive path on", () => {
    expect(answerMode("", null)).toBe("audio");
    expect(answerMode("?debug", "audio")).toBe("audio");
    expect(answerMode("?answers=text", null)).toBe("text");
    expect(answerMode("?x=1&answers=TEXT", null)).toBe("text");
    expect(answerMode("", "text")).toBe("text");
    expect(answerMode("?answers=audio", "text")).toBe("audio"); // the URL wins
  });
});

describe("TalkClient text mode (fake socket)", () => {
  it("configures text output by default; the fallback switch keeps audio output and the old player path", async () => {
    const t = await openClient();
    expect(t.ws.sent[0].session.output_modalities).toEqual(["text"]);
    expect(t.ws.sent[0].session.audio.output.voice).toBe("marcus");
    const a = await openClient({ answerMode: "audio" });
    expect(a.ws.sent[0].session.output_modalities).toEqual(["audio"]);
    a.client.respond("x", "hello?");
    a.ws.recv({ type: "response.created", response: { id: "r1" } });
    a.ws.recv({ type: "response.output_audio.delta", response_id: "r1", item_id: "i", delta: "AAA" });
    expect(a.player.enqueued).toEqual(["AAA"]);
    expect(a.speaker.played).toEqual([]);
  });

  it("the question travels in response.input (live: with per-response instructions the model does not see conversation items)", async () => {
    const { client, ws } = await openClient();
    client.respond("Be Alice.", "What does Alice drink?");
    expect(ws.sent.at(-1)).toEqual({ type: "response.create", response: {
      instructions: "Be Alice.", max_output_tokens: 600,
      input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "What does Alice drink?" }] }] } });
    expect(ws.types().filter((t) => t === "conversation.item.create")).toEqual([]);
  });

  it("streams text -> sentences with their feeling -> speaker, prefetching ahead; captions follow the voice; drains after the last sentence", async () => {
    const { client, ws, events, speaker, sentences } = await openClient();
    client.respond("Be Alice.", "q");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_item.added", response_id: "r1", item: { id: "m1", type: "message" } });
    ws.recv({ type: "response.output_text.delta", response_id: "r1", item_id: "m1", delta: "[sadness] I miss " });
    expect(speaker.played).toEqual([]); // no sentence yet
    ws.recv({ type: "response.output_text.delta", response_id: "r1", item_id: "m1", delta: "him. [neutral] He left. [amusement] But he" });
    await flush();
    expect(sentences).toEqual([["I miss him.", "sadness", true], ["He left.", null, false]]);
    expect(speaker.played).toEqual(["I miss him."]);
    expect(speaker.prefetched).toContain("He left."); // synthesised while the first one plays
    expect(types(events).filter((t) => t === "audio-start")).toHaveLength(1);
    expect(captions(events)).toEqual([["I miss him.", false]]);
    ws.recv({ type: "response.output_text.done", response_id: "r1", text: "[sadness] I miss him. [neutral] He left. [amusement] But he laughed" });
    ws.recv({ type: "response.done", response: { id: "r1", status: "completed", output: [{ type: "message", role: "assistant" }] } });
    expect(sentences.at(-1)).toEqual(["But he laughed", "amusement", false]); // the tail is flushed at the end
    expect(types(events)).toContain("response-done");
    expect(types(events)).not.toContain("playback-drained"); // still speaking
    speaker.end(); await flush();
    expect(speaker.played).toEqual(["I miss him.", "He left."]);
    expect(captions(events).at(-1)).toEqual(["I miss him. He left.", false]);
    speaker.end(); await flush();
    speaker.end(); await flush();
    expect(speaker.played).toEqual(["I miss him.", "He left.", "But he laughed"]);
    expect(types(events).filter((t) => t === "playback-drained")).toHaveLength(1);
    expect(captions(events).at(-1)).toEqual(["I miss him. He left. But he laughed", true]);
  });

  it("a text answer with nothing to say drains at once", async () => {
    const { client, ws, events } = await openClient();
    client.respond("x", "q");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_text.done", response_id: "r1", text: "[neutral]" });
    ws.recv({ type: "response.done", response: { id: "r1", status: "completed", output: [] } });
    expect(types(events)).toEqual(["response-done", "playback-drained"]);
  });

  it("barge-in in text mode: stops the speaker, reports what was heard, cancels, drops the late text, and the next answer plays", async () => {
    const { client, ws, events, speaker } = await openClient();
    client.respond("x", "q1");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_text.delta", response_id: "r1", item_id: "m1", delta: "[neutral] One. [neutral] Two. " });
    await flush();
    expect(speaker.played).toEqual(["One."]);
    const p = client.interrupt();
    await flush();
    expect(speaker.stops).toBe(1);
    expect(events).toContainEqual({ type: "heard", text: "One." });
    expect(ws.types().at(-1)).toBe("response.cancel");
    ws.recv({ type: "response.output_text.delta", response_id: "r1", item_id: "m1", delta: "[neutral] Three. " }); // generated faster than spoken
    ws.recv({ type: "response.done", response: { id: "r1", status: "cancelled", output: [] } });
    await p;
    ws.recv({ type: "response.output_text.delta", response_id: "r1", item_id: "m1", delta: "[neutral] Four. " }); // a straggler
    await flush();
    expect(speaker.played).toEqual(["One."]);
    expect(types(events)).not.toContain("playback-drained");
    client.respond("y", "q2");
    ws.recv({ type: "response.created", response: { id: "r2" } });
    ws.recv({ type: "response.output_text.delta", response_id: "r2", item_id: "m2", delta: "[relief] Fresh start. " });
    await flush();
    expect(speaker.played).toEqual(["One.", "Fresh start."]);
    expect(captions(events).at(-1)).toEqual(["Fresh start.", false]); // not stacked on the cut-off answer
  });

  it("tool calls work in text mode: the preamble is spoken, the call is ours, the continuation carries the results in its instructions", async () => {
    const { client, ws, events, speaker } = await openClient();
    client.respond("Be Alice.", "jump to chapter two");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_text.delta", response_id: "r1", item_id: "m1", delta: "[neutral] Off we go. " });
    ws.recv({ type: "response.output_item.added", response_id: "r1", item: { id: "fc1", type: "function_call", name: "jump_to", call_id: "c1", executor: "client" } });
    ws.recv({ type: "response.function_call_arguments.done", response_id: "r1", item_id: "fc1", call_id: "c1", name: "jump_to", arguments: "{\"chapter\":\"2\"}" });
    ws.recv({ type: "response.output_text.done", response_id: "r1", text: "[neutral] Off we go. " });
    ws.recv({ type: "response.done", response: { id: "r1", status: "completed", output: [
      { type: "message", role: "assistant" }, { type: "function_call", call_id: "c1", name: "jump_to", arguments: "{\"chapter\":\"2\"}", executor: "client" }] } });
    await flush();
    expect(speaker.played).toEqual(["Off we go."]);
    expect(events.filter((e) => e.type === "tool-call")).toEqual([{ type: "tool-call", calls: [{ callId: "c1", name: "jump_to", args: { chapter: "2" } }] }]);
    expect(types(events)).not.toContain("response-done");
    client.returnTools([{ callId: "c1", output: "Moved to Chapter Two: The Pool of Tears." }]);
    const tail = ws.sent.slice(-2);
    expect(tail[0]).toEqual({ type: "conversation.item.create", item: { type: "function_call_output", call_id: "c1", output: "Moved to Chapter Two: The Pool of Tears." } });
    expect(tail[1].type).toBe("response.create");
    expect(tail[1].response.instructions).toBe("Be Alice.");
    // live: a function_call_output item (or a note in the instructions) is not seen; a user input message is
    const note = tail[1].response.input[0].content[0].text as string;
    expect(tail[1].response.input[0].role).toBe("user");
    expect(note).toContain('who said: "jump to chapter two"');
    expect(note).toContain("- jump_to: Moved to Chapter Two: The Pool of Tears.");
    ws.recv({ type: "response.created", response: { id: "r2" } });
    ws.recv({ type: "response.output_text.delta", response_id: "r2", item_id: "m2", delta: "[contentment] Here we are. " });
    ws.recv({ type: "response.done", response: { id: "r2", status: "completed", output: [{ type: "message", role: "assistant" }] } });
    await flush();
    speaker.end(); await flush();
    expect(speaker.played).toEqual(["Off we go.", "Here we are."]);
    expect(captions(events).at(-1)).toEqual(["Off we go. Here we are.", false]); // the preamble stays in the bubble
    speaker.end(); await flush();
    expect(types(events).filter((t) => t === "playback-drained")).toHaveLength(1);
    expect(captions(events).at(-1)).toEqual(["Off we go. Here we are.", true]);
  });

  it("pause / resume hold the sentence speaker; a TTS failure ends the answer with an error and cancels the response", async () => {
    const { client, ws, events, speaker } = await openClient();
    client.respond("x", "q");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_text.delta", response_id: "r1", item_id: "m1", delta: "[neutral] One. [neutral] Two. " });
    await flush();
    await client.pausePlayback();
    await client.resumePlayback();
    expect([speaker.pauses, speaker.resumes]).toEqual([1, 1]);
    speaker.fail("Speech request failed (503)");
    await flush();
    expect(events.at(-1)).toMatchObject({ type: "error", message: expect.stringMatching(/Speech request failed/) });
    expect(ws.types().at(-1)).toBe("response.cancel");
    ws.recv({ type: "response.output_text.delta", response_id: "r1", item_id: "m1", delta: "[neutral] Three. " }); // late text of the failed answer
    await flush();
    expect(speaker.played).toEqual(["One."]); // "Two." and "Three." are never spoken
  });

  it("the server-tool wait and the sentence queue count as a busy turn: a mode change waits for the answer to drain (#141)", async () => {
    const { client, ws, speaker, sockets } = await openClient();
    client.respond("x", "q");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_text.delta", response_id: "r1", item_id: "m1", delta: "[neutral] One. " });
    ws.recv({ type: "response.done", response: { id: "r1", status: "completed", output: [] } });
    await flush();
    client.setServerTurns(true); // hands-free switched on while the answer is still being spoken
    expect(sockets).toHaveLength(1); // not dropped under the answer
    speaker.end(); await flush();
    expect(sockets).toHaveLength(2); // reopened once the answer drained
    expect(sockets[1].readyState).toBe(0);
  });
});

describe("TalkClient audio mode keeps the old behaviour", () => {
  it("a direct-audio answer still plays through the PCM player and drains on silence", async () => {
    const { client, ws, events, player } = await openClient({ answerMode: "audio" });
    client.respond("x", "q");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_audio.delta", response_id: "r1", item_id: "i1", delta: "AAA" });
    ws.recv({ type: "response.output_audio_transcript.done", response_id: "r1", transcript: "Hi." });
    ws.recv({ type: "response.done", response: { id: "r1", status: "completed", output: [] } });
    expect(player.enqueued).toEqual(["AAA"]);
    expect(types(events)).toContain("response-done");
    expect(types(events)).not.toContain("playback-drained");
  });
});

vi.restoreAllMocks();
