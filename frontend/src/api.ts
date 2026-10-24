export type Segment = { speaker: string; text: string; emotion: string | null };
export type TtsChunk = {
  id: string;
  speaker: string;
  voice: string;
  tts_text: string;
  char_start: number;
  char_end: number;
};
export type Paragraph = {
  idx: number;
  section_id: string;
  text: string;
  segments: Segment[];
  tts_chunks: TtsChunk[];
};
export type Character = {
  id: string;
  name: string;
  aliases: string[];
  first_para: number;
  voice: string;
  voice_tags: string;
  persona: string;
  portrait: string | null;
};
export type Book = {
  id: string;
  title: string;
  author: string;
  kind: "novel" | "textbook";
  license: string;
  attribution: string | null;
  sections: { id: string; title: string; start_para: number }[];
  paragraphs: Paragraph[];
  characters: Character[];
  narrator: { id: string; voice: string; is_character: string | null } | null;
};
export type BookSummary = {
  id: string;
  title: string;
  author: string;
  kind: "novel" | "textbook";
  license: string;
  attribution: string | null;
  paragraphs: number;
  sections: number;
  characters: number;
};
export type Health = { ok: boolean; boson_key_set: boolean };

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} failed: ${res.status}`);
  return res.json() as Promise<T>;
}

export const fetchBooks = () => getJson<BookSummary[]>("/api/books");
export const fetchBook = (id: string) => getJson<Book>(`/api/books/${encodeURIComponent(id)}`);
export const fetchHealth = () => getJson<Health>("/api/health");
