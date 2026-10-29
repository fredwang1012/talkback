import { MAX_GROUP } from "../talk/TalkController";
import { togglePick } from "./groupScene";

type Props = {
  name: string; // the one talked to: always answers first
  choices: { id: string; name: string }[]; // who may join (groupChoices)
  picks: string[]; // joined, in the order picked (= the order they answer in)
  onPicks: (p: string[]) => void;
  disabled?: boolean;
};

/** #79: "Also ask" toggles under the Talkback controls. Each joined character is one more answer, so the count shows. */
export default function GroupPicker({ name, choices, picks, onPicks, disabled }: Props) {
  if (!choices.length) return null;
  const full = picks.length >= MAX_GROUP - 1;
  return (
    <fieldset className="group-pick" disabled={disabled}>
      <legend>Also ask (group, up to {MAX_GROUP})</legend>
      <div className="group-pick-list">
        {choices.map((c) => {
          const at = picks.indexOf(c.id);
          return (
            <button key={c.id} type="button" className="spoiler-toggle" aria-pressed={at >= 0} disabled={at < 0 && full}
                    title={at >= 0 ? `Answers ${at === 0 ? "second" : "third"}; press to leave out` : full ? `At most ${MAX_GROUP} answer one question` : `Add ${c.name}: answers after ${name}`}
                    onClick={() => onPicks(togglePick(picks, c.id))}>
              {at >= 0 && <span className="group-order">{at + 2}</span>}{c.name}
            </button>
          );
        })}
      </div>
      {picks.length > 0 && (
        <p className="group-note" role="status">
          {picks.length + 1} answers per question, one after the other ({picks.length + 1} answer requests). Stop ends them all.
        </p>
      )}
    </fieldset>
  );
}
