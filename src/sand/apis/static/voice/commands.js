// The spoken commands of voice mode. No imports and no page: tested in
// node (tests/js).

// Every phrase starts with "hey sand" or "hi sand": that, and "[unk]",
// is all that keeps ordinary talk from being taken for a command.
export const COMMANDS = {
  start: [
    "hey sand start record",
    "hey sand start recording",
    "hey sand record",
    "hi sand start record",
  ],
  stop: ["hey sand stop", "hey sand stop recording", "hi sand stop"],
  // said once when voice mode turns on, to check microphone and voice
  test: ["hey sand", "hi sand"],
};

// "[unk]" stands for any other word: without it the recognizer would
// force all talk onto the phrases.
export const GRAMMAR = [...Object.values(COMMANDS).flat(), "[unk]"];

// The longest phrase first: "hey sand stop recording" also ends with
// "stop recording", and starts with "hey sand".
const PHRASES = Object.entries(COMMANDS)
  .flatMap(([command, phrases]) => phrases.map((phrase) => [phrase, command]))
  .sort((a, b) => b[0].length - a[0].length);

// "start", "stop", "test" or null. text is what was said between two
// pauses, e.g. "[unk] [unk] hey sand stop": a command counts only at its
// end, so one in the middle of a sentence does nothing.
export function commandIn(text) {
  const said = " " + text.trim().replace(/\s+/g, " ");
  for (const [phrase, command] of PHRASES) {
    if (said.endsWith(" " + phrase)) return command;
  }
  return null;
}
