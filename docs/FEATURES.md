# Talkback: full features list

Every feature below was checked against the code at commit `7158c68` on `main` (main files in brackets), against `docs/internal/NOTES.md`, and against the GitHub issues and PRs (numbers in the Issues / PRs column). Paths are relative to the repository root: `be/` = `backend/app/`, `fe/` = `frontend/src/`. How each feature works is in [HOW-IT-WORKS.md](HOW-IT-WORKS.md); the system design is in [ARCHITECTURE.md](ARCHITECTURE.md).

**Status key:** **works** = in code and tested · **partial** = in code, with a limit noted · **gap** = not built, or the issue says done but the code is missing · *(unverified live)* = in code and unit-tested, but NOTES says nobody checked it against the live service.

## Counts

| Area | Features |
|---|---|
| 1. Library & uploads | 19 |
| 2. Reading / read mode | 15 |
| 3. Voices, faces & lip-sync | 21 |
| 4. Talkback: talk to characters | 24 |
| 5. Voice agent & tools | 14 |
| 6. Recap & summaries | 8 |
| 7. Textbooks | 12 |
| 8. Expressive delivery | 11 |
| 9. Upload processing pipeline | 22 |
| 10. Accounts, privacy & security | 15 |
| 11. Public processing service (for clones) | 8 |
| 12. Deployment & ops | 9 |
| 13. Developer experience | 13 |
| **Total** | **191** |

By status: **177 works** and **14 partial or gap**. The partial ones are L14, R15, V18, V20, T6, T12, T20, X6, E6, E10, E11 and C9. The real gaps are R7 (the #44 server position sync is missing from the code) and X12 (tables). Open issues and other known gaps are listed in the last section.
Tests: the backend has about 1,080 test functions in 80 files (pytest, every provider faked). The frontend has about 675 `it`/`test` cases in 80 files (vitest).

---

## 1. Library & uploads

| ID | Feature | What it does for the user | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| L1 | Built-in sample books | Five fully processed public-domain books ship and work with only a Boson key: Alice, Oz, Peter Pan, Wind in the Willows, A Christmas Carol | offline preprocess (Claude/Gemini) | — | works | [data/books/*, README] |
| L2 | Owner-only Harry Potter 1-7 | The owner's private HP series (gitignored, never shipped). Only the owner's account sees it on the deployed site | OWNER_ONLY_BOOKS=hp* | — | works | [be/users.py is_owner_only] |
| L3 | Novels / Textbooks shelves | The library has two columns of equal width, sorted by title. Series come first | — | #63 | works | [fe/shelves.ts groupShelves, fe/pages/Library.tsx] |
| L4 | Series stacks | Volumes of a series stack into one card in volume order. Stack depth shows the book count (up to 4). The blurb reads "7 books by X, from A to B" | — | #63 #110 | works | [fe/shelves.ts, Library.tsx] |
| L5 | Add book dialog | Upload a PDF or .txt with Kind set to Auto-detect, Novel or Textbook. The form is centred | — | #63 #91 | works | [Library.tsx, /api/books/upload] |
| L6 | Auto title & author | Fills title and author from PDF metadata or a Gutenberg header, else asks Gemini Flash (title page). Several authors are split and joined with commas | Gemini Flash (Vertex) | #83 #95 | works | [be/enrich/describe.py, /api/books/describe] |
| L7 | Multi-file / parallel uploads | Drop several files at once. Each gets its own card and progress bar. Up to 6 queued per account | — | #18 #149 | works | [fe/uploads.ts, be/main.py UPLOAD_QUEUE_MAX] |
| L8 | Duplicate-upload block | Re-uploading the same file gives a 409 with "Open the existing copy". Uploading a book that is being deleted is also blocked | per-account file hash | #82 | works | [main.py _duplicate, users.upload_hashes_json] |
| L9 | Narrator-first availability | An upload can be read with the narrator in seconds. Voices, recap and characters switch on as each background stage lands | — | #46 | works | [main.py upload, enrich/pipeline.py] |
| L10 | Card progress & status notes | Each card shows stage, % progress, refused/error messages and degraded notes ("N recap notes could not be written") | — | #152 | works | [fe/pages/VolumeNote.tsx, /api/books/{id}/status] |
| L11 | Live list refresh | The library refreshes every 10 s and skips re-rendering when nothing changed (keeps scroll and open stacks) | — | — | works | [fe/liveList.ts] |
| L12 | Polling survives restart | Status polling waits out a backend restart (502/503/504, no response) for up to a minute | — | #156 | works | [fe/api.ts, test/pollRestart.test.ts] |
| L13 | Try again | A failed, refused or interrupted job shows "Try again", which resumes from the saved stages. A stale Try again on a finished book reports it as ready | — | #138 #145 #155 #157 | works | [VolumeNote.tsx, /api/books/{id}/enrich] |
| L14 | Move to Textbooks / Novels | Moves an upload between shelves. To Textbooks re-reads the PDF to restore formulas. To Novels runs character enrichment | Gemini | #144 | partial (figures are not restored, **#148**) | [shelves.ts moveLabel, /api/books/{id}/kind, ingest_upload.rebuild_textbook_text] |
| L15 | Delete an uploaded book | The card disappears at once. Vertex datapoint deletes and file removal run in the background and resume after a retry | Vertex Vector Search | #48 #58 #64 | works | [main.py DELETE /api/books/{id}, deleting.json] |
| L16 | Hide / restore built-in books | Built-in books cannot be deleted, but each account can hide them. "Hidden books (N)" restores them | — | #62 | works | [/api/books/{id}/hide, /restore, users.set_hidden] |
| L17 | Card action menu | Move / Delete / Hide in a keyboard-navigable menu (arrow keys wrap) | — | #63 | works | [shelves.ts cardActions, wrapIndex] |
| L18 | Robust listing | One unreadable book.json no longer takes the whole library down | — | #127 | works | [be/books.py, tests/test_books_listing_robust.py] |
| L19 | Favicon | Talkback favicon on every version | — | #15 #42 | works | [frontend/public] |

## 2. Reading / read mode

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| R1 | Read aloud with word highlighting | Each word lights up as it is spoken. Timestamps are aligned to the paragraph text, and numbers are spelled out so they align | **Higgs TTS 3** with timestamps | — | works | [fe/reader/ReaderEngine.ts, be/tts_cache.py, be/textnorm.py, /api/tts] |
| R2 | TTS clip cache | Every clip is made once (key = sha256 of model, tts_text, voice…) and served from `/media/tts`, so replays cost nothing | Boson TTS | — | works | [be/tts_cache.py, main.py mount] |
| R3 | Player bar | Play/pause, previous/next sentence, previous/next chapter, speeds 0.75x-1.5x | — | — | works | [fe/reader/PlayerBar.tsx] |
| R4 | Keyboard shortcuts | Hold Space to talk, K play/pause, ←/→ sentence, [ ] speed, T switches Read/Talk, ` opens the debug drawer | — | — | works | [fe/pages/Reader.tsx L285-375, ui/DebugDrawer.tsx] |
| R5 | Prefetch | The next chunks are fetched ahead, so a section starts without lag | — | #7 #27 | works | [fe/reader/prefetch.ts] |
| R6 | Instant voice change | A voice change is heard from the next chunk with no long wait | — | #37 #43 | works | [fe/reader/voiceSwitch.ts] |
| R7 | Reading position | Remembers where you are in each book and follows the text with auto-scroll | localStorage | #44 | **partial / gap**: the position is kept per browser (localStorage). #44 is closed, but there is no server-side sync route yet | [fe/reader/position.ts] |
| R8 | Chapter list | Click to jump. Titles lose stray symbols and page numbers but keep chapter numbers. Figure text, equation numbers and running headers never become chapters | — | #65 #84 | works | [fe/reader/ChapterList.tsx, cleanTitle.ts] |
| R9 | Nested chapter tree (textbooks) | A collapsible tree by section level instead of a flat list of 368 rows. Novels stay flat | — | #39 #57 | works | [fe/reader/chapterTree.ts] |
| R10 | Spoilers toggle | Per book, off by default. Off: characters know only what you have read. On: whole-book knowledge, every character unlocked, web search allowed | server-enforced | #94 | works | [fe/reader/spoilers.ts, main.py spoilers flag] |
| R11 | Auto-resume | After an answer, reading continues from the same sentence (toggle) | — | — | works | [PlayerBar.tsx "Auto-resume"] |
| R12 | Read / Talkback modes | Switch between the page and the full conversation view (T) | — | — | works | [Reader.tsx, reader/TalkbackView.tsx] |
| R13 | Friendly media errors | Blocked autoplay ("Tap play to continue") and a denied mic give plain messages instead of raw DOMExceptions | — | #4 #26 | works | [fe/audio/mediaErrors.ts] |
| R14 | Silent mode | Without a Boson key the reader still runs at an estimated reading pace | — | — | works | [position.ts silentDurationMs] |
| R15 | Demo mode `?demo=1` | Plays only pre-rendered audio and never calls Boson for speech. Opens a preset position with a pinned speaker | prerendered TTS/avatar | — | **partial**: hard-wired to `hp1-sorcerers-stone` + Hagrid, which a clone does not have | [fe/demo.ts, main.py cached_only 409] |

## 3. Voices, faces & lip-sync

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| V1 | Two voices / One voice | Novels: Two voices (default) reads every speaker in a male or female voice by gender. One voice reads everything, dialogue included, in the narrator's voice. Old "Full cast" is read as two | Boson TTS presets | #85 #93 | works | [be/cast.py, fe/reader/castLogic.ts] |
| V2 | Narrator choice | Pick whether the male or the female voice narrates | — | #86 #93 | works | [castLogic.ts narratorGender] |
| V3 | Textbooks: one voice | Textbooks have one narrator card | — | #85 | works | [cast.py] |
| V4 | Offline gender inference | Works out each character's gender from titles, first-name lists, tag pronouns and the cast preset, with no LLM | — | #85 | works | [be/genders.py, enrich/attribute.infer_genders] |
| V5 | Per-character gender override | Change a character's gender in the Character Control panel (Two voices only) | — | #93 | works | [cast.py, VoiceLab/VoicePicker] |
| V6 | Visual voice picker | Voice cards (voice + face) instead of a dropdown. Keyboard radio navigation | — | #86 | works | [fe/reader/VoicePicker.tsx] |
| V6b | Character Control panel | One panel, opened from the player bar and the Talkback view, holds voice mode, narrator, voice and face cards, genders, cloning and faces (formerly "Voices") | — | #93 #111 #164 | works | [fe/reader/VoiceLab.tsx, PlayerBar.tsx, TalkbackView.tsx] |
| V7 | Cast settings saved on the server | Mode, voices, faces and genders are stored per book (per account on the live site) | — | — | works | [GET/PUT /api/books/{id}/cast, users.cast_json] |
| V8 | Voice cloning | Record or upload a sample, type what you said, tick the consent box and create a voice, optionally with a face. Recordings are converted to WAV | **Boson voices API** | #108 #111 | works | [fe/reader/LabDialogs.tsx, audio/toWav.ts, /api/voices/clone] |
| V9 | Delete voices & faces | Delete cloned voices and uploaded faces. A borrowed face is unlinked, not deleted | Boson voices API | #51 #59 | works | [DELETE /api/voices/{id}, /api/faces/{id}] |
| V10 | Face library | Upload portraits (PNG/JPEG/WebP checked by magic bytes, up to 5 MB) and name them. Separate Add voice and Add face dialogs | — | #111 | works | [be/faces.py, /api/faces] |
| V11 | Camera portrait | Take the portrait with the webcam (preview, retake, use photo). Shows the upload requirements | getUserMedia | #107 | works | [fe/reader/CameraCapture.tsx, audio/camera.ts] |
| V12 | Auto-crop head & shoulders | Gemini finds the face box. You adjust a square crop with mouse or keys (arrows move, +/- resize) | **Gemini Flash** vision | #109 | works | [be/face_detect.py, fe/reader/FaceCropper.tsx, /api/faces/detect] |
| V13 | Animate a face | Renders the face's speaking and idle loops once | **Higgs Avatar** | — | works | [/api/faces/{id}/animate, scripts/make_face_loops.py] |
| V14 | Streaming lip-sync, always on | Every line spoken by a face (read mode, answers, recaps) is lip-synced. Streamed as fMP4 over MediaSource, so it starts before the render ends. No toggle | **Higgs Avatar** `/v1/videos` streaming | #33 #35 #80 #92 #52 | works | [fe/talk/avatarStream.ts, clipVideo.ts, /api/avatar/stream, /api/avatar/talk] |
| V15 | Voice/face sync engine | Start gate, drift control and rendezvous: the voice never rewinds, short back-to-back chunks stay in step, and the face hides when its line ends | — | #32 #66 #104 | works | [fe/talk/startGate.ts, driftController.ts, faceSync.ts, reader/readLipSync.ts] |
| V16 | Lip-sync at 0.75x-1.5x | The clip plays at the voice's playbackRate, and the start gate grows with speed | — | #105 | works | [faceSync.ts, readLipSync.ts] |
| V17 | Any voice × any face | Each request carries the `face_id` on screen, resolved only in the caller's own library | Higgs Avatar | #113 | works | [main.py _lipsync_face] |
| V18 | Fallbacks | No face, an unbound face, or a failed or stalled render falls back to the talk loop or the still. iOS Safari (no MSE) gets render-then-play | — | #19 #122 | partial (iOS slower; Realtime voice answers are audio-only, no lip-sync) | [faceSync.ts, reader/speechLevel.ts] |
| V19 | Lip-sync cache & metering | Clips are cached per account and line. Avatar seconds come from mp3 frame headers (fixes a 4x under-count) | — | #79 | works | [be/mp3dur.py, users.lipsync_dir] |
| V20 | Voice baking (CLI) | Bakes a character's pitch/speed tags into a custom Boson voice, so read mode and Realtime sound the same | Boson TTS + voices | #69 | partial (script only, no UI) | [be/voice_bake.py, scripts/bake_voices.py] |

## 4. Talkback: talk to characters

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| T1 | "Talk to" picker | Choose who to talk to: the narrator or a main character. Characters not yet met are locked ("appear later in the story") unless spoilers are on. The server returns 403 too | — | — | works | [fe/reader/Stage.tsx, position.splitCharacters, main.py] |
| T2 | Push-to-talk | Hold Space or the mic button and release to send. A fresh ephemeral key is minted per connection | **Higgs Realtime** (STT higgs-stt-3.1), manual turns | #1 #25 | works | [fe/talk/TalkClient.ts, /api/realtime/token] |
| T3 | Typed questions | Type a question box. On the Gemini path it streams NDJSON sentences; otherwise it is sent as a Realtime `input_text` item | Gemini 2.5 Flash / Realtime | #97 | works | [TalkbackView.tsx, /api/answer] |
| T4 | Spoiler-safe grounding | Passages are eligible only if `para_end <= reader_para`, enforced 3 times (VS filter, post-filter, `_finish`). Adds the "story so far" and earlier books of the series | **Vertex Vector Search 2.0** hybrid (gemini-embedding-001 + full-text, RRF) or local BM25 (+ optional e5) | eval in NOTES | works | [be/retrieval.py, gcp/vertex_index.py, gcp/vectorstore.py] |
| T5 | Full-book mode | With spoilers on, the whole book and the "Whole book:" summary are used, with full-book persona prompts | — | — | works | [personas.py, main.py] |
| T6 | Personas in character | Personas with concrete speech habits. Spoiler-safe rules: never state or deny an outcome that is not yet established | Gemini / Claude offline | #16 #61 | partial (eval: about 3 true leaks per 122 questions from model outside knowledge) | [be/personas.py, preprocess/repersona.py] |
| T7 | Answers in the character's voice | Gemini streams sentences. Each sentence goes through `/api/speak` and plays while the next is synthesised. Fast start | Gemini + **Higgs TTS** | #34 #45 | works | [fe/talk/sentencePlayer.ts, /api/speak] |
| T8 | Answer backend selection | Gemini when GCP or the public service is available, otherwise Boson Realtime writes and speaks the answer. `/api/health` reports which | — | — | works | [main.py answer_backend] |
| T9 | Talkback conversation view | Full chat view with face, bubbles, status line (no flicker) and "Back to reading" | — | #81 | works | [fe/reader/TalkbackView.tsx] |
| T10 | Barge-in | Press again, press Space or type while it talks: local flush in under 0.15 s, truncate at the ms heard, cancel, then listen. Late output from the old answer is dropped by response id | Realtime `conversation.item.truncate`, `response.cancel` | #10 #100 | works | [TalkClient.interrupt, test/bargeIn.test.ts] |
| T11 | Hands-free mode | Talk with no button. Uses Boson `semantic_vad` with `create_response:false`, falling back to server_vad, then a local RMS gate. 300 ms pre-roll keeps the first words | Realtime turn detection | #114 #116 #135 #139 | works | [fe/talk/handsFree.ts, TalkController serverTurns] |
| T12 | Hands-free echo protection | Mic hold while the character speaks (+600 ms tail). Barge-in uses an echo-relative threshold. `input_audio_buffer.clear` per turn, so questions do not pile up | browser AEC/NS/AGC | #103 #149 #151 #159→#162 | partial (headphones advised; laptop-speaker echo not verified live; #162 lowered thresholds to 0.04 / echo ratio 1) | [handsFree.ts, TalkClient.setMicHold] |
| T13 | "I didn't catch that" | Fast failure for empty or missing transcripts. Never sends an empty question. After 2 empty turns, switches to the local gate | — | #99 #135 | works | [TalkController] |
| T14 | Voice-timed captions | Realtime captions appear in step with playback (`.length` fragments). An interrupted bubble is trimmed to what was heard | Realtime transcript deltas | #135 #139 | works | [TalkClient, chatLog.cutHeard] |
| T15 | Conversation memory | Each character remembers your chat across turns and reconnects (last 6 lines). "{name} remembers this chat (N lines)". Interrupted replies are marked in the history | Realtime history seeding / Gemini `history` | #71 | works | [fe/talk/chatLog.ts HISTORY_TURNS, test/priorTurns.test.ts] |
| T16 | Clear chat | Deletes the chat, and the character forgets it | — | — | works | [TalkbackView.tsx] |
| T17 | Play/pause & replay on every reply | Pause or resume a live answer (voice + face). Replay finished replies at no cost. Only one plays at a time | — | #67 #90 | works | [fe/talk/replay.ts, sentencePlayer.ts] |
| T18 | Per-character voice & face | Remembers which voice and face each "Talk to" character uses | — | #106 | works | [castLogic.ts talkVoice/talkFaceId] |
| T19 | Read-mode mic | Ask from read mode: the answering face takes the stage and chat memory is kept per speaker | — | — | works | [fe/reader/readFace.ts, test/readModeMemory.test.ts] |
| T20 | Emotion in answers | Each Gemini sentence carries an emotion marker, sent to TTS as `<|emotion:X|>`. Realtime is told to show feeling in its voice. The character reacts to the reader's tone | Higgs TTS emotion tags | #14 #125 | partial (avatar expression not controllable; reader emotion is not detected from audio) | [be/emotion.py, personas.py, fe/talk/answerEmotion.ts] |
| T21 | Multilingual answers | Answers in the reader's language. Non-English questions are translated to English for search. Language badge "Detected from what you said". Transcription hint after 2 clear questions | Gemini translate, Realtime `transcription.language` | #11 #12 #13 #120 #124 | works (voice commands are English only) | [be/multilingual.py, fe/talk/language.ts] |
| T22 | Non-English TTS | Answers in 102 documented languages are voiced with `language` + `normalize`. The caption highlights the whole sentence (no timestamps) | Higgs TTS 3 | #135 #137 | works | [be/tts_lang.py, /api/speak] |
| T23 | Web search in answers | Textbooks and spoilers-on novels can use Google Search grounding. Source links and Google Search suggestions show in a sandboxed iframe. Daily cap | **Gemini Grounding with Google Search** | #142 | works (Realtime path never searches; Gemini decides when to search) | [be/websearch.py, fe/reader/SourceLinks.tsx] |
| T24 | Connection & quota handling | Plain messages for Boson connection limit (1013), key (3000) and billing (4429). 3-minute playback ring buffer, so long answers do not skip | Realtime | #1 #25 | works | [TalkClient.ts, audio/playback.ts] |

## 5. Voice agent & tools (Realtime function calling)

16 client tools in [fe/talk/tools.ts]. The tool set depends on the book kind (`toolDefsFor`). Boson's server `web_search` tool is never declared.

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| A1 | Voice agent framework | Ask by voice and the character acts on the reader. A client-executed `function_call` becomes a tool call, its output is returned, and the character speaks a confirmation | **Higgs Realtime tool calling** | #72 #102 #112 | works | [tools.ts runTool, TalkController.returnTools] |
| A2 | Navigation (novels & textbooks) | `jump_to` (chapter number, title, Roman numeral, "last", "bookmark" or a quoted passage), `read_chapter`, `resume_reading`, `pause_reading` | — | #72 | works | [tools.ts] |
| A3 | Go back / repeat | `go_back`: restart the paragraph, or go back n paragraphs or n sentences ("say that again") | — | — | works | [tools.ts] |
| A4 | Sections (textbooks) | Jump by number ("2.3", "two point three") or title words, plus `next_section` / `previous_section`. Fills numbering gaps | — | — | works | [fe/talk/sections.ts] |
| A5 | Find topic | `find_topic` "where is BM25 explained". Title match, else tf-idf over paragraphs, in the browser (no paid call) | — | — | works | [sections.ts / tools.ts] |
| A6 | Spoken tutor quiz | `quiz_me`: 3 questions on this section/chapter, one at a time, right or wrong with a reason, then a score | Realtime (or Gemini via `ask`) | #75 | works *(unverified live)* | [tools.ts, TalkController.quiz] |
| A7 | Explain here | `explain_here`: explains the current paragraph or nearby equation/figure in 2-4 sentences | — | — | works | [tools.ts] |
| A8 | Delivery controls | `set_speed` (1.25 / faster / slower / normal), `set_voice_mode` (one/two), `set_narrator` (male/female), novels only | — | #72 | works | [tools.ts] |
| A9 | Recap tool | `open_recap` chapter/book/series, plus "summarize chapter 4" for any chapter of a textbook | — | #89 #129 | works | [tools.ts] |
| A10 | Bookmarks | `bookmark` saves the spot. "Jump to bookmark" returns to it | — | #72 | works | [tools.ts] |
| A11 | Show cast | `show_cast` opens the Character Control panel (novels) | — | #72 | works | [tools.ts] |
| A12 | Tool spoiler guard | In novels with spoilers off, a target past the reader is refused and the model says why. Textbooks are exempt | — | #72 | works | [tools.ts resolveJump] |
| A13 | Typed / Gemini command fast path | `parseIntent` matches plain phrasings ("read chapter 3", "a bit faster", "quiz me", "which section covers X") without a model call. Strips filler words from transcripts | — | #72 #102 | works (English only) | [tools.ts parseIntent] |
| A14 | Tool chips | "Jumped to Chapter II" chips show in the chat but are never sent back as history | — | #72 | works | [chatLog.ts role "tool"] |

## 6. Recap & summaries

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| S1 | Recap menu | Last chapter / This book so far / The series so far, built from stored summaries with `upto_para <= para`. No model call | — | #89 | works | [be/recap.py, fe/reader/RecapMenu.tsx, /api/books/{id}/recap] |
| S2 | Recap in chat, read aloud | Posted as a narrator message and read aloud with lip-sync. Not sent to the model | Higgs TTS + Avatar | #47 #89 | works | [fe/reader/readAloud.ts, RecapPanel.tsx] |
| S3 | Stop / resume mid-sentence | Stop pauses and keeps the place. Resume continues mid-sentence. Long recaps no longer time out | — | #68 #87 #88 | works | [sentencePlayer.ts pause] |
| S4 | Earlier-book summaries | End on a full sentence, with a "More" button | — | #49 #60 | works | [recap.py] |
| S5 | Novel "story so far" notes (uploads) | Parallel per-chapter checkpoint notes, each written only from text up to its checkpoint. Failed notes get a second pass | Gemini (Flash-Lite on live) | #152 | works | [be/enrich/summaries.py] |
| S6 | Series recap | Closing summaries of earlier volumes in the same series (built-in with built-in, uploads with the same owner's uploads) | — | #115 #118 | works | [books.earlier_in_series] |
| S7 | Textbook study-guide notes | One note per chapter and per numbered subsection (3-6 sentences, formulas in words). Recap "This chapter" or any section, from anywhere in the book | Gemini fast tier | #129 | works | [be/enrich/textbook.py, scripts/summarize_textbook.py] |
| S8 | Offline summaries (built-in books) | Rolling incremental notes for the built-in books | Claude Sonnet (then) / Gemini | — | works | [preprocess/summaries.py] |

## 7. Textbooks

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| X1 | Novel vs textbook detection | One cheap call on 3 samples, with a heuristic fallback (dialogue ratio, Figure/Exercise markers, numbered headings) | Gemini fast | #2 #28 | works | [be/enrich/detect.py] |
| X2 | Chapter/section detection | Font-size and rich heading detection. Drops contents pages, title page, copyright and running headers | PyMuPDF | #3 #29 #53 | works | [be/ingest_upload.py, parsing.py] |
| X3 | Layout cleanup | Drops captions, footnotes and marker-symbol captions. Reads two columns in order | — | #6 #30 #54 | works | [ingest_upload.py] |
| X4 | Code listings | Shown in a code font and skipped when reading aloud | — | #38 #55 | works | [BookText.tsx para-code, ingest_upload.py] |
| X5 | Display equations | Detected by math fonts and spacing, cropped to PNG. Gemini returns LaTeX and a spoken reading. KaTeX render (crop or text fallback). Numbered ("Equation three point four") | **Gemini vision**, KaTeX | #5 #41 #119 #124 #131 | works | [be/enrich/equations.py, fe/reader/equation.ts, /api/books/{id}/equations] |
| X6 | Inline formulas | Formulas inside sentences are marked, cropped and read by Gemini. Inline KaTeX. The spoken reading is highlighted | Gemini vision | #132 #144 | partial (formulas split over several PDF lines are only partly marked) | [ingest_upload.py, tts_cache.align_timestamps] |
| X7 | Math speech in words | Simple math read aloud ≠, ←, dotted section numbers, digits after the point | — | #5 #31 #56 | works | [be/textnorm.py] |
| X8 | Figures in the reader | Found from their "Figure N" captions, cropped, shown inline with click to enlarge (Esc closes). Figure text no longer leaks into the body | PyMuPDF | #40 #147 | works (new uploads only) | [ingest_upload._take_figures, fe/reader/Figure.tsx, /api/books/{id}/figures] |
| X9 | Read captions toggle | Skip or read figure captions while reading. Saved positions stay valid | — | #40 | works | [PlayerBar.tsx, position.nextChunk] |
| X10 | Textbook voice agent | Section tools, find topic, quiz, explain (see A4-A7). No spoiler gate | Realtime tools | #75 | works | [tools.ts] |
| X11 | Faster textbook ingest | Concurrent indexing. The book is published before indexing finishes | Vertex | #36 #46 | works | [main.py] |
| X12 | Tables | Tables ("Table N") are still dropped | — | — | **gap** (by design so far) | [NOTES #40 section] |

## 8. Expressive delivery (Higgs TTS tags)

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| E1 | Emotion per segment | Each dialogue segment carries one of 20 emotions from segmentation | `<|emotion:X|>` | — | works | [enrich/segment.py, schema.Emotion] |
| E2 | Whisper / shout styles | Rule-based from the adjacent speech tag (hissed, bellowed…) or all-caps "!" lines. Ambiguous cases get no tag | `<|style:whispering/shouting|>` | #117 | works | [be/delivery.py] |
| E3 | Sound effects | Laughter, sigh, crying, sniff, cough, screaming, sneeze, humming, only before matching onomatopoeia in dialogue. One per chunk, never burping | `<|sfx:…|>` | #73 #117 #135 #137 | works | [delivery.py] |
| E4 | Expressiveness | `expressive_high` for shouted lines, `expressive_low` for whispers | prosody tags | #73 | works | [delivery.py] |
| E5 | Pauses | `long_pause` at section starts and after scene separators. `pause` before dialogue that follows narration | prosody pause tags | #73 | works | [ttschunks.lead_pause] |
| E6 | Singing | Songs and sung verse after a "sang:" lead-in are sung (Alice lullaby, Sorting Hat…). Recited verse is left alone | `<|style:singing|>` | #74 | partial (known misses: Toad's song, some stanzas) | [delivery.is_sing_lead_in, ttschunks.sing_offset] |
| E7 | Natural reading speed | Opt-in. 0.75x/1.25x/1.5x use Boson speed tags instead of browser playbackRate | `speed_slow/fast/very_fast` | #78 | works | [fe/reader/naturalSpeed.ts, delivery.apply_speed] |
| E8 | Tag residue stripping | Removes tag words glued to timestamps, so highlighting stays aligned | — | — | works | [delivery.strip_tag_residue] |
| E9 | Chunk-rules auto-upgrade | Books stamped with an older `CHUNK_RULES_VERSION` (now 2) are rechunked on load and at startup. Only changed chunks are re-voiced | — | #133 #143 | works | [ttschunks.rechunk_book, books.schedule_all_upgrades] |
| E10 | Sentence-aligned chunking | Chunks of at most 280 characters, one voice per speaker | — | — | partial (number expansion after packing can reach ~500 chars) | [be/ttschunks.py] |
| E11 | Character prosody tags | Per-voice pitch/speed tags (presets). Dropped in Two voices mode | prosody tags | #85 | partial | [cast.py tags_for] |

## 9. Upload processing pipeline

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| P1 | PDF / text ingest | Paragraphs, sections and markers (heading, code, equation, inline math, figure). Limits: 20 MB, 2,000 pages, 5 M chars | PyMuPDF | #3 | works | [be/ingest_upload.py, parsing.py, uploadlimit.py] |
| P2 | Kind detection | See X1. Stored with confidence and method | Gemini fast | #2 | works | [enrich/detect.py] |
| P3 | Cast pass | One call proposes up to 8 main characters with preset voices and aliases. `first_para` is computed from the text, plus series and volume number | Gemini (smart tier) | #115 | works | [be/enrich/cast.py] |
| P4 | Dialogue segmentation | 10 paragraphs per call. Segments must tile the paragraph verbatim. Retries alone once, then falls back to the narrator. Restores dropped quote marks | Gemini fast | — | works | [be/enrich/segment.py] |
| P5 | Long-novel segments | Segments are rebuilt as slices of the paragraph, so long novels no longer fail | — | #146 | works | [segment.py] |
| P6 | Heuristic attribution | Free pass that gives untagged dialogue (back-and-forth, action beats) to the right character | — | #17 #61 #115 | works | [be/enrich/attribute.py] |
| P7 | Minor voices | Uncast speakers ("said the gnome") get a lower or higher stand-in voice by inferred gender instead of the narrator | — | #70 | works | [be/enrich/minor.py] |
| P8 | Early series detection | One cheap call right after publish sets the series, so the volume stacks within seconds | Gemini fast | #145 #150 | works | [be/enrich/series_early.py] |
| P9 | Series reconciliation | Same-author uploads are asked once whether they belong to a known series (first volumes such as Dune). Also runs at startup | Gemini fast | #134 #136 | works | [be/enrich/series_link.py] |
| P10 | Per-owner series grouping | Uploads join only the same owner's series, never built-in books | — | #115 #118 | works | [books.same_library] |
| P11 | Summaries stage | See S5 / S7 | Gemini | #129 | works | [pipeline.py] |
| P12 | Index stage | Vertex re-index with speakers, or local chunks (BM25 / e5) | **gemini-embedding-001 + Vector Search** | #36 #46 | works | [gcp/indexing.py, retrieval.py] |
| P13 | Progressive saves | Every stage saves book.json atomically (per-file write lock, temp file + replace) | — | #133 | works | [books.write_lock, save_book] |
| P14 | Parallel processing | Many books at once (ENRICH_PARALLEL=1000), each with up to UPLOAD_CONCURRENCY=24 calls | — | #18 #149 #152 | works | [config.py, main.py] |
| P15 | Resume after restart | Interrupted jobs restart at startup as their owner. Each resume counts as an attempt, so a crashing book is not retried forever | — | #138 | works | [main._resume_enrichments, TOO_MANY_TRIES] |
| P16 | Resilience / retries | One transient-error policy (network, 429, 5xx; never 401/402/403 or quota) with jittered backoff. Windows PermissionError retry | — | #152 #153 | works | [be/retrying.py, books.retry_permission] |
| P17 | Index failures never fail a book | Reports done with "search uses basic mode" and falls back to BM25. Startup re-indexes up to 20 books. Old index-only failures are settled | — | #152 #153 #154 | works | [main._retry_failed_indexes] |
| P18 | Gemini pacer | One process-wide AIMD concurrency limit (8 → 32, halves on a 429, honours Retry-After). Interactive calls go first | — | #158 | works | [be/pacer.py] |
| P19 | Flash-Lite for books | `ENRICH_GEMINI_MODEL` lets book processing use gemini-2.5-flash-lite. Deploy keeps it | Gemini 2.5 Flash-Lite | #160 #163 | works | [enrich/llm.py, scripts/deploy_cloudrun.sh] |
| P20 | Cost estimate & confirm mode | Estimate before the job. `UPLOAD_CONFIRM=true` shows "Process with characters (~$x)" | — | — | works | [pipeline.estimate, VolumeNote.tsx] |
| P21 | Provider fallback | Claude → Gemini on out-of-credit, auth or overload. Gemini is the live default (Claude credit gone) | Claude / Gemini | — | works | [be/enrich/llm.py] |
| P22 | No-LLM fallback | Without any LLM: name-based cast and narrator reading, with a clear message | — | — | works | [pipeline.py, public_client.py] |

## 10. Accounts, privacy & security

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| C1 | Google sign-in | The live site sits behind Cloud Run IAP. The JWT is verified against Google keys and the audience, and spoofed email headers are ignored. Sign out link | **Cloud Run IAP** | — | works | [be/users.py verify_email, Library.tsx Sign out] |
| C2 | Per-account data | Voices, faces, cast settings, lip-sync clips, hidden books and upload hashes live in `data/users/<hash>/` | — | — | works | [users.py, tests/test_per_user*.py] |
| C3 | Owner-only books | Uploads carry `owner.txt`, and other accounts get 404. `OWNER_ONLY_BOOKS` limits ids to owner emails | — | — | works | [users.can_see_book, guard_book] |
| C4 | Owner migration | First start copies the shared data to the owner's namespace (idempotent). Dry-run script | — | — | works | [users.migrate_to_owner, scripts/migrate_per_user.py] |
| C5 | Delete requires account | Deleting needs a signed-in identity | — | — | works | [users.require_account] |
| C6 | Access-code gate | `TALKBACK_ACCESS_TOKEN` gates paid endpoints. The page asks for the code and has "Forget access code on this device" | — | — | works (no longer deployed; IAP replaced it) | [main.require_access, fe/accessToken.ts] |
| C7 | Keys stay on the server | The browser gets only ephemeral Realtime keys. Secrets are hidden from the settings repr | — | #153 | works | [config.py repr=False, /api/realtime/token] |
| C8 | Rate limits | Per IP or account token buckets: token 10/min, TTS misses 120, clone 5, answer 20, retrieve 30, upload 6, describe 12, avatar 12, face 10, face-detect 12. Trusted proxy uses the right-most XFF | — | — | works | [be/ratelimit.py, main.py] |
| C9 | Upload spend caps | Per-book estimate cap ($4) and cumulative cap ($15), persisted | — | — | partial (**live deploy lifts both to $1000** and grounded cap to 100,000 for the hackathon) | [enrich/spend.py, deploy_cloudrun.sh L91] |
| C10 | Small-call caps | Describe and face-detect reserve cost in the upload budget, with global bucket and daily caps. Grounded answers 100/day | — | #142 | works | [describe.py, face_detect.py, daily_count.py] |
| C11 | Kill switches | `UPLOAD_ENRICH`, `WEB_SEARCH`, `AUTO_RECHUNK`, `MINOR_VOICES`, remove `PUBLIC_API_URL`, close the public service (IAM), budget stop | — | — | works | [config.py] |
| C12 | Lip-sync isolation | `face_id` is resolved only in the caller's library. Clips are served only to their owner | — | #113 | works | [main._lipsync_face, LipsyncFiles] |
| C13 | Portrait isolation | `/portraits` serves only the caller's faces and hides the shared folders | — | — | works | [main.py PortraitFiles] |
| C14 | Copyright guard | HP is gitignored and owner-only. The deploy never syncs portraits or uploads | — | — | works | [.gitignore, deploy_cloudrun.sh] |
| C15 | Search-suggestion compliance | Google's markup is shown unmodified in a script-less sandboxed iframe (no referrer), as Google's display rules require | — | #142 | works | [websearch.clean_suggestions, SourceLinks.tsx] |

## 11. Public processing service (for GitHub clones)

Same image with `PUBLIC_API=true` (entry `app.asgi:app`), Cloud Run service `talkback-public`. It ships **closed** (403 without IAM).

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| U1 | Hosted processing for key-less clones | A clone with only a Boson key gets full upload processing and Gemini typed answers | Gemini on Vertex | #141 | works | [be/public_api.py, be/public_client.py] |
| U2 | Routes | `GET /api/health`; `POST /api/public/enrich` + `GET …/enrich/{job}`; `/answer`; `/translate`; `/detect`; `/describe`; `/series`; `/series-early`; `/faces/detect`; `/equations` (ticket) + `/equations/{ticket}`. Everything else is 404 | — | #141 | works (a duplicate `/series-early` route was removed in #165) | [be/public_api.py] |
| U3 | Caps | Book ≤ $1.5 (job stops at 1.25x). Total ≤ $10. Per IP: 1 book/hour and $2/day. Answers $2/day, 4 streams, 20/min/IP. Small calls 30/min/IP. Face boxes 10/h/IP and 100/day. Grounded answers 50/day (+$0.035 reserve). Equation tickets 2/h/IP, one open per IP, 32 total, ≤ 24 PNGs per request | — | #141 #142 | works | [config.py, public_api.py, DEPLOY.md] |
| U4 | Privacy | No request bodies logged. Results kept in memory for 30 min. Spoiler filtering never leaves the clone (only eligible passages are sent) | — | — | works | [public_api.py, main.py /api/answer] |
| U5 | Client fallbacks | Closed, missing, unreachable or capped service: local heuristic cast with a message saying why. 401/403 marks the service unavailable for a minute, and answers then go through Realtime | — | — | works | [public_client.mark_unavailable] |
| U6 | Re-validation | The clone re-checks that every returned segment tiles its own text, strips injected emotion markers and re-cleans sources | — | #14 #142 | works | [public_client.py] |
| U7 | Feature parity | Series, minor voices, textbook notes, equations, translation, face box, describe and detect all work on a clone | — | #141 (NOTES "Public API parity") | works (needs a redeploy of talkback-public to carry the latest code; unverified) | [tests/test_public_parity*.py] |
| U8 | Open/close switch | Close or open with one IAM binding (allUsers run.invoker). Closing is the kill switch | Cloud Run IAM | #24 | works | [scripts/deploy_public_api.sh, DEPLOY.md] |

## 12. Deployment & ops

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| D1 | One container | The FastAPI image also serves the built frontend at `/`, so there is no CORS setup. No torch in the image | Docker | — | works | [Dockerfile, main.py mount] |
| D2 | Cloud Run `talkback` | us-central1, min 0 / max 1 instance, no CPU throttling, 1 GiB | **Cloud Run**, Artifact Registry, Cloud Build | — | works | [scripts/deploy_cloudrun.sh] |
| D3 | Data bucket | A Cloud Storage bucket mounted at `/app/data` (GCS FUSE) | **Cloud Storage** | — | works | [deploy_cloudrun.sh] |
| D4 | Deploy script | Prints its plan by default and runs only with `--yes`. Refuses a dirty tree. Rsyncs `data/books` except `u-*` and `cast_settings.json`. Image tagged by git sha. Sets env (per-user, Flash-Lite, caps) | gcloud | #145 #163 | works | [deploy_cloudrun.sh] |
| D5 | Public service deploy | Builds if needed, deploys closed (`--no-allow-unauthenticated`) and prints the URL | — | — | works | [scripts/deploy_public_api.sh] |
| D6 | Secrets | Boson key (and the legacy access token) live in Secret Manager | **Secret Manager** | — | works | [deploy_cloudrun.sh] |
| D7 | Budget hard stop | A $50 budget triggers Pub/Sub, and a Cloud Run function unlinks billing at 100% | Budgets, Pub/Sub, Cloud Functions | — | works | [infra/budget_stop/main.py] |
| D8 | Usage logging | Every paid call is logged (Gemini, embeddings, Claude, Boson), to Cloud Logging when deployed. `scripts/usage.py` prints a spend report | — | #79 | works | [be/gcp/usage.py, scripts/usage.py] |
| D9 | Startup warm-up | Health check. Rechunk upgrades, series reconciliation, enrichment resume and failed re-index all start at boot | — | #138 #152 | works | [main.py lifespan, /api/health] |

## 13. Developer experience

| ID | Feature | What it does | Key tech | Issues / PRs | Status | Code |
|---|---|---|---|---|---|---|
| DX1 | Backend tests | About 1,081 tests in 80 files, with every provider faked | pytest | — | works | [backend/tests] |
| DX2 | Tests never call real Google | No GCP project by default, and real genai clients are blocked | — | #161 | works | [tests/conftest.py] |
| DX3 | Frontend tests | About 676 cases in 80 files: fake socket, fake clock and fake media for barge-in, hands-free, lip-sync and tools. Plus `tsc` typecheck | vitest | — | works | [frontend/test] |
| DX4 | Offline pipeline | `scripts/run_all.py --plan` runs ingest_series, summaries, cast, segment, attribute, minor_voices, resegment, repersona, chunk_tts, index_vertex | Claude/Gemini, Vertex | — | works | [scripts/run_all.py, preprocess/*] |
| DX5 | Spoiler eval harness | Questions at reader positions are answered by the real `/api/answer` and judged, with leak and accuracy reports | Claude judge, Gemini | — | works | [preprocess/eval_spoilers.py, data/eval] |
| DX6 | API probes | Probe scripts for TTS, Realtime, Realtime turns, avatar, voice and secret, plus saved captures | Boson | #135 #151 | works | [scripts/probe_*.py, scripts/captures] |
| DX7 | Cost estimates | `estimate_expressive.py` (re-voice cost of rule changes, `--list-sung`), `summarize_textbook.py` estimate, upload estimate | — | #73 #74 | works | [scripts/estimate_expressive.py] |
| DX8 | Pre-render for demos | Pre-renders TTS and avatar clips. `/api/avatars/{book}` lists the clips that exist | Boson | — | works | [preprocess/prerender_tts.py, prerender_avatar.py] |
| DX9 | Demo questions | Positions, expected answers and should-decline questions for Alice | — | — | works | [data/demo/questions.json, docs/DEMO.md] |
| DX10 | Voice/face CLIs | `clone_voice.py --i-have-consent`, `make_face_loops.py`, `bake_voices.py` | Boson | — | works | [scripts/] |
| DX11 | Debug drawer | Backtick opens an event log plus a running estimate of the session's Boson spend (TTS, audio in/out, avatar) | — | — | works | [fe/ui/DebugDrawer.tsx, fe/spend.ts] |
| DX12 | Degrade-gracefully setup | `.env.example` with a key/degrade table. Runs locally without GCP (BM25, Realtime answers). Vite dev proxy | — | — | works | [.env.example, README] |
| DX13 | Issues as source of truth | GitHub issues track all work. NOTES.md logs every decision | — | #50 | works | [docs/internal/NOTES.md] |

---

## Known gaps / not built

| Issue | What | State |
|---|---|---|
| #8 | Podcast-style avatar mini-player (corner player, expand/collapse) | open, not built |
| #9 | Unified talk-while-reading screen (no Read/Talkback switch) | open, not built. Read-mode mic (T19) covers part of it |
| #76 | Group scene: ask 2-3 characters at once | open, not built |
| #77 | Share a character answer as an MP4 clip | open, not built |
| #140 | Non-English uploads (parse, read aloud, talk about non-English PDFs). Splitting, normalization, delivery rules, headings and prompts all assume English | open, not built |
| #148 | Moving a paper to Textbooks restores equations but not figures (`rebuild_textbook_text` omits `figures_dir`) | open, bug |
| (#44) | Server-side reading-position sync. The issue is closed, but the code keeps the position only in the browser (localStorage), so it does not follow you to another browser or device | gap |
| — | Demo mode is hard-wired to HP1, which clones do not have | gap |
| — | Local runs (no Google Cloud) search every book by keywords (BM25): no embeddings ship and uploads get none at runtime yet. Local embeddings for uploads are being added | partial, in progress |
| #159 | With laptop speakers instead of headphones, the lower hands-free barge-in thresholds (#162) can let the character's own voice interrupt it. Headphones are advised | partial |
| — | Gemini on Vertex uses dynamic shared quota, so heavy parallel processing can be throttled (429s). The pacer (P18) slows down and retries, so books finish later but do not fail | limitation |
| — | Tables in textbooks are dropped. Formulas split over several lines are only partly marked | gap / partial |
| — | Checks not yet done on the live service (per NOTES): hands-free echo with laptop speakers, quiz grading, `.length` caption granularity on long answers, whether the public service runs the latest code | unverified |
