# sand

Voice/text AI assistant for extracting structured lab process data into NOMAD.

## How it works

```
create an experiment in the SAND UI (with the experiment-info form)
  -> one NOMAD upload with an InputCollection entry
     (+ a WrittenNote at experiment_info.archive.json holding the form JSON)

record audio / save a step note in the SAND UI (experiment selected,
optionally with a free-text label)
  -> the file/note goes into the experiment upload
       -> the voice-eln plugin creates an AudioInput entry
          and transcribes it (Whisper, inside NOMAD)
  -> the entry is referenced from the experiment's InputCollection
  -> SAND shows a clickable link to the created entry
```

An **experiment** is one NOMAD upload holding a
[`nomad-voice-eln`](https://github.com/FAIRmat-NFDI/nomad-voice-eln)
`InputCollection` entry plus all the `AudioInput` and `WrittenNote` entries
that belong to it. The SAND dashboard lists the user's **unpublished**
experiments (published uploads are read-only); recordings and typed step notes
always attach to the selected experiment. SAND does **not** transcribe audio
itself — raw audio, machine transcript, and human corrections live in the
voice-eln entries.

## Running the SAND app

SAND is not a standalone application — it is a **NOMAD dashboard plugin**. NOMAD mounts it onto its API server under `dashboards/sand/`. To run it you
start a NOMAD instance with this plugin installed and configured. The easiest way
to do this for development is via the
[`nomad-distro-dev`](https://github.com/FAIRmat-NFDI/nomad-distro-dev) repository.

### Prerequisites

Before you can run the SAND app you need a few things in place:

- A working [`nomad-distro-dev`](https://github.com/FAIRmat-NFDI/nomad-distro-dev)
  checkout with its [basic infra prerequisites](https://github.com/FAIRmat-NFDI/nomad-distro-dev#basic-infra)
- The new NOMAD GUI (the `nomad-gui` package) **installed and enabled in the
  same NOMAD**. Only the new GUI lists dashboards, and SAND has no login of
  its own: it uses the GUI's.
- The [`nomad-voice-eln`](https://github.com/FAIRmat-NFDI/nomad-voice-eln) plugin
  **installed and enabled in the same NOMAD** — it owns audio entries and
  transcription. Follow its README for setup, including `GROQ_API_KEY` in the
  **action worker's environment** (speech-to-text runs there, not in SAND).
- The [`nomad-llm-extraction`](https://github.com/FAIRmat-NFDI/nomad-llm-extraction)
  plugin **installed and enabled in the same NOMAD** — its action entry point
  registers the extraction workflows on NOMAD's action worker; SAND only starts
  them there.
- The [`nomad-hysprint`](https://github.com/nomad-hzb/nomad-hysprint) plugin
  **installed and enabled in the same NOMAD** — its batch parser turns the
  sheet SAND writes into NOMAD entries.
- An **LLM API key** for the extraction model (Gemini by default; any LiteLLM
  model works), set as the provider's environment variable (e.g.
  `GEMINI_API_KEY`) in the **action worker's environment**. It is not
  configured in `nomad.yaml`.

### 1. Add the plugin to a NOMAD dev distribution

The SAND app is loaded as part of a NOMAD distribution, so the plugin first has
to live inside your `nomad-distro-dev` checkout as a workspace package. From the
root of `nomad-distro-dev`, add it under `packages/` (as a git submodule if you
have a repo for it) and register it with `uv`:

```sh
# Add the plugin source under packages/ (submodule shown here; a plain copy works too)
git submodule add https://github.com/FAIRmat-NFDI/sand.git packages/sand

# Register it as an editable workspace dependency
uv add packages/sand
```

This adds `nomad-sand` to `[project.dependencies]` and `[tool.uv.sources]` in the
distribution's `pyproject.toml` (with `nomad-sand = { workspace = true }`).

### 2. Configure the plugin in `nomad.yaml`

The `uv run poe setup` step (below) creates a `nomad.yaml` in the root of your
`nomad-distro-dev` checkout if one does not exist yet. You must edit it to
**enable** the entry points of SAND and of the plugins it builds on, and to set
SAND's options:

```yaml
plugins:
  entry_points:
    include:
      - sand.apis:sand_api
      - sand.actions.extract:extract_action_entry_point
      # the new NOMAD GUI: lists the dashboard, SAND uses its login
      - nomad_gui.apis:gui_api
      # audio and note entries, transcription
      - nomad_voice_eln.schema_packages:schema_package_entry_point
      - nomad_voice_eln.parsers:parser_entry_point
      - nomad_voice_eln.actions.transcribe:transcribe_action_entry_point
      - nomad_voice_eln.actions.record_note:record_note_action_entry_point
      # extraction
      - nomad_llm_extraction.actions:llm_extractor_action_entry_point
      # the sheet SAND writes is parsed into hysprint entries
      - nomad_hysprint.schema_packages:hysprint_package
      - nomad_hysprint.parsers:hysprint_experiment_parser
    options:
      sand.apis:sand_api:
        llm_model_name: 'gemini/gemini-2.5-flash'  # LiteLLM notation
        # The LLM key is NOT configured here: set the provider env var
        # (e.g. GEMINI_API_KEY for gemini/* models) in the environment of
        # the cpu action worker, next to voice-eln's GROQ_API_KEY.
        # Base URL of the NOMAD API the app uploads to. For a local instance:
        nomad_base_url: 'http://localhost:8000/nomad-oasis/api/v1'
        # Live transcription while recording (optional). Empty key = feature
        # off; also read from the DEEPGRAM_API_KEY environment variable.
        deepgram_api_key: '<your-deepgram-api-key>'
        deepgram_model: 'nova-3'
        # default of the GUI toggle "save live transcript instead of
        # running whisper" (the user decides per recording; default off)
        # store_live_transcript: true
        # SAND opens in a browser tab (also the default). It does not work
        # embedded in the GUI: the iframe blocks downloads.
        launch_modes: ['tab']
```

`include` is a whitelist: once it is set, NOMAD loads only the entry points
listed, so other plugins you use have to be listed too.

There is no Groq/Whisper configuration in SAND anymore: speech-to-text is done
by the voice-eln transcription action, and its `GROQ_API_KEY` lives in the
action worker's environment.


> [!WARNING]
> Do not commit real API keys to `nomad.yaml`. Keep them out of version control

### 3. Start NOMAD

From the root of your `nomad-distro-dev` checkout:

```sh
uv run poe setup

docker compose up -d

uv sync

uv run poe start

# in a second terminal: the action worker (transcription, extraction),
# with GROQ_API_KEY and the LLM key (e.g. GEMINI_API_KEY) in its environment
uv run poe cpuworker

# optional, in a third terminal: the old GUI on port 3000. SAND's
# "View in NOMAD" links still point to it.
uv run poe gui start
```

The new GUI needs no command of its own: NOMAD serves it at
`http://localhost:8000/nomad-oasis/gui/v2/`.

### 4. Open the app

SAND is a NOMAD **dashboard** plugin: it is listed on the (new) NOMAD GUI's
*Dashboards* page and opens in a new tab. It does not run embedded in the GUI,
whose iframe blocks downloads (the sheet). It is mounted at (**note the
trailing slash**):

```
http://localhost:8000/nomad-oasis/dashboards/sand/
```

SAND has no login of its own: it uses the login of the NOMAD GUI, which renews
the token in the cookie while it is open. So SAND works only while the NOMAD
GUI is open in another tab of the same browser. SAND watches the token and warns when it is about to
expire or has expired; it then does not start a new recording.

### 5. Voice mode (optional)

With **Voice mode on**, a recording is started and stopped by voice, for
work where the hands are not free (glove box).

#### Turning it on

Do this while the hands are still free:

1. Select the experiment.
2. Click **Voice mode on**. SAND checks the setup and lists the result:

   | Check | Fails when |
   |---|---|
   | Experiment | none is selected |
   | NOMAD login | the login is not valid. Keep NOMAD open in another tab of this browser during the experiment |
   | Speech recognizer | its files are not installed (see below), or they do not load |
   | Microphone and voice | the microphone is not allowed, or SAND does not understand "hey sand" |
   | Screen stays on | the browser can not keep the screen on. A warning only |

3. When the check "Microphone and voice" runs, say **"hey sand"**. The
   words SAND hears are shown beside the check. There are 10 seconds.
4. The sign **Listening** with a blinking dot: voice mode is on. When a
   check failed, voice mode stays off; fix what the list names and click
   again.

Turning on and off makes no sound: it is done by click, at the screen.

#### The commands

| To | Say | Also understood |
|---|---|---|
| start a recording | **"hey sand, start record"** | "hey sand, start recording", "hey sand, record" |
| stop it and save the note | **"hey sand, stop"** | "hey sand, stop recording" |

"hi sand" works in place of "hey sand". Every command begins with it:
"stop" alone does nothing, so a note can hold the word.

#### What the beeps say

| Beep | Meaning |
|---|---|
| rising, two tones | the recording runs: speak |
| falling, two tones | the recording stopped |
| one high tone, after the falling one | the note is saved in NOMAD |
| two low tones | it did not work. The screen shows why |

No beep means that SAND did not take the command: say it again.

#### How to speak

- **Pause before and after a command**, about one second. SAND takes a
  command only at the end of what was said: "...five millilitres hey sand
  stop and then..." in one breath is a part of the note, not a command.
- **Speak the note after the rising beep.** What is said before it is not
  in the recording.
- **Wait 1.5 seconds before the next command.** After a command SAND
  takes no other for that long, so that one command heard twice is not
  done twice. "stop" and "start" right after each other: the second is
  lost.
- **Speak towards the microphone.** A headset or a clip-on microphone is
  understood better than a laptop across the room, for the commands and
  for the note.

One note, from start to end:

1. "hey sand, start record", pause
2. rising beep
3. the note
4. pause, "hey sand, stop", pause
5. falling beep, then one high tone: saved

#### On the screen

| Shown | Meaning |
|---|---|
| **Listening**, blinking dot | voice mode waits for a command |
| **Recording**, red, blinking dot | a recording runs |
| Heard: hey sand ... | what SAND made of the last words. It knows the words of the commands only, all others are shown as "..." |
| the live transcript | the note, as it is understood |

When a command is not taken, "Heard:" shows what arrived in its place.

#### Good to know

- **The Record and Stop buttons work as before**, also while voice mode
  is on.
- **"hey sand, stop" is heard in two ways**: by the recognizer in the
  browser, and in the live transcript, which hears it better after a long
  note or with noise. With live transcription, "hello sand, stop" and
  "hey sand, stop the recording" are understood as well.
- **The stop command is not a part of the note**: it is taken off the
  end of the saved live transcript. It stays in the audio, and in the
  transcript when Whisper makes it (live transcript not saved).
- **A failed upload** gives two low beeps. The recording is kept in the
  page and can be sent again from the screen: do not close the page.
- **Voice mode off** by click stops and saves a running recording. After
  a reload of the page voice mode is off.
- English only.

#### Privacy

While voice mode listens, the microphone is heard by the recognizer in
the browser only: nothing is sent and nothing is stored. Audio leaves the
computer only during a recording, as with the Record button.

#### Installing the speech recognizer

The speech recognizer is
[Vosk](https://alphacephei.com/vosk/) (Apache-2.0), in the build of
[vosk-browser](https://github.com/lichess-org/vosk-browser). Its files are
not part of this repository. Put them into `src/sand/apis/static/voice/vosk/`
(ignored by git) once:

```sh
mkdir -p src/sand/apis/static/voice/vosk && cd src/sand/apis/static/voice/vosk

# the recognizer
curl -L https://registry.npmjs.org/@lichess-org/vosk-browser/-/vosk-browser-0.0.3.tgz \
  | tar xz --strip-components=2 package/dist/vosk.wasm package/dist/vosk.wasm.js package/dist/vosk.worker.js

# the model (41 MB), repacked from zip to tar.gz
curl -LO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip -q vosk-model-small-en-us-0.15.zip
tar czf model.tar.gz vosk-model-small-en-us-0.15
rm -r vosk-model-small-en-us-0.15 vosk-model-small-en-us-0.15.zip
```

Without these files SAND works as before; turning voice mode on then
says that the speech recognizer is not installed.
