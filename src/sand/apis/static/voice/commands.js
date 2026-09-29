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
const GREETINGS = ["hey", "hi", "hay"];
const NAMES = ["sand", "sam", "send", "sent", "san", "stand", "sandy", "sands", "sandra", "zand"];
const STOPS = [["stop"], ["stop", "recording"], ["stop", "record"], ["stop", "the", "recording"]];

// How many words at the end of text are the spoken stop command, 0 if
// none.
function stopWordsAt(words) {
  const plain = words.map((word) => word.toLowerCase().replace(/[^a-z]/g, ""));
  for (const stop of STOPS) {
    const from = plain.length - stop.length - 2;
    if (from < 0) continue;
    const [greeting, name, ...rest] = plain.slice(from);
    const matches = GREETINGS.includes(greeting) && NAMES.includes(name)
      && rest.every((word, i) => word === stop[i]);
    if (matches) return stop.length + 2;
  }
  return 0;
}

function wordsOf(text) {
  return text.trim().split(/\s+/).filter((word) => /[a-z]/i.test(word));
}

// For a transcript (Deepgram, Whisper), not for the recognizer's text.
export function stopSaidIn(transcript) {
  return stopWordsAt(wordsOf(transcript)) > 0;
}

// The transcript without the stop command at its end.
export function withoutStop(transcript) {
  let drop = stopWordsAt(wordsOf(transcript));
  if (drop === 0) return transcript;
  const all = transcript.trim().split(/\s+/);
  while (drop > 0 && all.length > 0) {
    // a token without letters ("-") belongs to the command, but is no word
    if (/[a-z]/i.test(all.pop())) drop -= 1;
  }
  // a dash or the like that led to the command
  while (all.length > 0 && !/[a-z0-9]/i.test(all.at(-1))) all.pop();
  return all.join(" ");
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
