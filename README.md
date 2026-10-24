# Hyper-Wispr

Voice dictation for Windows that runs **entirely on your own computer**. Hold a key, speak, let go,
and the text appears wherever your cursor is: Word, Outlook, the browser, your EHR, anything.
No account, no subscription, no audio sent to the internet.

It's modelled on how Wispr Flow works (see [docs/wispr-flow-analysis.md](docs/wispr-flow-analysis.md))
but is written from scratch and uses open-source models:

- **Speech to text:** OpenAI's Whisper, running locally through [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
  Uses your NVIDIA graphics card if it can run the model (GTX 10-series, MX150 or newer), otherwise the CPU.
- **Cleanup:** removes "um/uh", handles "scratch that", spoken punctuation ("comma", "new line"),
  your custom word list and replacements. Optionally, a small local AI model (through
  [Ollama](https://ollama.com)) polishes grammar and formatting and adapts the style to the app
  (email vs. chat vs. document), with medical-writing rules for doses and drug names.

## Install (Windows)

1. Go to [Releases](https://github.com/omar-184/Hyper-Wispr/releases/latest) and download
   `Hyper-Wispr-Setup-<version>.exe`, or the `-NVIDIA` version if your PC has an NVIDIA GeForce
   GTX 10-series, MX150 or newer card (bigger download, much faster). Older cards such as the MX130
   can't run the speech model, so take the regular version.
2. Run it. No administrator password is needed. If Windows says "Windows protected your PC", click
   **More info → Run anyway** (the installer isn't code-signed).
3. Optional but recommended: install [Ollama](https://ollama.com/download) for the AI cleanup step.
   Hyper-Wispr downloads its AI model automatically.
4. The Hyper-Wispr window opens on its dashboard, and a round microphone icon appears in the system tray (bottom right; it may be under the ^ arrow).
   Grey = loading, blue = ready, red = listening, amber = working. The first start downloads the
   speech model once (about 0.5 to 1.6 GB), so it stays grey for a few minutes.

### Install from source instead

Download the code (green **Code** button → **Download ZIP**), unzip it, double-click
`scripts\install.bat`, then `scripts\run.bat`. `scripts\start-with-windows.bat` makes it start at login.

## Use

| Do this | What happens |
| --- | --- |
| Hold **Ctrl + Win**, speak, release | Text is pasted at your cursor |
| Tap **Ctrl + Win** quickly | Hands-free: keeps listening. Tap **Ctrl + Win** again to finish |
| Press **Esc** while listening | Cancel, nothing is pasted |
| Say "new line", "new paragraph", "comma", "period", "question mark" | Inserts that punctuation |
| Say "scratch that" | Deletes the sentence you just said |
| Select text, hold **Ctrl + Win + Alt**, say e.g. "make this shorter" | Command mode: the selected text is rewritten as you asked (needs AI cleanup) |
| Press **Ctrl + Alt + Z** | Pastes your last dictation again |
| Tray icon → **Whisper mode** | Picks up speech spoken quietly, e.g. next to a patient |
| Tray icon → **Copy last dictation** | Puts your previous dictation on the clipboard; paste it with Ctrl+V |
| Start Hyper-Wispr (Start menu or desktop) | Opens the Hyper-Wispr window; closing it keeps dictation running in the tray |

## Customize

Click the tray icon (or right-click → **Open Hyper-Wispr**) to open the window:

- **Dashboard**: whether it's ready, words dictated today and this week, time saved, speaking speed,
  the last 14 days, recent dictations and quick switches.

- **History**: search your dictations, copy one, or redo it with a bigger speech model. Encrypted with your
  Windows account and deleted automatically after 30 days (you can change that).
- **Notes**: a notepad inside the app; click into a note and dictate.
- **General**: change the shortcut by pressing the keys you want, command mode, pick and test your microphone,
  whisper mode, keep the microphone ready, mute music while dictating, paste or type, fit dictations into the
  text around the cursor, learn from your corrections.
- **Speech**: speech model, English, English + Arabic or Arabic (or other languages), GPU or processor.
- **AI cleanup**: turn the Ollama step on or off, medical mode, which model, a writing style (formal, casual, very
  casual) for each kind of app, your own extra instructions.
- **Vocabulary**: words you corrected (offered for the dictionary), dictionary and starred words, replacements,
  snippets, filler words.
- **About & updates**: checks GitHub for a newer version (once a day, can be turned off), downloads the
  right installer for your PC and installs it in place. Settings, history and the speech model are kept.

When started at login it waits in the tray instead of opening the window.

Changes save immediately and the running app applies them within a second (a new speech model loads in the
background while the old one keeps working).

Advanced: everything is stored in `%APPDATA%\HyperWispr\settings.json`, which you can also edit by hand
(About & updates → Open file). For example:

```json
{
  "push_to_talk_keys": ["ctrl_l", "cmd"],
  "speech": { "model": "auto", "languages": ["en", "ar"] },
  "dictionary": ["apixaban", "empagliflozin", "laparoscopic cholecystectomy"],
  "replacements": { "hyper whisper": "Hyper-Wispr" },
  "snippets": { "my signature": "Dr. Jane Doe\nDepartment of Medicine" },
  "ai_cleanup": { "enabled": true, "model": "qwen2.5:3b", "medical_mode": true }
}
```

- **dictionary**: names and terms you want spelled your way. They are given to Whisper as hints and to the AI step.
  **dictionary_starred** words always go first (Whisper only takes a limited number of hints).
- **replacements**: always change the left side into the right side.
- **snippets**: say the trigger phrase on its own and the full text is pasted.
- **speech.model**: `auto` picks `large-v3-turbo` with an NVIDIA GPU (GTX 10-series, MX150 or newer) and `small` on CPU. Bigger = more accurate but slower:
  `tiny`, `base`, `small`, `medium`, `large-v3-turbo`, `large-v3`. English-only versions (`small.en`, `medium.en`) are a bit more accurate for English.
- **speech.languages**: the languages you dictate in (default `["en"]`). With one language it is always used;
  with several, Hyper-Wispr picks the most likely of them each time. Guessing from all languages is unreliable
  for short dictations, so keep this list short. `speech.language` forces a single language.
- **ai_cleanup.model**: any Ollama model. `qwen2.5:3b` is fast; `qwen2.5:7b` or `llama3.1:8b` are better if you have a good GPU.
- **push_to_talk_keys**: key names like `ctrl_l`, `ctrl_r`, `cmd` (the Windows key), `alt_l`, `shift_r`, `f8`.

History is kept locally in `%APPDATA%\HyperWispr\history.sqlite3`, and the log in `hyper-wispr.log` in the same folder.

## Troubleshooting

- **Nothing happens when I hold the keys:** run `scripts\run-with-log.bat` to see messages. The first start takes
  a while to load the model; the icon turns blue when ready.
- **Text doesn't appear in one specific program:** some programs running as Administrator ignore keystrokes from
  normal programs. Try `"paste": {"method": "type"}` in settings.
- **Test without the microphone:** `.venv\Scripts\python.exe -m hyper_wispr --transcribe some-recording.wav`

## For developers

```bash
uv venv && uv pip install -e ".[dev]"
pytest
```

Releases: pushing a tag like `v0.2.0` (or running the workflow from the Actions tab with **publish** ticked) runs `.github/workflows/release.yml`, which tests the code, builds the
app with PyInstaller (CPU and NVIDIA variants), smoke-tests it, wraps it with Inno Setup and publishes the
installers as a GitHub Release.

Code layout: `hyper_wispr/hotkeys.py` (push-to-talk state machine), `audio.py` (microphone),
`speech.py` (Whisper), `cleanup.py` (rules), `ai_cleanup.py` (Ollama), `context.py` (which app is in front),
`paste.py` and `clipboard.py` (paste, keeping your clipboard and keeping dictations out of clipboard history),
`uia.py` (reading the text box you dictate into), `learn.py` (learning corrected words), `media.py` (muting music),
`history.py` and `secure.py` (encrypted history and notes), `app.py` (tray app tying it together), `updates.py` (update check and install), `settings_ui/` (the window:
a pywebview page in `web/`, run as a separate process with `--settings`).
