# Mixing — the rough-mix recipe

A rough mix has one job: make dialogue clear and consistent, sit music and effects
underneath it, and hit the loudness spec for where it's going. This file is the
recipe with the numbers, mapped to `mix_plan`. It gets you a solid, deliverable
rough — not a re-recording mixer's final, and it's honest about where a human
must take over.

## Loudness targets — set by delivery, not by taste

Integrated loudness (LUFS) is the anchor. Pick the target by destination:

- **Web / streaming / social (YouTube, Vimeo, IG, TikTok, podcasts):** dialogue/
  program at **−16 LUFS** integrated. This is the `mix_plan` default
  (`dialogue_lufs=-16`).
- **Broadcast (EBU R128):** **−23 LUFS** integrated (±0.5). This is a hard spec —
  broadcasters reject non-compliant files.
- **Broadcast (US ATSC A/85):** −24 LKFS (essentially −24 LUFS).
- **Cinema / theatrical:** not LUFS-based (dial-tone/85 dB SPL calibrated) — out of
  scope for a rough mix; flag for a dubbing stage.

Set `mix_plan(..., dialogue_lufs=...)` to the destination's number. Everything else
below is relative to that anchor.

## The dialogue anchor

Dialogue is the reference the whole mix hangs off.

- Normalize dialogue to the target integrated LUFS so it's **consistent** across
  the piece — a viewer should never reach for the volume knob between shots.
- Aim for dialogue peaks around **−6 to −3 dBFS** with the integrated loudness at
  target; if peaks are hotter, you're leaning on the limiter too hard.
- Keep dialogue **mono-summed and centered** (or center channel) unless there's a
  reason not to — off-center dialogue is fatiguing and fails on mono playback.

## Music bed level

- Music bed sits **−18 to −22 LUFS**, i.e. roughly **4–8 dB under** the dialogue
  anchor when they play together. Start at −6 dB under dialogue and adjust.
- Music-only stretches (intro, montage, outro with no VO) can come UP toward the
  program target (−16) — the bed only needs to be low when it's competing with
  words.
- If the bed is masking consonants even at −8 dB under, the problem is spectral
  (the music occupies the vocal frequency range), not just level — cut the
  arrangement (see music-editing.md) or EQ-carve the bed. Level alone won't fix a
  busy midrange.

## Ducking

Automated ducking lowers the bed when dialogue is present.

- **Duck depth: −6 to −8 dB** under the un-ducked bed level. Less than −6 and the
  bed still fights the voice; more than −10 and the duck "pumps" audibly and the
  music seems to disappear and reappear, which is distracting.
- **Ramp times: 0.3–0.5 s** for both the duck (attack) and the release. Faster than
  ~0.2 s and the level change clicks/pumps; slower than ~0.7 s and the bed is still
  loud into the first word (attack) or ducks the music long after the line ends
  (release).
- Release slightly slower than attack (e.g. attack 0.3 s, release 0.5 s) so the bed
  swells back gracefully after a line rather than snapping up.
- `mix_plan` derives ducking windows from dialogue-track silence detection: it
  ducks where there's speech, releases in the gaps. Verify the windows didn't miss
  quiet dialogue (raise sensitivity) or duck on loud breaths (tighten it).

## SFX placement relative to dialogue

- Hard effects (impacts, whooshes, UI clicks) can peak **at or slightly above**
  dialogue level for a moment — they're transient, they won't hurt the integrated
  loudness, and they need to land.
- Sustained/ambient beds (room tone, wind, traffic) sit **well under** dialogue,
  around **−24 to −30 LUFS** — felt, not heard, and never masking words.
- Place a hard SFX **on the picture hit**, not on the dialogue — if it collides
  with a word, nudge it into the nearest gap or duck the SFX under that word.

## True-peak, LRA, and the safety checks

- **True-peak ceiling: −1 dBTP.** Never let the master exceed −1 dBTP (use −2 dBTP
  for lossy-codec delivery like AAC, which can overshoot on decode). Clipping a
  true-peak ceiling is a hard fail on QC. Put a true-peak limiter last.
- **LRA (loudness range) sanity:** for spoken-word web content, LRA roughly **5–11
  LU** is healthy. Very low LRA (<4) means it's over-compressed and lifeless; very
  high (>15) on a talking piece means the level is inconsistent and quiet parts
  will be missed. Music/drama legitimately run wider.
- `mix_plan` measures integrated LUFS, true-peak, and LRA (ffmpeg EBU R128) and
  reports them — read the numbers, don't assume the target was hit.

## Check on bad speakers

Before you call a mix done, listen on the worst thing you have: a phone speaker or
cheap earbuds. If dialogue survives a phone speaker, it'll survive anything. Most
of your audience is on bad speakers — a mix that only works on studio monitors is a
mix that fails in the real world. Specifically check that dialogue is still clear
under the music on a phone, since small speakers lose the low end that would
otherwise mask less.

## Worked recipe — web talking-head with music bed

1. `mix_plan(timeline|files, dialogue_lufs=-16)`.
2. Dialogue normalized to −16 LUFS integrated, peaks ≤ −3 dBFS.
3. Music bed at −6 dB under dialogue (lands ~−20 LUFS under VO), up to ~−16 in
   music-only sections.
4. Ducking −6 dB, attack 0.3 s / release 0.5 s, windows from dialogue silence
   detection.
5. SFX transients at/just above dialogue on their picture hits; ambience −26 LUFS.
6. True-peak limiter at −1 dBTP last.
7. Confirm reported integrated ≈ −16, true-peak ≤ −1 dBTP, LRA ~5–11.
8. Phone-speaker check.

## Mapping summary

- `mix_plan(timeline|files, dialogue_lufs=-16, ...)` — measures loudness, builds
  the dialogue-normalization plan, music-bed level, and ducking windows; reports
  LUFS / true-peak / LRA. Set `dialogue_lufs` to the delivery spec.
- Ducking depth/ramp and bed level are parameters of the plan — set them from the
  ranges above.
- Interchange tier: `mix_plan` premixes audio to a file you drop onto the timeline;
  live tier can apply levels via the API.

## Gain-staging order of operations

Do these in order — the order matters as much as the values:

1. **Repair** first (denoise, de-hum, de-click) — on the raw source, before any
   gain. A leveler applied before repair just makes noise consistently loud.
2. **Clip gain / normalize per clip** so every dialogue clip is in the same
   ballpark before processing. Wildly uneven input defeats a downstream compressor.
3. **EQ** (subtractive first — cut problems before boosting).
4. **Compression** to even out dynamics.
5. **De-ess** after compression (compression can push sibilance up).
6. **Bus/loudness normalize** to the integrated LUFS target.
7. **True-peak limiter** last, at −1 dBTP.

`mix_plan` handles the leveling/loudness/ducking stages (2, 6, 7 and the ducking
windows); repair, EQ, compression, and de-essing are where a human or a dedicated
processor comes in on anything beyond a rough.

## Dialogue EQ — quick, safe moves

- **High-pass** dialogue at **80–100 Hz** (voices carry nothing useful below that;
  rumble and handling noise do). For a deep male voice, back off to ~70 Hz.
- **Cut** a narrow dip around **200–400 Hz** if the voice sounds boxy/muddy (a few
  dB, find it by sweeping).
- **Presence lift** a gentle 2–4 dB around **3–5 kHz** for intelligibility — this is
  where consonants live.
- **De-ess** harsh sibilance around **5–8 kHz** if it spits.
- Boost narrow, cut wide. Always prefer cutting a problem over boosting around it.

## Compression — starting point for dialogue

- Ratio **2:1 to 4:1**, attack **5–15 ms** (fast enough to catch peaks, slow enough
  to let consonant transients through), release **80–150 ms**, aiming for **3–6 dB**
  of gain reduction on the loud words. More than ~8 dB GR and it sounds squashed.
- Two gentle stages (a little compression twice) sound more natural than one heavy
  stage for a wide-dynamic performance.

## Reference levels at a glance (relative to a −16 LUFS dialogue anchor)

- Dialogue: −16 LUFS integrated, peaks ≤ −3 dBFS.
- Music bed under VO: ~−20 to −22 LUFS (−4 to −8 dB under dialogue).
- Music-only sections: up to ~−16 LUFS.
- Ambience / room beds: −24 to −30 LUFS.
- Hard SFX transients: at or just above dialogue, momentarily.
- Master true-peak: ≤ −1 dBTP (−2 for lossy delivery).

## When the plan needs a human

- **Music-forward pieces** (music videos, trailers with scored builds) — the music
  isn't a bed, it's the lead; a duck-the-bed recipe is wrong. Mix by ear.
- **Drama / narrative** — dynamics, perspective (on/off-mic), and worldizing are
  storytelling; a loudness-normalized flat mix flattens the performance.
- **Anything with a delivery spec beyond integrated LUFS** (stems, M&E, Dolby,
  channel layouts, dialnorm metadata) — hand to a re-recording mixer.
- **Noisy/problematic source** — denoise, de-ess, de-hum, and dialogue repair come
  BEFORE leveling; a rough-mix leveler can't fix broken source, it just makes the
  problems a consistent loudness.
