import { createClipFollower, type FollowAudio } from "./clipFollow";
import { tryPlayQuietly } from "../audio/mediaErrors";
import { useEffect, useRef } from "react";
import type { Face as FaceT } from "../api";
import type { FaceSync } from "../talk/faceSync";
import type { TalkUi } from "../talk/useTalk";
import type { LiveFaceUi } from "../talk/useLiveFace";
import { FaceView, ReadFace } from "./FaceView";
import { LiveFaceView } from "./LiveFaceView";
import { answerOnStage, stageFace } from "./readFace";

/** Read-mode lip-sync (#80, always on since #92): the persistent clip video the sync drives; `shown` once a clip has
 * a frame and is in step with the voice (otherwise the talk loop or the still face shows). */
export type ReadLipSyncUi = {
  sync: FaceSync;
  shown: boolean;
};

export type StageFaceProps = {
  name: string; // who the face is (the letter circle without a face or portrait)
  portrait: string | null;
  speakerId: string;
  talk: TalkUi;
  avatarSrc: string | null; // pre-rendered clip for the line being read, when there is one
  avatarPlaying: boolean;
  getAudio?: () => FollowAudio | null; // read mode: the chunk's audio element, which the clip follows
  faceFor: (speakerId: string) => FaceT | undefined; // the face an answer shows
  readingFace: FaceT | null; // face of whoever is reading aloud right now
  preloadLoops?: string[]; // the next reader's loop videos, warmed up so a speaker change starts on time
  readSpeaking: boolean; // the audio is audibly speaking right now (not in a pause)
  levelRef: { current: number }; // live loudness of the audio, for the still-image pulse
  readLipSync?: ReadLipSyncUi;
  live?: LiveFaceUi; // #140: a running live face shows (with its own voice) while an answer is on stage
  className?: string;
};

/**
 * Muted on purpose: the page's own audio element stays the single source of sound, so it cannot double up.
 * The clip is driven by the chunk's audio (reader/clipFollow): it starts, pauses and ends with the voice and
 * corrects drift by nudging its playback rate rather than seeking.
 */
function AvatarVideo({ src, playing, getAudio }: { src: string; playing: boolean; getAudio?: () => FollowAudio | null }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    if (!getAudio) { // no audio to follow (silent preview): plain play/pause
      if (playing) void tryPlayQuietly(v); else v.pause();
      return;
    }
    const f = createClipFollower(v);
    const h = window.setInterval(() => { if (playing) f.tick(getAudio()); else if (!v.paused) v.pause(); }, 100);
    return () => window.clearInterval(h);
  }, [src, playing, getAudio]);
  return <video ref={ref} key={src} src={src} muted playsInline preload="auto" />;
}

/** The one persistent read-mode lip-sync <video>, owned by the sync (see talk/faceSync): never swapped, so nothing
 * flashes; it sits over the loop face (or the portrait) and fades in only while a clip plays in step. It stays
 * mounted whatever face is shown, so a chunk's clip is never lost to a face change between chunks. */
function ReadClipVideo({ sync, shown }: { sync: FaceSync; shown: boolean }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    sync.setVideo(ref.current);
    return () => sync.setVideo(null);
  }, [sync]);
  return <video ref={ref} className={`rf-clip${shown ? " on" : ""}`} muted playsInline />;
}

/**
 * The read-mode face (moved out of the Stage into the avatar player, #33): the reading face while the book is read,
 * the answering face (lip-synced through `talk.faceSync`, or the live face) while a question is answered, the chunk's
 * pre-rendered clip, the portrait, or the letter circle. Mount it once: its videos are what the syncs drive.
 */
export function StageFace({
  name, portrait, speakerId, talk, avatarSrc, avatarPlaying, getAudio, faceFor, readingFace, preloadLoops = [], readSpeaking,
  levelRef, readLipSync, live, className = "",
}: StageFaceProps) {
  // a question asked here (mic or Space) and its answer take the stage, as in Talkback: the paused reading's frozen
  // lip-sync frame, reading face or pre-rendered clip must not cover the answering face
  const answering = answerOnStage(talk.state, talk.speaking);
  const clipShown = !!readLipSync?.shown && !answering;
  const face = (answering ? null : readingFace) ?? faceFor(speakerId);
  const shows = stageFace({ answering, avatar: !!avatarSrc, reading: !!readingFace, face: !!face });
  const liveOn = answering && !!live?.face && !talk.narrating;
  const liveName = live?.info.avatars.find((a) => a.id === live.pick)?.name;
  const big = !!face || (shows === "avatar") || clipShown || liveOn; // a real face grows the circle; no face keeps the small monogram
  const thinking = ["transcribing", "retrieving", "answering"].includes(talk.state) && !talk.speaking;
  return (
    <div className={`medallion${big ? " big" : ""}${talk.state === "listening" ? " live" : ""}${thinking ? " thinking" : ""}${talk.speaking ? " speaking" : ""} ${className}`.trim()}
         aria-hidden="true">
      {liveOn && live ? <LiveFaceView media={live.media} name={liveName ?? name} />
        : shows === "avatar" && avatarSrc ? <AvatarVideo src={avatarSrc} playing={avatarPlaying} getAudio={getAudio} />
        : shows === "read" && face ? <ReadFace face={face} speaking={readSpeaking} levelRef={levelRef} clipShown={clipShown} />
        : shows === "talk" && face ? <FaceView face={face} speaking={talk.speaking} sync={talk.faceSync} clipShown={talk.clipShown} />
        : portrait ? <img src={portrait} alt="" /> : <span className="mono">{name.charAt(0)}</span>}
      {readLipSync && <ReadClipVideo sync={readLipSync.sync} shown={clipShown} />}
      {preloadLoops.map((src) => <video key={src} className="preload" src={src} muted playsInline preload="auto" />)}
    </div>
  );
}
