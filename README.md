# Deadflat -- Deadlock flat-shading map guide

## What this produces

Every material used by the map's world geometry gets replaced with a flat,
untextured version: no normal map (so no fake bump/detail from lighting),
and a single solid color per material computed as the desaturated average color of
whatever texture it used to have. A brick wall becomes flat brown, a
concrete floor becomes flat grey, a patch of grass becomes flat green —
each material keeps its own distinct shade rather than everything becoming
one uniform color, but nothing has surface detail anymore. Alpha-cutout
materials (leaf cards, fences, grates) keep their cutout silhouette; only
their color is flattened, so they don't turn into solid rectangles.

This can't be built or tested from here — it needs your local Deadlock
install and Valve's Source 2 tooling, which only run on your machine. What
follows is the full pipeline: extract → flatten → recompile → pack → install.
I already wrote and tested the flattening script (`flatten_map_materials.py`)
against synthetic Source 2-shaped material files, so the logic is sound;
what I can't verify from here is the exact text format your specific
Deadlock decompile produces, since that depends on your Source 2 Viewer
version. Step 3 below tells you how to sanity-check that quickly before
running it over everything.

## Requirements

- Deadlock installed via Steam.
- [Source 2 Viewer](https://s2v.app/) (also called `Source2Viewer` /
  `HLExtract`'s successor) — for decompiling materials out of the game's VPK.
- **CSDK 12** — the community-assembled Deadlock SDK (CS2's Workshop Tools
  merged with community fixes). Get it from the Deadlock Modding Notes site's
  tools page and its linked Google Drive download; extract it somewhere
  *outside* any OneDrive-synced folder (Desktop/Documents can cause file
  permission issues), then run `csdkcfg.exe` to configure it.
- Python 3.10+ with Pillow (`pip install --break-system-packages pillow`)
  installed wherever you'll run the script — this can be the same machine.

## Step 1 — Find the map's materials

Open `game/citadel/pak01_dir.vpk` (inside your Deadlock install folder) in
Source 2 Viewer. Browse the `materials/` tree. You're looking for whatever
subfolders hold *environment art* — building facades, terrain/ground tiles,
foliage, props used to dress the map — as opposed to `materials/heroes/`,
`materials/items/`, `materials/ui/`, or particle-related folders, which you
don't want to touch. I can't tell you the exact folder names without seeing
your VPK's contents myself, but they should be recognizable once you're
browsing — often something like `world/`, `props/`, `city_construction_kit/`,
or similarly named. If Source 2 Viewer can decompile the map's own `.vmap`
file, doing that first can also surface exactly which materials it
references.

Multi-select those folders, right-click, and **Decompile & Export**,
preserving the file paths (Source 2 Viewer does this by default). Export
into a fresh `content` folder — this becomes your addon's raw-source folder.
Ideally, set this to be the `content` folder CSDK 12 generates when you
create a new addon project (Step 4 covers creating that addon), so you don't
have to move files afterward.

This step matters most for scope: you'll get a `.vmat` file plus one or more
`.vtex` + image files per material. On a full map, expect this to be
hundreds of files. That's fine — the script handles arbitrarily many.

## Step 2 — Sanity-check the format once

Before running the script on everything, open **one** decompiled `.vmat`
file in a text editor and look at how its texture references are written.
You want to see something like:

```
TextureColor = resource:"materials/whatever/foo_color.vtex"
TextureNormal = resource:"materials/whatever/foo_normal.vtex"
```

The script matches on the parameter *key* containing "color"/"diffuse"/
"albedo" (for the base color slot) or "normal"/"bump" (for the normal map
slot) case-insensitively, so it doesn't need an exact key name — but if your
decompile uses something wildly different (no "color"/"normal" substring at
all in the key), tell me and I'll adjust the matching before you run it over
everything.

Also peek inside the `.vtex` file that `TextureColor` points to — it's a
small text stub, and somewhere in it should be a quoted path ending in
`.tga`/`.png`/etc. (e.g. `m_fileName = "foo_color.tga"`). That's the actual
image the script will average. If that image sits in the same folder as the
`.vtex` (the common case), you're set.

## Step 3 — Run the flattening script

```bash
python3 flatten_map_materials.py /path/to/your/addon/content
```

This walks every `.vmat` under that folder and, for each one:

- Resolves its color texture, computes the average RGB (ignoring fully
  transparent pixels), and writes a same-size replacement if any pixel is
  translucent (preserving the cutout), or a tiny 8×8 flat swatch otherwise.
- Resolves its normal texture and replaces it with a neutral flat normal
  (128, 128, 255), preserving that texture's own alpha channel untouched
  (some Deadlock materials may pack a roughness/smoothness mask there —
  flattening color and normal shouldn't also silently change that).
- Writes new `_flat_color.vtex` / `_flat_normal.vtex` stubs (copies of the
  originals with just the image reference swapped) and rewrites the `.vmat`
  to point at them, leaving the originals untouched in case you want to
  revert one material by hand.
- Writes `content_root/flatten_report.json`, one entry per texture slot
  touched: which material, which role, the computed color, and whether it
  succeeded, was skipped, or errored.

Useful flags:

- `--dry-run` — report what it would do without writing anything. Good for
  a first pass over the whole folder before committing to it.
- `--flatten-roughness` / `--flatten-ao` — also flatten roughness and
  ambient-occlusion textures. Not required by the "flat color, no normal
  map" spec, but AO maps in particular can still visibly darken corners and
  crevices even after color/normal are flat; turn these on if you want a
  truly shadowless, uniformly-lit result.
- `--verbose` — more logging.
- Safe to re-run: it detects its own `_flat_<role>` outputs and skips them
  instead of flattening an already-flat texture again.

After it runs, open `flatten_report.json` and check for anything marked
`"skipped"` or `"error"` — those are texture slots it couldn't confidently
handle (usually because the `.vtex` stub's format didn't match what it
expected) and need a manual look.

## Step 4 — Recompile in CSDK 12

1. In CSDK 12's Asset Browser, create a new addon project if you haven't —
   this gives you paired `content/<addon>` (source) and `game/<addon>`
   (compiled) folders. If Step 1's export didn't already land inside this
   addon's `content` folder, move it there now, keeping the `materials/...`
   path structure intact.
2. In the Asset Browser, navigate to the folder(s) containing your modified
   `.vmat`/`.vtex` files, select them (or the parent folder, if batch
   recompiling a folder works in your CSDK version), right-click, and choose
   **Recompile > Full**.
3. Confirm there are no compile errors. A common one is a `.vtex` stub
   referencing settings your CSDK's shader doesn't recognize — since the
   originals compiled fine before you touched them, and the script only
   swaps the image filename inside an otherwise-untouched copy, this should
   be rare, but if it happens, open the flagged `_flat_*.vtex` and compare
   it against its original to see what differs.

## Step 5 — Package and install the addon

1. Open CS2 Workshop Manager (bundled with CSDK 12) from the Asset
   Browser's toolbar, click **New**, fill in placeholder submission info
   (name/description/preview — the actual Workshop submission will fail,
   that's expected and fine), and let it build. This produces a VPK under
   `game/citadel_addons`.
   - If your addon is large (a full map's worth of materials can add up),
     use the **Multichunk Workshop Manager** instead to avoid the 2 GB
     single-VPK limit.
2. Use the Workshop Manager's **Contents** view to confirm the VPK actually
   contains your flattened `.vmat`/`.vtex` files and not the whole game.
3. Rename the resulting file to `pak0N_dir.vpk` (pick an unused number,
   01–99) and copy it into `<Deadlock install>/game/citadel/addons/`.
4. Launch Deadlock and load into the map. If nothing changed, double check
   the addon actually packed the *recompiled* (post-Step-4) versions of the
   files, not the pre-recompile source.

## Iterating

Because the script never overwrites your originals — it only adds
`_flat_color`/`_flat_normal` siblings and repoints the `.vmat` — you can
revert any single material by hand (point its `TextureColor`/`TextureNormal`
back at the original `.vtex` and recompile just that file), or blow away the
whole `content` folder and re-decompile if you want to start over. Re-running
the script after adding more decompiled materials is safe; it won't
re-touch ones it already flattened.

## Known limitations / things to watch for

- **Scope discovery is manual.** I don't have a way to tell you which exact
  VPK folders are "the map's meshes" versus hero/item/UI art without seeing
  your decompile — that part of Step 1 is on you, using Source 2 Viewer's
  browser.
- **Shared textures get one shared color**, by design — if two different
  building facades used the same tiling brick texture, they'll end up the
  exact same shade of brown, not two different browns. That's the "average
  per material" approach; the alternative (a hand-authored category palette
  where every building is forced to one fixed hardcoded brown regardless of
  its original texture) was the other option we discussed and didn't go
  with — let me know if you'd rather have that instead, it's a different
  script.
- **Roughness/AO left alone by default**, per above — flip on the flags if
  you still see shading variation you don't want.
- **Decal/blend materials** (e.g. a material that blends two textures
  together, like a dirt-over-concrete blend) may reference *two* color
  textures under different-looking keys; the script's keyword matching
  should still catch both if their keys contain "color"/"diffuse", but
  double check the report for these.
