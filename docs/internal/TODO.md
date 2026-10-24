# Talkback — to-do list

Parked ideas and open work, in the order we'd likely do them. Nothing here is built yet unless marked.
Judging weights to keep in mind: Use of Higgs Audio 30% · UX 25% · Innovation 25% · Technical execution 20%.

---

## 1. Podcast-style avatar player (UX)
Make the face/avatar behave like the Spotify podcast/video player.

- **Default: mini player in the corner.** A small floating card (bottom-right on desktop, above the player bar on mobile) shows the current speaker's face (talking/idle, audio-driven), their name, and play/pause. The book text stays fully readable.
- **Expand button** (⤢) on the mini player opens an **almost full-screen view**: large face, speaker name, the current sentence as a caption, play/pause/skip, and in Talkback the conversation bubbles and mic.
- **Collapse button** (⤡ or Esc, or swipe down on mobile) shrinks it back to the corner. **Hide** (×) tucks it away entirely; a small tab/button brings it back.
- Smooth animated transition between mini ↔ expanded; remembers the last state per device; works with keyboard and screen readers; respects reduced motion.
- No face assigned → mini player shows the small letter circle (as today).

```
Reading (default)                           Expanded (click ⤢)
┌───────────────────────────────┐           ┌───────────────────────────────┐
│ Chapter 5: The Dementor       │           │                           ⤡  │
│                               │           │          ┌────────┐           │
│ It was a bright cold day...   │           │          │  FACE  │           │
│ ...                           │           │          │ (large)│           │
│                    ┌────────┐ │           │          └────────┘           │
│                    │ (face) │ │           │            Hagrid             │
│                    │ Hagrid │ │           │  "Yeh'll be all righ', Harry" │
│                    │ ⏸  ⤢  × │ │           │        ⏮    ⏸    ⏭    🎤      │
│                    └────────┘ │           └───────────────────────────────┘
│ ⏮ ⏸ ⏭  Speed  Voices  Recap  │
└───────────────────────────────┘
```

## 2. One unified mode: talk while reading (no Read/Talkback switch)
- A single reading screen with a **mic on/off toggle** and a **type box**. Speak or type any time; if it's a question or comment, reading pauses, the character answers, and reading resumes from the same sentence.
- Speaking while a character is answering **interrupts** them (barge-in).
- Who answers: the character you name ("Hagrid, why…"), otherwise the last one picked, otherwise the narrator.
- Hard parts: the app hearing its own voice (pause/duck reading on speech, browser echo cancellation, headphones hint), ignoring noise (local voice detection + minimum length), and cost (detect speech locally before streaming to Boson Realtime, which bills by audio time).
- Fits the "Conversation as the Interface" track and the turn-taking/interruption criteria.

## 3. Interruptions and language switching in Talkback (Higgs specialist)
- **Barge-in:** press the mic (or just speak, with item 2) while a character talks → they stop within ~0.15 s, the bubble is marked "interrupted", it listens immediately, and the next answer knows it was cut off. Both answer paths (Realtime voice; Gemini + Boson TTS).
- **Multilingual:** ask in French/Spanish/Chinese, or switch mid-conversation → answer in that language in the character's voice, language badge on the bubble; translate the question for searching the English book.
- Check Boson docs first (turn detection, `response.cancel`, supported languages); probe with a few cents of Boson.
- **⚠ Check these before building (reminder):**
  - [ ] Which languages Higgs Realtime **understands** (speech → text) and **speaks**, and whether each preset/cloned voice sounds right in them (e.g. Chinese with Harry's voice, with a cloned English voice).
  - [ ] **Code-switching / weaving**: one sentence mixing languages ("Hagrid, 为什么 he hid the egg?") — does Realtime transcribe it, and can TTS speak a mixed reply naturally?
  - [ ] **Switching mid-conversation** works turn to turn with no setting change (Realtime currently auto-detects; confirm).
  - [ ] **Typed questions** in other languages: the Gemini path should answer in the same language — add the prompt rule and test; the realtime typed path too.
  - [ ] **Search**: the books are English — translate non-English questions before searching (BM25 local, Vertex live) so answers stay grounded.
  - [ ] **Faces**: the lip-synced avatar is driven by the audio, so it should match any language — verify mouth shapes look right for Chinese and Spanish clips.
  - [ ] Badge: today's language badge ("Heard: Español") is a guess from the transcript text only (script ranges + common words, `frontend/src/talk/language.ts`); it doesn't steer anything. Decide whether to keep it as a guess or use what Realtime reports.

## 4. Emotion
- **Voice (partly done):** ~8% of book lines carry an emotion tag (anger, fear…), plus whisper/shout/laughter. To do: emotion on Talkback answers, and wider coverage where the text clearly implies it.
- **Avatar face:** check Higgs Avatar docs for any expression/emotion control beyond audio-driven lip-sync; otherwise rely on expressive audio.
- **Reader's emotion from their own voice:** check whether Higgs Realtime/STT reports emotion or tone; if not, infer from the words. Let characters react ("You sound worried, Harry…").

## 5. Smaller items
- **Favicon:** replace the default Vite favicon with a Talkback icon (e.g. an open book with a speech bubble, in the dark green + gold palette). Provide `favicon.svg` plus PNG fallbacks (32×32, 180×180 apple-touch-icon) and a matching `<title>`/theme colour in `frontend/index.html`, so it shows the same everywhere: local runs, GitHub clones (committed in `frontend/public/`) and the live site (ships with the next deploy).
- Dobby's character description (Gemini wrote a generic one; he refers to himself in the third person).
- Harry Potter dialogue: lines without a "said X" tag are still read by the narrator (needs a model with credit to redo).
- Live site: allow 2–3 uploads to process at once instead of one at a time (if several people upload during judging).
- Read-mode face: tune speech-detection thresholds on real (and quiet cloned) voices if the mouth lags.
- Clean-clone test with a real Boson key, end to end, just before submission.

## Submission day
- Make the GitHub repo public; tag the commit to judge.
- Write the form text (one-liner, 200-word description, 200-word "how you built it", team info, known limitations).
- Record the 2–3 minute demo video.
- Open the live link (switch off the sign-in gate) and the public processing service for the judging window; close both after.
