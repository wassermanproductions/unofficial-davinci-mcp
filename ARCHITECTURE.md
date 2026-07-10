# Wasserman's Unofficial DaVinci MCP — build architecture (internal brief)

> Internal build brief. Delete this file before publishing; the public docs are
> README.md + skills/. Public docs are USER-FIRST: what it does, install, use,
> troubleshoot. No build-process narrative.

## Product
One MCP stdio server that lets ANY MCP agent (Claude Code, Codex, Cursor,
Hermes) run DaVinci Resolve for you. Two tiers, auto-detected at startup and
reported by `resolve_capabilities`:

- **live** (DaVinci Resolve Studio): drives the running app through the
  scripting API — import media, build/edit timelines, apply grades/LUTs per
  clip, markers, renders. External scripting is Studio-only.
- **interchange** (free DaVinci Resolve, or Resolve not running): produces
  files the user imports in one action — FCPXML/EDL timelines, .cube LUTs,
  marker CSVs, premixed audio. Every mutating live tool has an interchange
  fallback wherever one is possible; tools state clearly when Studio is
  required.

## Layout
- `davinci_mcp/` — package. `server.py` (zero-dep JSON-RPC 2.0 stdio MCP,
  protocolVersion 2024-11-05 — mirror the proven pattern in
  /Users/eklpse1/Desktop/blockout/mcp/blockout-mcp.mjs, in Python),
  `resolve_api.py` (fusionscript discovery/connection; env overrides;
  graceful "Studio required / app not running" errors), `mode.py` (tier
  detection), `tools_live.py`, `tools_interchange.py`, `registry.py` (one
  table: name, schema, handler, tier, description).
- `engines/` — deterministic creative engines, no LLM calls, stdlib+numpy+
  ffmpeg only (librosa optional extra for beat tracking, code must degrade
  gracefully without it):
  - `color_match.py` — reference image/still → per-shot color transform
    (Reinhard + Lab histogram options) → bake `.cube` LUT (33pt); live tier
    can also apply via API; always writes the LUT file.
  - `loudness.py` — ffmpeg EBU R128 measure; dialogue normalization plan;
    music bed level; ducking windows from silencedetect on the dialogue track.
  - `dead_air.py` — silence/dead-air detection on talking clips → cut list →
    tightened timeline plan (live: apply; interchange: FCPXML).
  - `music_cut.py` — cut a song to a target duration ending on a musical
    boundary with a smooth "sting out": onset/beat grid (librosa if present,
    else energy-envelope fallback), choose exit point near target, exponential
    tail fade, optional pre-sting silence gap. Outputs cut WAV + edit metadata.
  - `beat_grid.py` — beats/onsets for cut-to-music; JSON grid consumed by
    assemble tools.
  - `assemble.py` — edit-plan schema (clips, ranges, order, audio, markers,
    beat-snap option) → live timeline OR FCPXML 1.9 + EDL. FCPXML must import
    clean into free Resolve 19/20.
- `skills/` — editorial knowledge the agent reads (also served by the
  `get_editing_knowledge` tool so every client benefits): `color-looks.md`,
  `beat-cutting.md`, `dialogue-editing.md`, `music-editing.md`, `mixing.md`.
  Written like a seasoned editor teaching an assistant: concrete numbers
  (LUFS targets, fade lengths, frame handles), decision rules, vocabulary →
  parameter mappings the tools accept.
- `voice/` — push-to-talk bridge (macOS): menu-bar app, hold-to-talk hotkey,
  local faster-whisper transcription, pastes the transcript into the focused
  agent terminal (clipboard + Cmd-V keystroke via osascript) with optional
  auto-Enter; optional spoken replies OFF by default. Own README with the
  Accessibility/Microphone permission walkthrough. Zero cloud calls.
- `tests/` — pytest. Mocked Resolve API for live tools (record calls);
  golden-file tests for FCPXML/EDL/LUT/cut-plan outputs; real-ffmpeg audio
  engine tests on generated tones/speech-shaped noise; MCP protocol test
  (spawn server, initialize/tools/list/tools/call over stdio).

## Tool surface (agent-facing names)
`resolve_capabilities`, `get_editing_knowledge(topic)`, `probe_media(paths)`,
`scan_media_folder(path)`, `assemble_edit(plan)`, `beat_grid(audio)`,
`cut_music(song, target_seconds, ...)`, `tighten_dialogue(clip|timeline, ...)`,
`mix_plan(timeline|files, dialogue_lufs=-16, ...)`, `color_match(reference,
shots|timeline, ...)`, `apply_lut(lut, clips)` (live) / LUT files (both),
`import_media`, `create_timeline`, `append_to_timeline`, `add_markers`,
`render` (live tier), `generate_fcpxml`, `generate_edl`, `generate_marker_csv`
(both tiers). Mutating tools: dry_run=True default + confirm gate — same
contract as the reviewed Hermes plugin.

## Hard rules
- Package name/dir: repo `unofficial-davinci-mcp`, product name "Wasserman's
  Unofficial DaVinci MCP". README carries a clear disclaimer: unofficial,
  not affiliated with or endorsed by Blackmagic Design; DaVinci Resolve is
  their trademark.
- Apache-2.0 + NOTICE (already present — credit Sam Wasserman, keep the
  wassermanproductions.com · wasserman.ai lines). CITATION.cff like
  github.com/wassermanproductions/blockout. README footer credit block
  matches the other repos ("Created by Sam Wasserman", both sites).
- NO process narrative anywhere public. NO Co-Authored-By trailers on
  commits. User-first docs.
- Python ≥3.10, stdlib + numpy + (ffmpeg binary expected on PATH with the
  usual /opt/homebrew fallbacks). faster-whisper only inside voice/ extras.
  librosa optional extra `[beats]`.
- Source material: the reviewed Hermes plugin bridge is snapshotted at
  /tmp/dvr-source-snapshot (operations.py etc.) — port and improve; do not
  import from it at runtime.
- Sam's machine has FREE Resolve: anything live-tier cannot be E2E'd here —
  mock-test it thoroughly; free-tier features MUST be E2E'd for real
  (including an actual FCPXML import into the installed free Resolve during
  QA).
