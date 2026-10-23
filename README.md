# Hyper-Wispr

Voice dictation for Windows that runs **entirely on your own computer**. Hold a key, speak, let go,
and the text appears wherever your cursor is: Word, Outlook, the browser, your EHR, anything.
No account, no subscription, no audio sent to the internet.

It's modelled on how Wispr Flow works (see [docs/wispr-flow-analysis.md](docs/wispr-flow-analysis.md))
but is written from scratch and uses open-source models:

- **Speech to text:** OpenAI's Whisper, running locally through [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
  Uses your NVIDIA graphics card if you have one, otherwise the CPU.
- **Cleanup:** removes "um/uh", handles "scratch that", spoken punctuation ("comma", "new line"),
  your custom word list and replacements. Optionally, a small local AI model (through
  [Ollama](https://ollama.com)) polishes grammar and formatting and adapts the style to the app
  (email vs. chat vs. document), with medical-writing rules for doses and drug names.

## Install (Windows)

1. Go to [Releases](https://github.com/omar-184/Hyper-Wispr/releases/latest) and download
   `Hyper-Wispr-Setup-<version>.exe`, or the `-NVIDIA` version if your PC has an NVIDIA graphics card
   (bigger download, much faster).
2. Run it. No administrator password is needed. If Windows says "Windows protected your PC", click
   **More info → Run anyway** (the installer isn't code-signed).
3. Optional but recommended: install [Ollama](https://ollama.com/download) for the AI cleanup step.
   Hyper-Wispr downloads its AI model automatically.
4. A round microphone icon appears in the system tray (bottom right; it may be under the ^ arrow).
   Grey = loading, blue = ready, red = listening, amber = working. The first start downloads the
   speech model once (about 0.5 to 1.6 GB), so it stays grey for a few minutes.

### Install from source instead

Download the code (green **Code** button → **Download ZIP**), unzip it, double-click
`scripts\install.bat`, then `scripts\run.bat`. `scripts\start-with-windows.bat` makes it start at login.

## Use

| Do this | What happens |
| --- | --- |
| Hold **Ctrl + Win**, speak, release | Text is pasted at your cursor |
| Hold **Ctrl + Win**, tap **Space**, let go | Hands-free: keeps listening. Press **Ctrl + Win** again to finish |
| Press **Esc** while listening | Cancel, nothing is pasted |
| Say "new line", "new paragraph", "comma", "period", "question mark" | Inserts that punctuation |
| Say "scratch that" | Deletes the sentence you just said |
| Tray icon → **Paste last dictation** | Pastes your previous dictation again |

## Customize

Tray icon → **Open settings**. This opens `%APPDATA%\HyperWispr\settings.json` in Notepad.
After saving, choose **Reload settings** in the tray menu (changes to the speech model need a restart).

Useful settings:

```json
{
  "push_to_talk_keys": ["ctrl_l", "cmd"],
  "speech": { "model": "auto", "language": "" },
  "dictionary": ["apixaban", "empagliflozin", "laparoscopic cholecystectomy"],
  "replacements": { "hyper whisper": "Hyper-Wispr" },
  "snippets": { "my signature": "Dr. Jane Doe\nDepartment of Medicine" },
  "ai_cleanup": { "enabled": true, "model": "qwen2.5:3b", "medical_mode": true }
}
```

- **dictionary**: names and terms you want spelled your way. They are given to Whisper as hints and to the AI step.
- **replacements**: always change the left side into the right side.
- **snippets**: say the trigger phrase on its own and the full text is pasted.
- **speech.model**: `auto` picks `large-v3-turbo` with an NVIDIA GPU and `small` on CPU. Bigger = more accurate but slower:
  `tiny`, `base`, `small`, `medium`, `large-v3-turbo`, `large-v3`. English-only versions (`small.en`, `medium.en`) are a bit more accurate for English.
- **speech.language**: leave empty to auto-detect, or set e.g. `"en"` or `"ar"` to force one.
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
`paste.py` (clipboard paste), `app.py` (tray app tying it together).
