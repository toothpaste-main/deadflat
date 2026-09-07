# Deadlock Flat-Shading Map Mod

Replaces every map material with a flat grey — no normal maps, no texture
detail. Each material gets its own shade (luma-matched to its original
texture, then desaturated), so surfaces stay distinguishable from each
other. Alpha-cutout materials (foliage, fences) keep their cutout shape.

Everything below except running `flatten_map_materials.py` itself happens
on your machine with Valve's Source 2 tools — none of it can be done from
a chat session.

## Requirements

- **Python 3.10+ with Pillow.** PowerShell: `python --version` (try `py
  --version` if that fails — Windows often skips registering `python3`).
  Then `pip install pillow` (skip `--break-system-packages`; that's a
  Linux-only workaround).
- **[Source 2 Viewer](https://s2v.app/)** — decompiles the game's materials.
- **CSDK 12** — the community Deadlock SDK (Deadlock Modding Notes site's
  tools page). Needed to recompile and pack the mod; Source 2 Viewer alone
  can't do either. Extract outside any OneDrive-synced folder, then run
  `csdkcfg.exe`.

## Quick start

1. **Create an addon project** in CSDK 12's Asset Browser — gives you a
   `content/<addon>` (source) / `game/<addon>` (compiled) folder pair.
2. **Decompile the map's materials** in Source 2 Viewer: open
   `game/citadel/pak01_dir.vpk`, browse `materials/`, and export only the
   *environment-art* folders (buildings/ground/props — not `heroes/`,
   `items/`, `ui/`, particles), preserving paths, into that addon's
   `content` folder.
3. **Sanity-check one file** before running the script on everything: open
   a decompiled `.vmat` and confirm its texture keys look like
   `TextureColor = resource:"...vtex"` / `TextureNormal = resource:"...vtex"`,
   and that its `.vtex` contains a quoted image path (e.g.
   `m_fileName = "foo_color.tga"`). If it looks very different, flag it
   before proceeding.
4. **Run the script** (see [Usage](#usage) below) against that `content`
   folder, then check `flatten_report.json`.
5. **Recompile** the changed `.vmat`/`.vtex` files in CSDK 12's Asset
   Browser (right-click → Recompile > Full).
6. **Pack the addon**: CS2 Workshop Manager (bundled with CSDK 12) → New →
   fill placeholder info → build (the submission itself will fail — that's
   expected). Use Multichunk Workshop Manager instead if it's over 2 GB.
   Rename the output to `pak0N_dir.vpk` (01–99) and copy it into
   `<Deadlock install>/game/citadel/addons/`.
7. **Launch Deadlock** and check the map.

## Usage

```
python flatten_map_materials.py <content_folder> [options]
```

`<content_folder>` is the decompiled addon folder containing `.vmat` +
`.vtex` + image files together (with the `materials/...` structure intact)
— not a folder of images alone.

| Flag | Effect |
|---|---|
| *(none)* | Flatten color to a desaturated grey per material, normal maps to neutral (128,128,255), and write the changes. |
| `--dry-run` | Compute and report everything, write `flatten_report.json`, but don't touch any material/texture files. Run this first. |
| `--keep-hue` | Keep each material's own averaged color instead of desaturating it to grey. |
| `--flatten-roughness` | Also flatten roughness textures to their average value. |
| `--flatten-ao` | Also flatten ambient-occlusion textures to flat white — useful if AO shadowing still shows through corners/crevices after color and normal are flat. |
| `--report PATH` | Write the JSON report somewhere other than `<content_folder>/flatten_report.json`. |
| `-v`, `--verbose` | More detailed logging. |

### Behavior worth knowing

- **Safe to re-run.** It detects its own `_flat_color`/`_flat_normal`
  output and skips it instead of flattening an already-flat texture again
  — so you can decompile more materials later and just run it again.
- **Shared textures get one shared grey.** If two materials use the same
  tiling texture, they end up the same shade, since the color comes from
  averaging that shared texture, not from a per-material category.
- **Originals are never overwritten** — the script writes new
  `_flat_color.vtex` / `_flat_normal.vtex` files next to the originals and
  repoints the `.vmat`, so you can revert a single material by hand.
- **`flatten_report.json`** lists every texture slot it touched: which
  material, which role (color/normal/roughness/ao), the resulting color,
  and its status (`flattened`, `already-flat`, `skipped`, or `error`).
  Check anything `skipped`/`error` by hand — usually a `.vtex` whose format
  didn't match what the script expected.
- **Decal/blend materials** (two color textures blended together) are
  handled if both texture keys contain "color"/"diffuse"/"albedo" — check
  the report to confirm both got caught.
