# Music Editing — cutting a song to length without breaking it

Editing a song down (or up) is about respecting phrase structure. A song is built
from phrases; cut on phrase boundaries and the edit is inaudible, cut mid-phrase
and everyone hears it. This file covers where to cut, how to end, fade lengths,
and maps to `cut_music` and `mix_plan`.

## Phrase structure is the whole game

Popular music is built in **4-bar and 8-bar phrases**, grouped into sections
(intro, verse, chorus, bridge, outro). At 120 BPM in 4/4, one bar ≈ 2 s, a 4-bar
phrase ≈ 8 s, an 8-bar phrase ≈ 16 s.

- **Only cut on a phrase boundary** — the downbeat where one 4/8-bar unit ends and
  the next begins. Splice the end of one 8-bar phrase to the start of another
  compatible phrase and the join disappears.
- To SHORTEN: drop whole phrases (a verse, a repeated chorus), never partial bars.
- To find boundaries, run `beat_grid` for the beat/onset skeleton, then count bars
  from a known downbeat. `cut_music` targets a boundary near your requested
  length rather than slicing at an arbitrary timecode.

## cut_music: finding the exit

`cut_music(song, target_seconds, ...)` finds a musical boundary near
`target_seconds` and ends there with a clean sting-out.

- The exit should land at the **end of a phrase** — ideally the end of a chorus or
  a "post-chorus button" (the resolved hit right after a chorus). Ending
  mid-verse leaves the ear hanging.
- Prefer ending slightly LONG over slightly short: it's better to land on the next
  phrase boundary past `target_seconds` than to chop before a phrase resolves.
- Land the CHORUS on your key moment. If the video has a reveal at 0:30, structure
  the music cut so the chorus downbeat hits at 0:30 — pick the exit and the
  internal phrase drops so the peak aligns with the picture peak.

## Two ways to end: ring-out vs button

- **Tail ring-out** — let the final chord/note decay naturally under an
  exponential fade. Warm, soft, unresolved-but-gentle. Fits emotional pieces,
  slow fades to black, "…and we're out" endings.
  - Fade length **1.5–2.5 s**. Shorter than 1.5 s and the ring-out sounds cut off;
    longer than ~3 s and it drags and loses energy.
- **Button ending** — end ON a hard hit (the downbeat that resolves the phrase),
  with almost no fade. Punchy, definitive, "the end." Fits promos, hard cuts to
  logo, energetic pieces.
  - Fade length **0.2–0.4 s** — just enough to kill the reverb tail's sudden stop
    without softening the hit. A button with a long fade is a contradiction.

Choose by the picture: **hard cut / logo stinger → button; dissolve / fade to
black → ring-out.** `cut_music` exposes the tail-fade length and an optional
pre-sting silence gap — set fade per the ranges above.

## The pre-sting gap

`cut_music` can insert a short **silence gap before the final sting** (a beat of
air, then the button). A 0.15–0.35 s gap before a button hit makes the ending feel
intentional and gives the last hit impact — the silence is the wind-up. Use it for
button endings; skip it for ring-outs (silence before a fade is just a hole).

## Fade lengths — quick table

- Sting-out tail (ring-out): **1.5–2.5 s** exponential.
- Button ending fade: **0.2–0.4 s**.
- Pre-sting silence gap (button only): **0.15–0.35 s**.
- Music bed fade-IN at the top of a piece: **0.5–1.5 s** (fast enough to feel
  deliberate, slow enough not to pop).
- Crossfade between two music phrases at an internal edit: **one beat or less**
  (~200–500 ms at typical tempos) right on the downbeat.

## Ducking etiquette vs cutting the arrangement

Two different problems, two different fixes:

- **Ducking** (lowering the whole bed under dialogue) is a MIX move — handle it in
  `mix_plan`, not by editing the song. Use ducking when the music should keep
  playing continuously under talking. See mixing.md for duck depths/ramps.
- **Cutting the arrangement** (removing busy instrument layers, dropping to just
  drums or a pad under a dialogue-heavy stretch) is an EDIT move. If ducking alone
  leaves the track fighting the voice, cut to a sparser section of the song under
  dialogue and bring the full arrangement back for the montage. You can't do this
  with a stereo bounce — you need stems or a section of the song that's already
  sparse. When only a stereo mix exists, duck harder and accept it, or pick a
  different, less-busy cue.

Rule: **duck first, cut the arrangement only when ducking isn't enough** and you
have the stems/sparse sections to do it cleanly.

## Key-moment alignment (worked example)

60 s promo, reveal at 0:28, end hard-cut to logo at 0:60:

1. `beat_grid` the track; note the tempo and chorus downbeats.
2. Choose the chorus whose downbeat you'll align to 0:28; count back to build the
   0:00–0:28 run-up from intro + verse phrases that total the right length (drop a
   verse if needed — whole phrases only).
3. `cut_music(song, target_seconds=60)` with a button ending + a 0.25 s pre-sting
   gap so the logo hit lands clean at 0:60.
4. Verify the chorus lands on 0:28 and the button lands on the logo frame; nudge by
   swapping a phrase, not by slicing bars.
5. Feed the cut WAV into `assemble_edit` as the bed; `mix_plan` for levels/ducking.

## mapping summary

- `beat_grid` → tempo + boundaries (bar counting).
- `cut_music(song, target_seconds, tail_fade, pre_sting_gap, ...)` → cut WAV +
  edit metadata, ending on a boundary with your chosen sting style.
- `mix_plan` → bed level and ducking (not arrangement cuts).
- `assemble_edit` → lay the cut track and align internal phrase drops to picture.

## Extending a song (loop a section)

Sometimes the picture is longer than the track. To EXTEND without an obvious loop:

- Loop a **whole phrase** (4 or 8 bars), splicing end-of-phrase to a matching
  phrase start on the downbeat — same rule as shortening, in reverse.
- Loop a section that's texturally STABLE (a groove, a repeated chorus) — looping a
  section that's obviously building or resolving exposes the seam because the ear
  expects it to go somewhere.
- Cross the loop point with a **≤1-beat crossfade on the downbeat** to hide any
  micro-timing mismatch.
- Vary looped repeats visually (the picture over the loop should change even if the
  music repeats) so the audience doesn't notice the music circling.

## Two songs / medley joins

Joining track A to track B cleanly:

- **Match on the downbeat** of a phrase boundary in both — end A at a phrase end,
  start B at a phrase start.
- **Tempo:** within ~5–6 BPM they'll butt-join fine; further apart, hard-cut on a
  strong downbeat rather than crossfade (a crossfade between mismatched tempos
  flams audibly).
- **Key:** clashing keys grate on a crossfade. Prefer a hard cut on a big hit, or a
  brief silence/riser between them so the ear resets, or transition on a drum-only
  fill where there's no harmony to clash.
- Energy match: don't cut from a peak chorus straight into a sparse intro — bridge
  with a riser or a beat of silence.

## Stems vs stereo bounce

- With **stems** (separate drums/bass/music/vocal), you can cut the arrangement
  (drop layers under dialogue), rebalance, and hide edits far better.
- With only a **stereo bounce** you're limited to phrase-level cuts, fades, and
  ducking — no surgical layer removal. Set expectations accordingly; if the job
  needs arrangement changes and you only have a bounce, ask for stems or pick a
  cue that already has the sparse sections you need.

## Tempo and key — practical notes

- Establish tempo from `beat_grid`; convert to bar length (bar = 4 beats in 4/4).
  A 4-bar phrase = 16 beats; at 120 BPM that's 8 s.
- You don't need to know the KEY to cut on phrase boundaries — the boundary is
  rhythmic. Key only matters when JOINING two different pieces of music (above).

## When to hand it to a human

- **Music-forward pieces** where the song IS the content (music videos, dance) —
  the cut must serve the performance; edit by ear.
- Complex tempo/meter changes, rubato, classical — bar math breaks down.
- Anything where a licensed track's structure can't be altered per the license —
  check before you cut a phrase out.


## Shortening a song like a music editor (the craft, not the amputation)

Cutting a song to length is ARRANGEMENT surgery, not trimming. The amateur
move - play from the top, stop at the target time, fade - always sounds like
what it is: a chunk with a fade. The professional method:

1. **Splice matching material at phrase boundaries.** Find two moments A
   (early) and B (later) where the music is nearly the same thing - same
   section type, same instrumentation, same harmonic content (end of chorus 1
   -> end of chorus 2 is the classic). Remove A->B. Because the material on
   both sides of the seam matches, the join is inaudible. Whole phrases only:
   4/8/16-bar units, never mid-bar.
2. **Keep the song's REAL ending.** A composed ending (the final cadence,
   the last hit, the ring-out the artist wrote) beats anything synthetic.
   Splice the middle out so the outro lands where you need it. This is the
   single biggest difference between seamless and obviously-chopped.
3. **Cut on the downbeat, crossfade the seam.** Splice at the attack of
   beat 1 so the new downbeat masks the join, and ALWAYS equal-power
   crossfade - 15-60 ms for well-matched material, up to a full beat when
   the match is imperfect. Raw butt joins click and thump.
4. **Ending hierarchy** when the real ending can't be used: (a) button - end
   ON a strong downbeat hit and let its natural decay ring (cut everything
   after the hit, keep the reverb tail); (b) phrase-final ring-out with an
   exponential fade matched to the tail; (c) plain musical fade over the last
   2-4 bars - the weakest option, use only when nothing else fits.
5. **Verify the seam by measurement AND ear.** A good splice's spectral
   change is no bigger than the song's ordinary beat-to-beat variation. If
   the seam's flux spikes above the song's own transitions, it is audible -
   pick the next-best splice pair and try again.

Mapping to tools: `cut_music` implements this - it builds a beat-synchronized
similarity map of the song, prefers a phrase-aligned splice that PRESERVES
the real ending, crossfades every seam, self-measures seam audibility
(rejecting audible joins), and falls back through the ending hierarchy. Read
its per-edit report: `splices` (where and why), `ending_strategy`, and
`seam_quality` before accepting the edit.
