# Spoiler toggle report

Backend: `spoilers: bool = False` on RetrieveRequest (used by /api/retrieve and /api/answer); threaded to aretrieve/retrieve, build_instructions, build_answer_prompt. Full mode = novel and spoilers: whole book + earlier books eligible, story_so_far = "Earlier books:...\n\nWhole book:<last summary>", full-book persona templates. Spoiler-safe prompts unchanged. spoiler_filter with reader_para=None and earlier gives $or(current book, earlier $in). VertexIndex includes earlier books for all novel talk. Recap endpoint untouched.
Frontend: reader/spoilers.ts (per-book localStorage), Stage toggle + "Full book - spoilers on" badge (novels only), api.ts sends spoilers:true only when on.
Tests: backend 262 pass, frontend 140 pass, tsc clean.
Concern: character gate (403 for characters not yet met) is unchanged in full mode; the local Index has no earlier-book chunks (only the Vertex index does).
