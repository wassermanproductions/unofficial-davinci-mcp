# Color Looks — vocabulary to concrete moves

A look is a set of decisions about where the shadows, mids, and highlights sit,
how saturated they are, what tint lives in each zone, and how skin survives all
of it. This file turns the words directors use into numbers you can execute, and
maps every move onto this repo's tools.

## The one rule that outranks the rest

Correct exposure and white balance FIRST, then apply a look. A look LUT assumes a
neutral, correctly-exposed starting point. Never bake exposure or white-balance
fixes into a look LUT — you will re-grade the whole show every time the source
changes. In this repo that means: run `color_match` (or a manual primary pass) to
neutralize, then stack the creative look on top as a separate `.cube` via
`resolve_apply_lut` (live tier). Order on the node/effect chain: exposure/balance → look LUT → trims.

## Reading a reference image (do this before you touch a slider)

Look at five places, in this order:

1. **Skin.** Healthy skin rides a narrow hue line (roughly the "skin-tone
   indicator" vector). Note whether skin is pushed warm (golden), pushed cool
   (steely), or held neutral. Skin is the tripwire — most looks fail here.
2. **Neutrals.** Find something that should be gray/white (a shirt, paper, a
   wall). Whatever tint it carries IS the look's mid-tone bias.
3. **Shadow tint.** Look at the darkest non-black area. Teal? Green? Warm brown?
   Crushed to true black? This is the single most identity-defining choice.
4. **Highlight rolloff.** Do highlights clip hard (digital, punchy) or roll off
   softly (filmic)? Is there tint in the highlights (warm bloom, cool glass)?
5. **Saturation curve.** Is saturation uniform, or are shadows desaturated while
   mids stay rich (the common "cinematic" move)?

Write those five down as a target. That target is what you feed `color_match` as
the reference, and what you sanity-check the output against.

## Tool mapping cheat-sheet

- `color_match(reference, shots|timeline, method, strength)` — pick a graded
  reference frame; produces a per-shot transform and bakes a 33-point `.cube`.
  `method="reinhard"` for a gentle global tone/scale match; `method="lab_hist"`
  (Lab histogram) for a firmer match that also pulls tint and contrast shape.
- `strength` is a 0.0–1.0 blend of the match against the original. Treat it as
  "how much of the reference's personality to import."
- `resolve_apply_lut(lut, clips)` — live tier applies the baked `.cube`; both tiers
  always get the file to import by hand.
- When a look should be uniform across the whole piece, prefer ONE timeline LUT
  over per-clip `color_match`. Per-clip matching is for reconciling shots that
  were lit or exposed differently; a stylistic look that must read identically
  everywhere belongs on the timeline node.

### strength defaults by intent

- 0.30–0.45 — nudge; keeps the source's character, just leans it toward the ref.
- 0.55–0.70 — the honest default for "make it look like this."
- 0.80–1.00 — full transplant; use only when the reference is authoritative and
  your source is clean and neutral. High strength on mixed-lit footage wrecks
  skin.

### When to fall back to a single timeline LUT

- Talking-head / interview with consistent lighting → one timeline LUT.
- Music video or montage where you WANT uniform mood → one timeline LUT.
- Doc footage from five cameras/locations → per-clip `color_match` to a hero
  frame first (shot-matching), THEN a light timeline LUT for the shared look.

## The looks

### Teal & orange (the blockbuster default)

- **Does:** pushes shadows toward teal/cyan, highlights and skin toward orange —
  a complementary split that makes skin pop off cool backgrounds.
- **Shadows:** teal, lifted slightly (rarely crushed). **Mids:** neutral-to-warm.
  **Highlights:** warm/orange. **Saturation:** mids rich, shadows slightly down.
  **Skin:** stays orange-warm — this is why the look flatters faces.
- **Fails when:** you over-teal the shadows and skin in shadow goes green/sick;
  or backgrounds are already warm (sunset) so there's no complementary contrast.
- **This repo:** reference a warm-skin/cool-shadow frame, `method="lab_hist"`,
  `strength` 0.5–0.65. Verify skin didn't cross into yellow-green.

### Bleach bypass

- **Does:** high contrast, crushed blacks, heavily reduced saturation, silvery
  highlights — the "retained silver" film-lab effect. War films, gritty thrillers.
- **Shadows:** deep, near-black, low tint. **Mids:** contrasty, desaturated.
  **Highlights:** hot, silvery. **Saturation:** globally down 30–50%, reds survive
  most. **Skin:** ashen, pushed pale — intentional.
- **Fails when:** applied to a piece that needs warmth or approachable skin
  (weddings, lifestyle). It reads harsh and clinical.
- **This repo:** low `strength` `color_match` to a desaturated high-contrast ref,
  then bake as a timeline LUT via `resolve_apply_lut` (live) or a manual one-drop of the `.cube` (free). Because saturation loss is uniform,
  a single timeline LUT beats per-clip matching.

### Day-for-night

- **Does:** convincingly reads daytime footage as night — cool blue bias, lowered
  exposure in the grade (never in the LUT), crushed-but-visible shadows,
  desaturated, moonlight-cool highlights.
- **Shadows:** deep blue, retain some detail. **Mids:** pulled down, cool.
  **Highlights:** cool, dim — kill any warm sun spill. **Saturation:** down 40–60%.
  **Skin:** cool and dim, but keep a trace of warmth so faces don't go corpse-gray.
- **Fails when:** the sky stays bright (dead giveaway) or contrast is too flat.
  Mask/qualify the sky separately; the LUT can't fix a blown sky.
- **This repo:** primary exposure pull first, THEN the cool look LUT. Keep the two
  separate exactly as the top rule says.

### Golden hour (extend or fake it)

- **Does:** warm low-angle glow, gentle contrast, luminous highlights, skin lit
  like honey.
- **Shadows:** warm-neutral, soft (not crushed). **Mids:** warm. **Highlights:**
  warm bloom, soft rolloff. **Saturation:** up slightly in warm hues, oranges/
  yellows rich. **Skin:** glowing, warm — the whole point.
- **Fails when:** you push warmth so far whites turn pee-yellow, or shadows also
  go warm and the frame loses depth. Keep a little coolness in the deepest shadow.
- **This repo:** warm-highlight reference, `method="reinhard"` (gentle),
  `strength` 0.4–0.55. Reinhard's soft rolloff suits the luminous highlight.

### Matrix green

- **Does:** the sickly institutional green wash — everything tinted green,
  especially mids and highlights, moderate contrast.
- **Shadows:** green-black. **Mids:** green. **Highlights:** green. **Saturation:**
  green channel dominant, others suppressed. **Skin:** deliberately unhealthy,
  green-sallow.
- **Fails when:** used anywhere you want the audience to like the people on
  screen. It is an alienation look by design.
- **This repo:** heavy uniform tint → single timeline LUT. `strength` high is fine
  because the point is a total wash, not shot-matching.

### Film noir (high-key contrast B&W or near-mono)

- **Does:** stark contrast, deep blacks, blown practical highlights, minimal or
  zero saturation, hard shadow shapes.
- **Shadows:** true black, hard edge. **Mids:** compressed. **Highlights:** hot.
  **Saturation:** 0% (true noir) or ~15% (neo-noir tint). **Skin:** high-contrast,
  sculpted by light not color.
- **Fails when:** the source is flat/low-contrast — noir needs contrasty lighting
  at capture; a LUT can add contrast but can't invent shadow shape.
- **This repo:** contrast-heavy desaturated ref, timeline LUT. Watch black crush —
  losing all shadow detail can look like a codec error, not a choice.

### Pastel / airy (lifestyle, bridal, wellness)

- **Does:** lifted blacks (matte), gentle contrast, soft pastel saturation, clean
  bright skin.
- **Shadows:** lifted, milky, slight warm or pink tint. **Mids:** bright.
  **Highlights:** soft, never clipped harsh. **Saturation:** reduced and
  pastel-shifted (colors go powdery). **Skin:** bright, clean, gently warm.
- **Fails when:** lifting blacks so far the image looks foggy and low-contrast to
  the point of mush. Lift to taste, keep one true anchor black somewhere.
- **This repo:** `method="reinhard"`, low `strength` 0.35–0.5, matte-lift ref.

### Filmic — Kodak vs Fuji leanings

Two shorthand emulation targets. Neither is a single LUT; they're biases:

- **Kodak (Vision3 5219 feel):** warm skin, rich reds and oranges, creamy warm
  highlights, greens pulled slightly warm/olive. Flatters people. The default
  "expensive warm" look.
- **Fuji (Eterna feel):** cooler, muted, greens stay green-cyan, softer
  saturation, gentle contrast, cooler skin. Reads restrained, naturalistic,
  slightly melancholic.
- **This repo:** pick the reference frame that already leans the way you want and
  let `color_match` carry the bias; don't try to dial "Kodak" from scratch on
  sliders. `strength` 0.45–0.6 preserves your source's exposure while importing
  the palette lean.

## LUT etiquette (repeat, because it's where people get burned)

- **Never bake exposure or white balance into a look LUT.** Fix those upstream
  (primary grade / `color_match` neutralize pass), keep the look LUT portable.
- **Apply order:** exposure/balance → look LUT → small trims on top. If you put
  the LUT before exposure correction, every exposure move fights the LUT's
  contrast curve and skin drifts.
- **One look, one timeline LUT** when it must read identically everywhere.
  Per-clip `color_match` is for reconciling MISMATCHED shots, not for stamping a
  uniform style.
- **33-point cubes** (what this repo bakes) are plenty for grading looks; you do
  not need 65-point unless you're chasing a hard tonal transition and see banding.
- **Check skin last, every time.** After any look, pull up a face and confirm it
  didn't cross into green, yellow, or magenta. Skin is the audience's reference;
  if it's wrong, nothing else matters.

## When a look needs a human

- Mixed lighting within a single shot (window daylight + tungsten practical) — a
  global LUT can't reconcile two white points; qualify/mask or send it to a
  colorist.
- Hero skin under a stylized look for a client who cares about the talent's
  complexion — get eyes on it.
- Anything going to broadcast/theatrical delivery with a spec — the look still has
  to pass scopes; a pretty LUT that illegally clips is a reject.
