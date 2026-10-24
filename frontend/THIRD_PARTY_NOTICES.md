# Third-party notices

Files under `src/audio`, `src/realtime`, `src/state`, `src/ui/DebugDrawer.tsx`, and `test/` are adapted
from https://github.com/boson-ai/higgs-realtime-tutorial, licensed under Apache-2.0
(see `THIRD_PARTY_LICENSE_higgs-realtime-tutorial.txt`). Each copied file carries a header noting this.
The tutorial's `RealtimeClient`, session config and dummy search tools were removed once `src/talk/TalkClient.ts` replaced them (it is new code, written for manual push-to-talk turns, and reuses the audio and event helpers).
