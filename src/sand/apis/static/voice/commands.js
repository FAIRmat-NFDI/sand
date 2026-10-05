// The spoken commands of voice mode. No imports and no page: tested in
// node (tests/js).

// Every phrase starts with "hey sand" or "hi sand": that, and "[unk]",
// is all that keeps ordinary talk from being taken for a command.
const CALLS = ["hey sand", "hi sand"];
const SAID = {
  start: ["start record", "start recording", "record"],
  // "stop" alone is no command: "hey sam, stop" is said to people too
  stop: ["stop recording", "stop the recording", "stop record"],
  // the call alone: said once when voice mode turns on, to check
  // microphone and voice
  test: [""],
};

// A command may be said with "please" after the call or at its end
// ("hey sand please stop recording", "hey sand stop recording please").
// One before the call needs no phrase: what comes before a command is
// ignored anyway (commandIn).
function politely(ending) {
  return ending ? [ending, "please " + ending, ending + " please"] : [ending];
}

// Every call with every ending: the recognizer hears "hi" as easily as
// "hey", whatever was said.
export const COMMANDS = Object.fromEntries(Object.entries(SAID).map(([command, endings]) => [
  command,
  CALLS.flatMap((call) => endings.flatMap(politely).map((ending) => (call + " " + ending).trim())),
]));

// "[unk]" stands for any other word: without it the recognizer would
// force all talk onto the words of the phrases.
//
// Vosk takes from the phrases only their words: it also reports them in
// another order ("stop sand hey") or alone ("[unk] stop [unk]"). That the
// words form a command is checked here, in commandIn.
export const GRAMMAR = [...Object.values(COMMANDS).flat(), "[unk]"];

// The longest phrase first: "hey sand stop recording" also ends with
// "stop recording", and starts with "hey sand".
const PHRASES = Object.entries(COMMANDS)
  .flatMap(([command, phrases]) => phrases.map((phrase) => [phrase, command]))
  .sort((a, b) => b[0].length - a[0].length);

// The end of a note that was stopped by voice, as a transcription writes
// it: with capitals and punctuation, and "sand" often as a name or a word
// that sounds like it.
const GREETINGS = ["hey", "hi", "hay", "hei", "hello"];
const NAMES = ["sand", "sam", "send", "sent", "san", "stand", "sandy", "sands", "sandra", "zand"];
const STOPS = [
  ["stop", "recording"],
  ["stop", "the", "recording"],
  ["stop", "record"],
  ["stop", "the", "record"],
];

// How many words at the end of text are the spoken stop command, 0 if
// none. A "please" before the greeting, after the name or at the end
// belongs to the command.
function stopWordsAt(words) {
  const plain = words.map((word) => word.toLowerCase());
  const tail = plain.at(-1) === "please" ? 1 : 0;
  const end = plain.length - tail;
  for (const stop of STOPS) {
    for (const command of [stop, ["please", ...stop]]) {
      const from = end - command.length - 2;
      if (from < 0) continue;
      const [greeting, name, ...rest] = plain.slice(from, end);
      const matches = GREETINGS.includes(greeting) && NAMES.includes(name)
        && rest.every((word, i) => word === command[i]);
      if (!matches) continue;
      const lead = plain[from - 1] === "please" ? 1 : 0;
      return lead + command.length + 2 + tail;
    }
  }
  return 0;
}

// The words of a text as runs of letters, with where they start: a
// transcription may join "hey sand" into "Hei-san".
function wordsOf(text) {
  return [...text.matchAll(/[a-z]+/gi)];
}

// For a transcript (Deepgram, Whisper), not for the recognizer's text.
export function stopSaidIn(transcript) {
  return stopWordsAt(wordsOf(transcript).map((match) => match[0])) > 0;
}

// The transcript without the stop command at its end. Cut where the
// command's first word starts, so the rest is kept as written.
export function withoutStop(transcript) {
  const words = wordsOf(transcript);
  const drop = stopWordsAt(words.map((match) => match[0]));
  if (drop === 0) return transcript;
  return transcript
    .slice(0, words[words.length - drop].index)
    .trimEnd()
    // a dash or the like that led to the command
    .replace(/(^|\s+)[^\sa-z0-9]+$/i, "");
}

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
