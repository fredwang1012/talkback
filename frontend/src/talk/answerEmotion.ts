/**
 * The emotion of each answer sentence (#14), kept by sentence text: the talk loop only passes the text to the speaker,
 * and the speaker then asks for the emotion to send with /api/speak. Cleared when a new answer starts.
 */
export function createEmotionMemo() {
  const byText = new Map<string, string>();
  return {
    note(text: string, emotion: string | undefined) {
      if (emotion) byText.set(text, emotion); else byText.delete(text);
    },
    get: (text: string): string | undefined => byText.get(text),
    clear: () => byText.clear(),
  };
}
