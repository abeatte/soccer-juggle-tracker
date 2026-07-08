# Tuning accuracy

Detection, pose, tracking, and identity are already wired and work out of the
box. The part that needs your input is **juggle-counting accuracy**, because it
depends on your camera angle, distance, lighting, and where the ground sits in
the frame. This is inherently an iterative, footage-driven process — plan for a
few short sessions, not one.

There are two phases:

- **Part 1 — First-clip calibration** (once): get the ROI, ground line, and ball
  detection dialled in so the debug video looks right.
- **Part 2 — Ongoing regression tuning** (repeat): keep a small set of
  hand-counted clips and measure error whenever you change a threshold, so you
  know a change actually helped.

---

## Part 1 — Your first real clip (calibration walkthrough)

### Step 1 — Capture a representative clip
Record 20–30 s of a kid actually juggling, from the **real mounted camera
position** (angle matters more than anything).

```bash
source .venv/bin/activate
python -m juggle_tracker.cli record --seconds 30
# -> writes inbox/clip_<ts>.mp4
```

### Step 2 — Process it with a debug overlay
```bash
python -m juggle_tracker.cli process inbox/clip_<ts>.mp4 --debug-video out.mp4
```
Open `out.mp4`. The overlay shows: person boxes + track IDs, the ball circle,
pose keypoints (magenta dots), and the live `streak:` counter. **Watch what the
machine sees** — this is your primary tuning instrument.

### Step 3 — Fix the four things, in order

1. **Is the ball detected on (almost) every frame?**
   Look for the orange ball circle. If it flickers/disappears, especially at the
   top/bottom of arcs (motion blur):
   - lower `models.ball_conf` (try `0.15`, even `0.10`)
   - raise `processing.infer_long_edge` (try `1280`) — slower but sharper
   - tighten the ROI (next step) so the ball is larger in the analysis frame.
   The ball is the single most important signal; get this right first.

2. **Is the ROI cropped to the play area?**
   A tight crop makes everything larger (better detection) and faster (less CPU).
   Set `roi: [x0, y0, x1, y1]` as fractions `0–1`. Re-run and confirm the kid +
   full ball arc stay inside the crop at all times.

3. **Calibrate the ground line.**
   Find the image-y where the ball would rest on the grass. Estimate it as a
   fraction of frame height (top = 0.0, bottom = 1.0) and set
   `juggle.ground_y_frac`. Contacts at/below this line are treated as floor
   touches and reset the streak. If real juggles are being reset as "ground",
   the line is too high (increase the value toward 1.0). If dropped balls aren't
   ending the streak, it's too low.

4. **Sanity-check contacts vs pose.**
   Watch the `streak:` counter tick up. If it:
   - **overcounts** small oscillations → raise `juggle.min_arc_px` (e.g. `25–30`)
   - **counts hand catches** → confirm `illegal_keypoints` includes wrists/elbows
     and that pose keypoints actually land on the arms (if pose is too coarse,
     raise `infer_long_edge`); optionally lower `contact_radius_px` so a far-away
     keypoint isn't credited
   - **misses valid touches** → raise `contact_radius_px` a little, or add the
     relevant keypoint to `valid_keypoints`.

### Step 4 — Re-run after each change
Change **one** knob at a time and re-process the same clip. Converging on good
settings usually takes 4–8 passes on the first clip.

### Symptom → knob quick reference

| Symptom in debug video | Config knob | Direction |
|---|---|---|
| Ball flickers / lost mid-arc | `models.ball_conf` | ↓ lower |
| Ball too small / blurry | `processing.infer_long_edge`, `roi` | ↑ / tighter |
| Jitter counted as juggles | `juggle.min_arc_px` | ↑ raise |
| Hand/catch counted | `juggle.contact_radius_px` | ↓ lower |
| Real touches missed | `juggle.contact_radius_px` | ↑ raise |
| Valid juggles reset as "ground" | `juggle.ground_y_frac` | ↑ toward 1.0 |
| Dropped ball not ending streak | `juggle.ground_y_frac` | ↓ lower |
| Wrong/"Unknown" person | more enroll images; `identity.match_threshold` | ↓ lower = more lenient |
| Analysis too slow | `infer_long_edge` ↓, `person_stride` ↑, tighter `roi` | — |

---

## Part 2 — Ongoing tuning (the sustainable process)

Eyeballing debug videos doesn't scale and can't tell you whether a change made
things better *overall*. Instead, keep a small **labelled validation set** and
measure error numerically after every change.

### 2.1 Build a validation set (once, then grow it)
1. Create `clips/` and copy in 8–15 varied clips: different kids, distances,
   lighting (sun/shade/dusk), short and long streaks, plus a couple of
   deliberate "hard" ones (hand catch, dropped ball, two kids passing through).
2. **Hand-count** the true best streak in each clip.
3. Record them in a CSV, e.g. `ground_truth.csv`:

   ```csv
   clip,expected
   clips/kid1_sunny_27.mp4,27
   clips/kid1_shade_14.mp4,14
   clips/kid2_dusk_8.mp4,8
   clips/hard_handcatch.mp4,5
   ```

   Keep this CSV in the repo (it's just filenames + numbers — no PII). The clips
   themselves stay gitignored under `clips/`.

### 2.2 Measure with the eval harness
`tools/eval.py` runs a **sandboxed** pipeline (Home Assistant disabled, throwaway
DB) so evaluating never disturbs your real scores:

```bash
python tools/eval.py ground_truth.csv
```

Output:

```
clip                                      pred   exp   err
----------------------------------------------------------
kid1_sunny_27.mp4                           26    27    -1
kid1_shade_14.mp4                           14    14    +0
kid2_dusk_8.mp4                              6     8    -2
hard_handcatch.mp4                           5     5    +0
----------------------------------------------------------
MAE (mean absolute error): 0.75 juggles over 4 clips
```

Add `--debug-dir dbg/` to also drop an overlay video per clip for the ones that
are off.

### 2.3 The change loop
1. Note the **current MAE** (your baseline).
2. Change **one** config value.
3. Re-run `tools/eval.py`.
4. Keep the change only if MAE **dropped** (and no individual clip got much
   worse). Revert otherwise.
5. Commit good configs so you can roll back:
   ```bash
   git add config.yaml ground_truth.csv && git commit -m "tune: lower ball_conf, MAE 1.1 -> 0.75"
   ```

> Because `config.yaml` is gitignored (it holds credentials), either commit a
> sanitised copy as `config.tuned.yaml`, or keep a `tuning-log.md` recording each
> config + its MAE. A one-line-per-experiment log is enough.

### 2.4 When to grow the set
Add a clip to the validation set whenever you hit a **new failure mode** in
production (a lighting condition, a trick, an occlusion the set didn't cover).
The set is your regression guard — it should accumulate the hard cases so a
future change can't silently reintroduce an old bug.

### 2.5 Realistic expectations
- Getting MAE to ~1–2 juggles on clean, single-kid, well-lit footage is
  achievable with threshold tuning alone.
- Pushing lower, or handling messy footage (harsh shadows, tiny distant ball,
  two kids), is where the long tail of effort lives and may need model upgrades
  (a larger YOLO, a ball-specific fine-tune) — a later step, not v0.1.

### 2.6 Periodic re-check
Re-run `tools/eval.py` after: any config change, an Ultralytics/model update, a
camera reposition, or seasonal lighting shifts. Treat a rise in MAE as a
regression to investigate.
