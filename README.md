# claude-read-aloud: text-to-speech for Claude Code

Read Claude's replies aloud in Claude Code. Every paragraph of a reply gets a 🔊 button. Press it and Claude reads from that paragraph to the end of the reply. A speed button sits beside the first paragraph.

It runs on your own computer with [Piper](https://github.com/OHF-Voice/piper1-gpl) voices, so a press starts speaking in about half a second, with no API key and no account. Microsoft's online voices (through [edge-tts](https://github.com/rany2/edge-tts)) are there as a fallback, or if you prefer them.

## What it does

- **🔊 on every paragraph.** Press one and it reads from there to the end of the reply. The button turns into ⏹ while that paragraph is being read, so you can follow along. Press ⏹ to stop.
- **Speed button.** On the right of each reply's first paragraph. It steps through 0.75×, 1×, 1.25×, 1.5×, 1.75× and 2×. The pitch stays the same.
- **Audio ready before you press.** When Claude finishes a reply, its audio is made in the background. Audio for the last two replies is kept. Older audio is deleted.
- **No gaps or stutter.** All paragraphs go through one audio player. Speech generation runs at low priority, so it never takes processor time from playback.
- **Skips what should not be read.** Code blocks, tables, link addresses and emoji are left out.
- **Picks a voice by language.** English, Swedish and Ukrainian voices are set up. Any other [Piper voice](https://huggingface.co/rhasspy/piper-voices) or edge-tts voice can be chosen. Each voice downloads the first time it is used.
- **Speaks only when you press.** Nothing is read aloud unless you ask, unless you turn on auto-read.

## Install

You need Python 3.9 or newer and FFmpeg (for `ffplay`).

1. Install the speech packages:

   ```bash
   pip install piper-tts edge-tts
   ```

2. Install FFmpeg and make sure `ffplay` is on your PATH:
   - Windows: `winget install Gyan.FFmpeg`
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`

3. Install the plugin. At the prompt of a Claude Code terminal session:

   ```
   /plugin install claude-read-aloud --marketplace kostiuk-dmytro/claude-read-aloud
   ```

   Answer `y` to add the marketplace, then pick a scope (user scope gives you the buttons in every project).

   From a shell:

   ```bash
   claude plugin install claude-read-aloud --marketplace kostiuk-dmytro/claude-read-aloud
   ```

   In the Claude desktop app: click **+** next to the prompt box, then **Plugins**, **Add marketplace** with `kostiuk-dmytro/claude-read-aloud`, then **Add plugin**.

4. Start a new session and run `/read-aloud doctor` to check that everything was found.

The first press downloads the voice (about 60 MB) and takes around 10 seconds. After that it is fast.

## Commands

| Command | What it does |
|---|---|
| `/read-aloud` | Help |
| `/read-aloud stop` | Stop reading |
| `/read-aloud speed 1.25` | Playback speed, 0.5 to 2 |
| `/read-aloud voice en_US-ryan-medium` | Use a fixed voice. Piper names look like `en_US-ryan-medium`. edge-tts names end in `Neural`, for example `en-US-AvaNeural`. |
| `/read-aloud voice auto` | Pick the voice from the reply's language (the default) |
| `/read-aloud auto on` | Read every reply aloud as soon as it is finished. `off` turns it off again (the default). |
| `/read-aloud doctor` | Show which Python, voices and tools were found |

## Settings

| Environment variable | Use |
|---|---|
| `CLAUDE_READ_ALOUD_PYTHON` | Full path to the Python that has `piper-tts` installed, if `python3`, `python` or `py` is not the right one |
| `CLAUDE_READ_ALOUD_PORT` | Port of the local voice server (default `59125`, listens on 127.0.0.1 only) |
| `CLAUDE_READ_ALOUD_MUTE` | Set to anything to play at volume 0, which is useful when testing |

State, the audio cache and downloaded voices are kept in `~/.claude/read-aloud/`.

## How it works

- `hooks/register.tsx` is a Claude Code hooks module. It draws the buttons beside each paragraph and registers `/read-aloud`. When a reply is finished, it asks the script to make the audio.
- `scripts/read_aloud.py` splits the reply into paragraphs and removes markdown. It makes the audio, keeps the cache, and plays everything through one `ffplay` process.
- `scripts/piper_server.py` keeps Piper voices loaded in a small local server (127.0.0.1 only), because loading a voice takes about 6 seconds and using one takes about 0.2 seconds. It stops by itself after 4 hours without use.

## Troubleshooting

- **No buttons:** start a new session after installing, and check that the plugin is enabled.
- **"Read aloud is not ready":** no Python with the script was found. Run `pip install piper-tts edge-tts`, or set `CLAUDE_READ_ALOUD_PYTHON`.
- **No sound:** run `/read-aloud doctor` and check that `ffplay` is found. Errors are logged to `~/.claude/read-aloud/log.txt`.
- **First press is slow:** the voice is being downloaded. It only happens once per voice.

Tested on Windows 11 with Claude Code 2.1. The scripts also handle macOS and Linux, but have not been tested there yet. Reports and pull requests are welcome.

## På svenska

claude-read-aloud läser upp Claudes svar i Claude Code. Varje stycke får en 🔊-knapp. Tryck på den så läses svaret upp från det stycket till slutet. Hastighetsknappen sitter till höger om första stycket. Allt körs lokalt med Piper, så uppläsningen startar på ungefär en halv sekund. Det finns en svensk röst (Lisa), och den väljs automatiskt när svaret är på svenska. Installera som ovan och kör sedan `/read-aloud doctor`.

## License

The code in this repository is MIT licensed. It does not include Piper or edge-tts: you install them yourself with pip, under their own licenses (`piper-tts` is GPL-3.0, `edge-tts` is LGPL-3.0). Piper voices have their own licenses too, listed on each voice's [model card](https://huggingface.co/rhasspy/piper-voices).
