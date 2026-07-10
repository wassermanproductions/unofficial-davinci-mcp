# Beat Cutting — cutting picture to music

Cutting to music is not "put a cut on every beat." It's mapping the song's
energy to shot density and cut placement so the edit feels like it's dancing with
the track, not marching to a metronome. This file gives you the timing numbers
and maps them to `beat_grid` and `assemble_edit`.

## The grid comes first

Run `beat_grid` on the music to get beats and onsets as a JSON grid. Everything
below is expressed relative to that grid. If librosa is installed the grid is
tempo-tracked; without it the engine falls back to an energy-envelope estimate —
still usable, but eyeball the downbeats before trusting bar math.

`beat_grid` gives you beat times. You supply the musical structure (where verse,
chorus, drop live) — the engine finds beats, not meaning. Listen once and mark
the section boundaries yourself.

## Cut placement: on, early, or off

Frames assume 24 fps unless noted; scale by your project rate.

- **On the beat.** The default. The cut lands exactly on the beat time. Reads
  tight and deliberate. Use for hard, percussive tracks and for the first cut of
  a sequence where you're establishing the rhythm.
- **1–2 frames early.** Cut slightly BEFORE the beat when the incoming shot has
  motion. The eye needs a beat or two to register a new frame; landing the cut
  1–2 frames early means the motion "arrives" ON the beat perceptually. This is
  the single most useful trick in music cutting — a strictly on-the-frame cut on
  a moving shot often feels a hair late.
- **On the off-beat (the "and").** Cut on the up-beat between beats for syncopated,
  playful, or hip-hop-inflected sequences. Alternating on-beat and off-beat cuts
  creates swing. Don't do a whole sequence off-beat unless the track itself is
  swung — it reads like a mistake.

Rule of thumb: **static shot → cut on the beat. Moving shot → cut 1–2 frames
early.** Set this via `assemble_edit`'s beat-snap tolerance (below); nudge
individual moving shots earlier by hand.

## Energy mapping: shots per phrase

Density should track the song's energy. Target shots per 8-bar phrase:

- **Intro / breakdown (low energy):** 1–3 shots per 8 bars. Let shots breathe;
  hold 2–4 seconds each. Wide, establishing, slow.
- **Verse (building):** 3–6 shots per 8 bars. One shot per 1–2 bars. Steady,
  conversational pace.
- **Pre-chorus (lift):** start shortening — 6–8 shots per 8 bars. The audience
  should feel the acceleration before the chorus hits.
- **Chorus / drop (peak):** 8–16 shots per 8 bars, i.e. cut on most or every
  beat/half-beat. This is where beat-snap earns its keep. Fastest, most kinetic.
- **Outro:** decelerate back toward intro density — cutting that stays fast into a
  fading outro feels unresolved.

The density CURVE matters more than the absolute numbers. Audiences feel the
change from verse to chorus density even if they can't count it. A flat density
across the whole song is the most common beginner mistake — it wastes the chorus.

## Land the hits

Reserve your best shot / biggest reveal for the downbeat of the chorus or the
drop. Structure the preceding phrase so the reveal lands exactly there. In
`assemble_edit`, order the plan so the hero clip's start snaps to that downbeat,
then build backward from it — don't discover the reveal position by accident.

## J-cuts and L-cuts against music

Music beds tolerate audio-led transitions well:

- **L-cut** (picture cuts, previous audio lingers): let a shot's diegetic audio or
  a vocal phrase ring a beat past the picture cut for continuity.
- **J-cut** (next audio starts early under current picture): bring the incoming
  shot's sound in 2–6 frames before its picture to pull the viewer forward.
- Over a strong music bed these are subtler than in dialogue, but they keep an
  all-music montage from feeling like a slideshow. Express as slight audio-range
  offsets vs picture ranges in the `assemble_edit` plan.

## Using beat_snap in assemble_edit

`assemble_edit` accepts a `beat_snap` option that snaps clip cut points to the
nearest grid time from `beat_grid`.

- **Tolerance in frames** — how far a cut may be moved to reach a beat. Guidance:
  - **±2 frames:** tight snap for chorus/drop sections. Cuts already near a beat
    lock on; nothing wanders.
  - **±3–4 frames:** relaxed snap for verses where you want musicality without
    forcing every cut.
  - **±6+ frames:** basically "grab the nearest beat" — only for rough assembly
    passes; too loose for a finish.
- Snap picture cuts, but apply the "1–2 frames early on motion" nudge AFTER
  snapping, or snap to a target that's already offset for moving shots.

### When NOT to snap

- **Dialogue overrides music.** If a clip carries a spoken line, the line's
  intelligibility and rhythm win. Never snap a cut into the middle of a word to
  hit a beat. Set those clips to skip beat-snap in the plan (dialogue overrides
  music, always).
- **Sync-sound performance** (a musician playing on camera) — the picture must
  match what's heard; don't slide it to a grid beat.
- **Emotional holds.** A held reaction shot that lands after a lyric can be more
  powerful off the grid. Rhythm serves feeling, not the reverse.
- **Very slow or rubato passages** where the grid is unreliable — snap will chase
  phantom beats. Cut by ear.

## Montage construction workflow (this repo)

1. `scan_media_folder` / `probe_media` to inventory clips and their usable ranges.
2. `beat_grid` on the track → the timing skeleton.
3. Mark section boundaries (intro/verse/chorus) against the grid by listening.
4. Build the `assemble_edit` plan: assign shots to sections per the density
   targets, order for the hero-shot-on-the-drop landing, set `beat_snap` tolerance
   per section (tight in chorus, loose in verse), flag dialogue/sync clips to skip
   snap, add J/L audio offsets.
5. Add `add_markers` at section boundaries so a human reviewer can navigate.
6. Interchange tier: the plan exports via `generate_fcpxml` (and `generate_edl`)
   for import into free Resolve; live tier builds the timeline directly.

## Tempo → frames per beat (24 fps)

Know how much room a beat gives you before you plan density:

- **90 BPM:** 0.667 s/beat = 16 frames. Slow, roomy — hold shots.
- **100 BPM:** 0.600 s/beat = 14.4 frames.
- **120 BPM:** 0.500 s/beat = 12 frames. The pop default.
- **128 BPM:** 0.469 s/beat = 11.25 frames. Club/EDM.
- **140 BPM:** 0.429 s/beat = 10.3 frames.
- **160 BPM:** 0.375 s/beat = 9 frames. Fast — cutting every beat here is
  frantic; consider cutting every 2nd beat and reserving every-beat for the drop.

At 12 frames/beat you have room to cut on the beat AND nudge for motion. Under ~10
frames/beat, every-beat cutting leaves no shot long enough to read — cut on the
half-bar or the downbeat instead and let the fast subdivision live in the music,
not the picture.

## Hard cut vs dissolve on music

- **Hard cut** on the beat is the default and reads as rhythmic. Almost all
  beat-driven cutting is straight cuts.
- **Dissolves** soften rhythm — use them to DROP energy (transition from chorus
  back to a mellow verse) or over sustained pads where there's no percussive beat
  to cut against. A dissolve should span roughly one beat and center on the beat,
  so the midpoint of the dissolve lands on the downbeat.
- Don't dissolve through a drop — a beat that demands a hard hit gets buried by a
  mush of two overlapping shots.

## Match-cut and action-cut on the beat

The strongest music cuts do two jobs at once: they land on the beat AND carry a
visual continuity.

- **Action cut on the beat:** a movement started in shot A completes in shot B, with
  the cut on the beat mid-motion. The beat and the motion reinforce each other —
  this is the most satisfying music cut there is.
- **Match cut on the beat:** a shape/color/composition in A rhymes with B across a
  beat cut. Save these for phrase boundaries where you want a "chapter" feel.
- Graphic-on-beat: whip-pans, light flashes, and camera shakes that peak on the
  beat let you cut inside the motion blur — the cut is invisible, the beat lands.

## Worked example — 30 s montage, 120 BPM track

1. `beat_grid` the track: 120 BPM, downbeats every 2 s, 15 downbeats in 30 s.
2. Section the track by ear: 0–8 s intro, 8–20 s verse-build, 20–30 s chorus.
3. Density plan: intro 3 shots (hold ~2.7 s each), verse-build 8 shots (~1.5 s,
   one per bar, accelerating), chorus 10–12 shots (cut every beat/half-beat).
4. Hero reveal shot ordered to start on the chorus downbeat at 20 s.
5. `assemble_edit` plan: `beat_snap` ±3 frames in intro/verse, ±2 in chorus;
   static shots snapped on-beat, the two moving push-ins offset 2 frames early.
6. `add_markers` at 8 s, 20 s (section boundaries) for the reviewer.
7. Live tier builds the timeline; interchange tier → `generate_fcpxml` +
   `generate_edl` for free Resolve import.

## Quick reference

- Static shot → cut on beat. Moving shot → cut 1–2 frames early.
- Density follows energy: intro 1–3 / verse 3–6 / chorus 8–16 shots per 8 bars.
- Best shot lands on the chorus/drop downbeat.
- Chorus snap ±2 frames; verse snap ±3–4; never snap dialogue or sync sound.
- A flat density curve wastes the song — make the chorus visibly faster.
- Under ~10 frames/beat, cut on the half-bar, not every beat.
- Action/match cuts ON the beat are the strongest music cuts — plan for them.
