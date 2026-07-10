# Voice — push-to-talk for your agent terminal

Hold a key, talk, release. Your words are transcribed on your Mac and pasted into
whatever window is in front — your agent terminal, an editor, a chat box. Nothing
is sent to the cloud; the speech never leaves your computer.

Default hotkey: **hold Right-Option** to talk, release to paste.

## Install

Install the voice extras alongside the package:

```bash
pip install 'unofficial-davinci-mcp[voice]'
```

Or, if you're running from a checkout without the package extras:

```bash
pip install -r voice/requirements-voice.txt
```

The first time you record, the speech model (~140 MB for the default `base`
model) downloads once and is cached. After that it runs fully offline.

## macOS permissions

The bridge needs two permissions. Grant both, then quit and reopen your terminal
so it picks them up.

**1. Microphone** — to hear you.

- Open **System Settings → Privacy & Security → Microphone**.
- Turn on the app you launch the bridge from (your terminal — Terminal, iTerm,
  Ghostty, VS Code, etc.).

**2. Accessibility** — to paste the transcript with a keystroke.

- Open **System Settings → Privacy & Security → Accessibility**.
- Click **+**, add the same terminal app, and turn it on.

If you launch the bridge from a different app later, add that app in both places
too. Permissions are per-app.

## Run

```bash
python -m voice
```

You'll see a menu-bar icon and a "Hold Right-Option to talk" reminder. Hold the
hotkey, speak, release — the text appears in the focused window.

Run without the menu-bar icon (plain background listener):

```bash
python -m voice --no-menubar
```

Handy one-off overrides:

```bash
python -m voice --model small --hotkey f13 --auto-enter
```

## Configure

Settings live in `voice/config.json`. Edit and relaunch.

| Key | Default | What it does |
| --- | --- | --- |
| `hotkey` | `"right_option"` | Hold-to-talk key. Also: `left_option`, `right_cmd`, `left_cmd`, `right_ctrl`, `f13`–`f19`. |
| `auto_enter` | `false` | Press Return after pasting, so a command runs immediately. |
| `model` | `"base"` | Speech model: `tiny`, `base`, `small`, `medium`, `large-v3`. Bigger = more accurate, slower. |
| `input_device` | `null` | Microphone name or index. `null` = system default input. |
| `max_seconds` | `90` | Longest single recording. |
| `language` | `null` | Force a language (e.g. `"en"`), or `null` to auto-detect. |
| `sample_rate` | `16000` | Capture rate. 16 kHz is ideal for speech; leave it. |
| `restore_clipboard_delay` | `0.5` | Seconds before your previous clipboard is put back after paste. |
| `cue_tones` | `true` | Soft start/stop beeps so you know when it's listening. |
| `menubar` | `true` | Show the menu-bar icon. `--no-menubar` overrides. |

Your previous clipboard contents are restored automatically a moment after each
paste, so using the bridge doesn't clobber what you had copied.

### Speed and accuracy

The `base` model transcribes a short spoken command in roughly a second on Apple
Silicon. If short commands come out wrong, try `small`. Editing terms (timeline,
LUT, LUFS, J-cut, sting, Resolve, FCPXML…) are already biased into the model, so
jargon transcribes better than a generic dictation tool.

## Troubleshooting

- **Nothing pastes, but I heard the beeps.** Accessibility permission is missing
  or was granted to the wrong app. Re-check **Privacy & Security → Accessibility**
  for the exact app you launched from, then fully quit and reopen it.
- **Silent recording / empty transcript.** Microphone permission is off, or the
  wrong input device is selected. Check **Privacy & Security → Microphone**, and
  set `input_device` if you use an interface other than the default.
- **Long pause on the very first use.** That's the one-time model download. Later
  recordings are fast.
- **The hotkey does nothing.** Another app may own that key. Pick a function key
  (`f13`–`f19`) in `config.json`, which rarely conflicts.
- **Words are wrong.** Bump `model` to `small` or `medium`, and set `language` if
  you speak one language, so it stops guessing.
- **The wrong window received the text.** Whatever window is frontmost when you
  release the key gets the paste — click into your target window first.

## Packaging note

This bridge is distributed as the `[voice]` extra of `unofficial-davinci-mcp`:

```
faster-whisper >= 1.0
sounddevice   >= 0.4.6
pynput        >= 1.7.6
rumps         >= 0.4.0
```

If those aren't yet wired into the package's optional-dependencies, use
`voice/requirements-voice.txt` in the meantime — it lists exactly the same set.
