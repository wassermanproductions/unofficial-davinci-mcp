# Dialogue Editing — removing dead air without killing the breath

The goal of a dialogue tighten is a track that moves at the speed of thought but
still sounds like a person talking, not a machine gun. Cut too little and it
drags; cut too much and every pause vanishes, the rhythm goes robotic, and jump
cuts stack up. This file gives the pause thresholds and maps them to
`tighten_dialogue`.

## The core parameter: max_pause

`tighten_dialogue(clip|timeline, max_pause, ...)` removes silences longer than
`max_pause` (in seconds), leaving handles on either side. Set it by content type:

- **Interview / podcast:** `max_pause` 0.5–0.7 s. Conversation has natural
  thinking pauses; below 0.5 s you start clipping the beats that make an answer
  feel considered. 0.6 s is a safe default for talking-head interviews.
- **Tutorial / explainer / VO:** `max_pause` 0.4–0.6 s. Instructional content
  wants to be brisk and information-dense; tighter than conversation. 0.5 s
  default.
- **Corporate / sizzle read:** 0.35–0.5 s. Energetic, punchy.
- **Drama / scripted performance:** DON'T auto-tighten. Performance pauses are
  acting choices — a held silence is a line reading. Cut drama by hand, beat by
  beat. If you must, use a very high `max_pause` (1.5 s+) only to catch dead slate
  air at heads and tails, never mid-scene.

Lower `max_pause` = tighter and faster but more jump-cut risk. Raise it whenever
the result starts sounding breathless.

## Keep-handles: why the tool leaves padding

`tighten_dialogue` keeps small handles (lead-in / lead-out) around each retained
segment instead of butt-splicing at the exact waveform edge. Reasons:

- **Consonant attacks and tails** live in the quiet moments right before/after the
  loud part of a word. Cut flush and you clip the "t", "k", "s" — speech starts to
  lisp and sound chopped.
- **Room tone continuity** — a few frames of handle lets crossfades hide the
  splice. A hard cut from speech straight to speech swaps room tone abruptly and
  clicks.
- Typical handle: **60–120 ms** each side (about 2–3 frames at 24 fps). Enough to
  protect consonants, short enough to still tighten. If splices click, increase
  the handle before you blame the fade.

Always put a **short crossfade (20–40 ms)** on the audio at each cut to mask the
room-tone seam. This is cheaper insurance than re-cutting.

## Breath handling

Breaths are where robotic edits are born.

- **Cut the breath BEFORE a sentence, keep the breath AFTER.** A person inhales to
  start speaking; if you cut that intake the next sentence begins on an unnatural
  glottal jump. The out-breath after a sentence is part of the phrase's natural
  decay — leaving it sounds human.
- Don't remove ALL breaths. A track with zero breaths sounds synthetic and
  exhausting — the listener unconsciously holds their own breath. Keep breaths at
  the ends of thoughts.
- Reduce, don't delete, loud distracting breaths: a −6 to −10 dB dip on the breath
  beats cutting it out entirely, which leaves a suspicious gap.

## Avoiding the robotic / jump-cut rhythm

The failure mode of aggressive tightening: every pause is exactly `max_pause`
long, so the cadence becomes metronomic and unnatural, and on camera every removed
pause is a visible jump cut.

- **Leave every 3rd–4th pause intact.** Perfect evenness is the tell. Preserving
  some natural pauses restores irregular, human cadence. If the tool exposes a
  "keep every Nth" or a randomized-retention option, set it to keep ~1 in 3–4;
  otherwise restore a few by hand after the pass.
- **Vary the tightness by section.** Tighten dense expository stretches harder;
  let emotional or emphatic moments keep their air.
- **Watch for stacked jump cuts.** Three tight cuts in a row on the same framing
  reads as a glitch. Space them or hide them (below).

## Hiding the cuts the plan creates (picture side)

Every removed pause on a single-camera talking head is a jump cut. Options, best
to worst:

1. **B-roll / cutaway** over the splice. The gold standard — the audio tightens,
   the picture never jumps because we're looking at something else. Plan B-roll to
   land ON the tightest clusters of cuts.
2. **Punch-in / reframe.** Alternate a wide and a ~15–20% punched-in version of
   the same shot; a jump cut between two different framings reads as a deliberate
   two-camera cut. Alternate, don't punch every cut the same way.
3. **Cutaway to a second angle** if you shot multi-cam — cleanest of all.
4. **Speed-ramp / morph transitions** — last resort; overused and often visibly
   artificial. A morph over a big jump is a tell.

Map the cut list from `tighten_dialogue` to where you need coverage: the tool's
output tells you exactly where the jump cuts land, so you know where B-roll or
punch-ins are required before you finish the picture.

## Matching params to the rules — worked settings

- **Podcast interview, single cam, lots of B-roll available:** `max_pause` 0.6 s,
  handles ~100 ms, keep ~1 in 3 pauses, 30 ms audio crossfades. B-roll over the
  dense clusters.
- **Tutorial screen-record VO:** `max_pause` 0.5 s, handles ~80 ms, tighter
  because picture is a screen (no jump-cut problem), keep ~1 in 4.
- **Emotional interview segment:** raise `max_pause` to 0.9–1.1 s just for that
  segment; let the pauses carry the weight.
- **Drama:** no auto-tighten. Hand-cut.

## Workflow (this repo)

1. `probe_media` / `scan_media_folder` to confirm clip audio and duration.
2. `tighten_dialogue(clip|timeline, max_pause=...)` → cut list + a tightened
   timeline plan. Live tier applies it; interchange tier exports via
   `generate_fcpxml`.
3. Read the cut list to find jump-cut locations; plan B-roll / punch-ins there.
4. `assemble_edit` to lay B-roll and cutaways over the flagged splices.
5. `add_markers` at any spot needing a human decision (an over-tight passage, a
   breath you want restored).
6. Hand the picture back for the room-tone crossfades if the plan didn't add them.

## Filler words and false starts

`tighten_dialogue` removes silence, not words — but the cut points it creates are
where you also clean speech disfluencies by hand:

- **Ums, uhs, likes, you-knows:** cut them, but leave a few frames of the
  surrounding pause so the sentence doesn't slam shut. Removing an "um" mid-sentence
  usually needs a matching handle trim on both sides.
- **False starts** ("I think— I mean, what I'm saying is…"): cut back to the clean
  restart. The removed audio is often longer than a pause, so mark these for the
  hand pass; the silence detector won't catch a spoken false start.
- **Repeated words** from a stutter or re-take: keep the strongest take of the
  phrase, cut on the breath before it so the join is clean.

Don't strip EVERY filler — a completely disfluency-free interview can sound coached
and inhuman. Clean the distracting ones, keep the character.

## Finding a clean cut point

When you need to place a splice by hand (not just trust `max_pause`):

- Cut in the **quietest frame** of the pause, not at the edge of a word — maximum
  room-tone overlap for the crossfade.
- Cut **before a consonant, not a vowel** — a splice into the start of a hard
  consonant (t, k, p, b) hides better than into an open vowel, which exposes a
  pitch/tone jump.
- Never cut in the middle of a word's sustain (a held vowel or an "mmm") — the
  waveform discontinuity clicks and the pitch jumps audibly.

## Pre-lap and overlap for pace

Beyond removing silence, you can INCREASE pace by overlapping:

- **Pre-lap:** bring the next speaker's first word in a few frames before their
  picture (a J-cut on dialogue). Makes an exchange feel quick and eager.
- **Tight overlap** in an argument or fast exchange: let the tail of one line ride
  a frame or two into the head of the next. Use sparingly — too much and it's
  unintelligible.
- These are picture/audio-range offsets in the `assemble_edit` plan, same mechanism
  as J/L cuts.

## When to leave it to a human

- Drama and any performance-driven material.
- Overlapping dialogue / crosstalk — automated silence detection can't tell who's
  talking; separate before tightening.
- Heavily noisy field audio where "silence" is never actually silent — the
  detector needs a clean floor; denoise or set `max_pause` conservatively and
  finish by hand.

## Editing by transcript

Silence detection can only cut the quiet. Word-level editing cuts the *words*.
Use it when the cleanup lives in the speech, not the gaps:

- **Fillers mid-phrase** — an "um" or "uh" that lands between two words, where no
  pause exists for `tighten_dialogue` to find.
- **False starts / restarts** — "what I— what I mean is": a spoken repetition the
  silence detector sails right past.
- **Interview cleanup** — trailing "you know", "sort of", scattered "like".

`transcribe_media(path)` runs on-device (faster-whisper) and returns per-word
timings; Resolve Studio's native in-app transcription is an alternative, but the
engine works in both tiers and needs no running Resolve. Then
`cut_by_transcript(media, transcript_json)` builds the cut plan.

**Danger zones — where transcript cuts go robotic:**

- **Never cut the breath in the middle of a phrase.** Removing a filler between
  two words of one thought slams the words together and sounds synthetic. The
  tool leaves 60–120 ms handles for exactly this; don't set them to zero.
- **Keep every 3rd–4th pause.** Collapsing all of them makes cadence metronomic;
  raise `max_pause` on emotional passages so the air survives.
- **Filler words carry meaning in emotional takes** — a hesitant "I… um… I don't
  know" is a performance. Don't machine-strip drama; a leading filler that opens
  a sentence is kept by default, and `tighten_only` skips word removal entirely.

**Workflow:**

1. `transcribe_media` (or point at an existing transcript JSON).
2. `cut_by_transcript` as a dry run — inspect the returned **reasons** list: every
   cut is tagged filler / pause / restart with the offending text.
3. Human-review the reasons; restore any filler that was a beat. The safety floor
   refuses to render a pass that guts more than half the take.
4. Confirm to render a preview, or hand the `assemble_plan` to `assemble_edit` /
   `generate_fcpxml` for interchange, or apply it live.
