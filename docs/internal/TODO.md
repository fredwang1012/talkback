# Talkback — to-do list

**GitHub issues are the single source of truth:** https://github.com/fredwang1012/talkback/issues
This file is a snapshot (2026-10-22) grouped by priority. `main` is protected: every change goes through a branch and a PR.
Judging weights: Use of Higgs Audio 30% · UX 25% · Innovation 25% · Technical execution 20%.

## Open PRs (not merged)
- **#45** Talkback faster answers + face in sync (#32, #33, #34). User test: voice fine; avatar still not quite synced and now twitches (likely the 80 ms seek-based drift correction in `talk/faceSync.ts`; fix plan in the PR comment). Fix before merging.
- **#46** Faster upload (#36): concurrent embedding/upserts, book readable before indexing (BM25 fallback), "Indexing N / total" progress, one indexing job at a time. Not yet tested by the user with a real upload.

## Bugs still open
- #3 textbook leftovers: contents without dot leaders read aloud, title page split into 3 sections, copyright line as a section, duplicate "2.4 … 39".
- #5 math leftovers: combining-character ≠ ("p1̸ =") garbled, "5.3.1" read as "five.three.one".
- #6 layout leftovers: captions starting with a symbol ("◮Figure 2.10"), per-page running headers ("… skip pointers 37").
- #32 / #33 / #34 Talkback face timing, lip-sync drift, answer speed (PR #45).
- #35 read-mode face is not lip-synced (generic loop driven by audio level).
- #36 upload speed (PR #46).
- #49 recap: earlier-book summaries cut mid-sentence (120-word trim).
- #16 Dobby persona; #17 HP untagged dialogue read by narrator.

## Features (ranked roughly by judging impact)
- #47 voice recap (narrator reads the recap aloud).
- #10 barge-in; #11–#13 multilingual (check Higgs support first); #9 unified talk-while-reading; #14 emotion.
- #8 podcast-style avatar player.
- #40 figures in the reader; #41 LaTeX equations rendered and spoken; #38 skip/summarise code listings; #39 nested chapter list.
- #44 save reading position to the Google account (live site); #48 delete an uploaded book; #18 concurrent uploads; #19 face threshold tuning.

## Submission day (#20–#24)
Clean-clone test with a real Boson key → make the repo public and tag → form text → 2–3 min demo video → open the live link and public API for judging, then close both.

## Done on 2026-10-22 (merged)
#1 voice buffer 12 s → up to 3 min (long answers no longer skip) · #2 novel/textbook detection · #3 chapter list (partial) · #4 tap-to-play instead of the raw autoplay error · #5 spoken math (partial) · #6 layout (partial) · #7 section-start lag / first-word skip · #15 favicon · #37 instant voice switching.
