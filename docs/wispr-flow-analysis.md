# How Wispr Flow works (reference notes)

These notes describe the **behaviour and architecture** of Wispr Flow 1.6.7 for Windows, observed by
inspecting the installed app with REA and standard binary tools. They are the reference for building
Hyper-Wispr. No Wispr Flow code, prompts, sounds or other assets are copied into this repository;
Hyper-Wispr is an independent implementation.

## Pieces of the app

| Part | What it is | What it does |
| --- | --- | --- |
| `Wispr Flow.exe` | Electron app (web UI packaged as a desktop program) | Settings hub, floating "flow bar" overlay, history, notes, meetings, account |
| `resources/app.asar` | Bundled JavaScript (webpack) | All app logic: recording, talking to the servers, formatting, storage |
| `Release/Wispr Flow Helper.exe` | .NET program, started by the main app | Keyboard hook, reading the focused text box, pasting text |
| `opusEncoder.worker.js` | Audio encoder | Compresses microphone audio (Opus) before upload |
| `migrations/*.js` | Database schema | Local SQLite history, dictionary, snippets, notes |

## The dictation flow

1. **Hotkey.** The Helper installs a low-level keyboard hook (`SetWindowsHookEx`). Holding the shortcut
   (default Ctrl+Win on Windows) is push-to-talk; there is also a hands-free (locked) mode.
2. **Context capture.** At the start of a dictation the Helper uses Windows UI Automation to read the
   focused text box: the text before and after the cursor, any selected text, and whether the field is
   editable. The main app also notes which application is in front and classifies it
   (email, messaging, code, document, AI chat, other).
3. **Audio.** The microphone is recorded, Opus-encoded and **streamed to Wispr's cloud servers**
   (`api.wisprflow.ai`) over a WebSocket. Speech recognition does not run on the PC.
4. **Formatting ("polish").** The server-side step sends the transcript plus context (app type,
   surrounding text, the user's dictionary words, the user's name, language) to a language model,
   with a separate variant for email. Non-English text goes to its own
   formatting endpoint. (The client references fine-tuned Llama 3.1 8B model names for its formatting
   calls; exactly which models the main server path uses is not visible from the client.)
5. **Paste.** The Helper saves the clipboard, writes the result (plain text and HTML), sends Ctrl+V
   with `SendInput`, then restores the original clipboard after a short timer. If no editable text box
   is focused, the text is shown in a notification instead so it isn't lost.
6. **Learning.** Afterwards the Helper watches the text box to see if the user edits what was pasted,
   which feeds the personal dictionary and style.

Other features found: dictionary (with "starred" words), text replacements, snippets (voice shortcuts),
command mode (speak an instruction to rewrite the selected text), per-app styles, history,
notes, meeting recording, and usage statistics.

## What Hyper-Wispr does differently

| Wispr Flow | Hyper-Wispr |
| --- | --- |
| Cloud speech recognition | Whisper running locally (faster-whisper; GPU if available) |
| Cloud fine-tuned Llama 8B for formatting | Rules + optional local LLM through Ollama |
| Account, sync, analytics | None; everything stays in `%APPDATA%\HyperWispr` |
| Settings UI | Tray menu + a plain JSON settings file (UI planned) |

## Not yet in Hyper-Wispr (candidates for next steps)

- Reading the text around the cursor (UI Automation) to give the AI cleanup more context.
- Command mode: select text, speak an instruction, get it rewritten.
- A floating indicator while recording, and a settings window.
- Learning from the user's edits.
