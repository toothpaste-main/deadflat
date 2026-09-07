#!/usr/bin/env python3
"""Flatten decompiled Source 2 (Deadlock) materials to solid colors.

This script is meant to run *locally*, against a folder of materials you
have already decompiled out of Deadlock's pak01_dir.vpk with Source 2
Viewer (see the accompanying guide for that step). It does not touch the
game, a VPK, or a running compiler -- it only rewrites text (.vmat/.vtex)
files and writes new small texture images next to the ones already on
disk, inside your own addon content folder.

For every ``.vmat`` material file found under ``content_root``:

  * Any texture bound to a "color"-like slot (TextureColor, TextureDiffuse,
    TextureAlbedo, ...) is replaced with a same-size image whose RGB is the
    average color of the original texture, with the original per-pixel
    alpha preserved. Alpha is preserved (rather than also being flattened)
    so that alpha-tested foliage/fence/grate cards keep their cutout
    silhouette instead of turning into solid rectangles. Fully opaque
    textures are additionally shrunk to a tiny flat swatch, since a
    uniform color has no spatial detail worth keeping at full resolution.
  * Any texture bound to a "normal"-like slot (TextureNormal, TextureBump)
    has its RGB replaced with a neutral tangent-space normal
    (128, 128, 255) -- i.e. "no bump" -- while its alpha channel is left
    alone, because some Source 2 shaders pack a roughness/smoothness mask
    into the normal map's alpha channel and blindly flattening it would
    change shading in ways nobody asked for.
  * Roughness and ambient-occlusion textures are left untouched unless you
    pass --flatten-roughness / --flatten-ao.

A texture that is shared by multiple materials (e.g. a tiling ground
texture) is only processed once and reused, so every material that used
that texture ends up the same flat color.

The script never guesses at exact Source 2 shader parameter names beyond
simple keyword matching, and it never guesses at the internal structure of
a .vtex compile-source stub beyond "find a quoted path ending in a known
image extension". Both of those are deliberately loose because the exact
text Source 2 Viewer emits can vary by version; if it fails to find a
reference it will say so in the report and skip that slot rather than
guessing.

Example:
    python3 flatten_map_materials.py ./my_addon/content --flatten-ao
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

LOGGER = logging.getLogger("flatten_map_materials")

# Image extensions Source 2 Viewer commonly exports texture sources as.
IMAGE_EXTENSIONS = (".tga", ".png", ".psd", ".exr", ".jpg", ".jpeg")

# Matches "Key = resource:"path/to/thing.vtex"" (modern KV3 decompiles)
# and "Key resource:"path/to/thing.vtex"" (no '=').
_KV3_TEXTURE_RE = re.compile(
    r'(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=?\s*resource:\s*"(?P<path>[^"]+\.vtex)"'
)

# Matches legacy-style '"Key" "path/to/thing.vtex"' quoted pairs.
_KV1_TEXTURE_RE = re.compile(
    r'"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s+"(?P<path>[^"]+\.vtex)"'
)

# Any quoted string ending in a known image extension, used to find the
# source image referenced inside a decompiled .vtex compile-source stub.
_IMAGE_EXT_PATTERN = "|".join(ext.lstrip(".") for ext in IMAGE_EXTENSIONS)
_IMAGE_REF_RE = re.compile(
    r'"([^"]+\.(?:' + _IMAGE_EXT_PATTERN + r'))"',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoleConfig:
    """How to flatten one texture "role" (color, normal, roughness, ...)."""

    name: str
    key_hints: tuple[str, ...]
    preserve_alpha: bool
    neutral_rgb: tuple[int, int, int] | None  # None means "use the average"
    shrink_if_opaque: bool


DEFAULT_ROLES: tuple[RoleConfig, ...] = (
    RoleConfig(
        name="color",
        key_hints=("color", "diffuse", "albedo"),
        preserve_alpha=True,
        neutral_rgb=None,
        shrink_if_opaque=True,
    ),
    RoleConfig(
        name="normal",
        key_hints=("normal", "bump"),
        preserve_alpha=True,
        neutral_rgb=(128, 128, 255),
        shrink_if_opaque=False,
    ),
)

OPTIONAL_ROLES: dict[str, RoleConfig] = {
    "roughness": RoleConfig(
        name="roughness",
        key_hints=("roughness", "rough"),
        preserve_alpha=False,
        neutral_rgb=None,
        shrink_if_opaque=True,
    ),
    "ao": RoleConfig(
        name="ao",
        key_hints=("ambientocclusion", "occlusion"),
        preserve_alpha=False,
        neutral_rgb=(255, 255, 255),
        shrink_if_opaque=True,
    ),
}

FLAT_SWATCH_SIZE = 8  # px, for fully-opaque textures that get shrunk


@dataclass
class MaterialResult:
    vmat: str
    role: str
    key: str
    source_texture: str | None
    output_texture: str | None
    average_rgb: tuple[int, int, int] | None
    status: str
    detail: str = ""


@dataclass
class Manifest:
    results: list[MaterialResult] = field(default_factory=list)

    def add(self, result: MaterialResult) -> None:
        self.results.append(result)

    def write(self, path: Path) -> None:
        payload = [vars(r) for r in self.results]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def find_vmat_files(content_root: Path) -> list[Path]:
    """Return every .vmat file under content_root, sorted for determinism."""
    return sorted(content_root.rglob("*.vmat"))


def classify_key(key: str, roles: tuple[RoleConfig, ...]) -> RoleConfig | None:
    """Return the RoleConfig whose hints match this parameter key, if any."""
    lowered = key.lower()
    for role in roles:
        if any(hint in lowered for hint in role.key_hints):
            return role
    return None


def extract_texture_refs(vmat_text: str) -> list[tuple[str, str, int, int]]:
    """Return (key, vtex_path, span_start, span_end) tuples found in a
    decompiled .vmat's text.

    The span covers exactly the path characters between the quotes, so a
    caller can splice in a replacement by position instead of by content --
    two different texture slots can legitimately reference the identical
    path string (e.g. a material reusing one texture for two roles), and a
    plain str.replace(old, new) would then clobber both with whichever
    replacement ran last.
    """
    refs: list[tuple[str, str, int, int]] = []
    seen_spans: set[tuple[int, int]] = set()
    for pattern in (_KV3_TEXTURE_RE, _KV1_TEXTURE_RE):
        for match in pattern.finditer(vmat_text):
            span = match.span("path")
            if span in seen_spans:
                continue
            seen_spans.add(span)
            refs.append((match.group("key"), match.group("path"), span[0], span[1]))
    return refs


def resolve_under_root(ref_path: str, content_root: Path) -> Path | None:
    """Resolve a path referenced inside a .vmat/.vtex to a real file.

    Tries the path as given (relative to content_root) first, then falls
    back to a suffix search, since decompile tools don't always agree on
    where the "root" of a relative reference sits.
    """
    direct = content_root / ref_path
    if direct.is_file():
        return direct

    ref_tail = Path(ref_path)
    candidates = [
        p
        for p in content_root.rglob(ref_tail.name)
        if p.as_posix().endswith(ref_tail.as_posix())
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        LOGGER.warning(
            "Ambiguous reference %r resolved to %d candidates; using the first: %s",
            ref_path,
            len(candidates),
            candidates[0],
        )
        return candidates[0]
    return None


def find_image_ref_in_vtex(vtex_path: Path) -> Path | None:
    """Find the source image a decompiled .vtex compile-stub points at."""
    text = vtex_path.read_text(encoding="utf-8", errors="ignore")
    match = _IMAGE_REF_RE.search(text)
    if not match:
        return None
    image_ref = match.group(1)
    candidate = (vtex_path.parent / image_ref).resolve()
    if candidate.is_file():
        return candidate
    # Fall back to a same-directory basename match.
    same_dir = vtex_path.parent / Path(image_ref).name
    if same_dir.is_file():
        return same_dir
    return None


def compute_flat_image(image_path: Path, role: RoleConfig) -> tuple[Image.Image, tuple[int, int, int]]:
    """Build the flattened replacement image for one source texture.

    Returns the new PIL image plus the average RGB actually used, so
    callers can log it.
    """
    source = Image.open(image_path)
    has_alpha = "A" in source.getbands()
    rgba = source.convert("RGBA")
    pixels = rgba.getdata()

    if role.neutral_rgb is not None:
        avg_rgb = role.neutral_rgb
    else:
        total = [0, 0, 0]
        count = 0
        for r, g, b, a in pixels:
            if a == 0:
                continue
            total[0] += r
            total[1] += g
            total[2] += b
            count += 1
        if count == 0:
            avg_rgb = (128, 128, 128)
        else:
            avg_rgb = (total[0] // count, total[1] // count, total[2] // count)

    fully_opaque = not has_alpha or all(a == 255 for *_, a in pixels)

    if role.preserve_alpha and not fully_opaque:
        flat = Image.new("RGBA", rgba.size)
        alpha_channel = rgba.split()[3]
        solid = Image.new("RGBA", rgba.size, (*avg_rgb, 255))
        flat.paste(solid, (0, 0))
        flat.putalpha(alpha_channel)
        return flat, avg_rgb

    if role.shrink_if_opaque:
        size = (FLAT_SWATCH_SIZE, FLAT_SWATCH_SIZE)
    else:
        size = rgba.size
    flat = Image.new("RGBA", size, (*avg_rgb, 255))
    return flat, avg_rgb


def write_flat_replacement(
    original_vtex_path: Path,
    original_image_path: Path,
    flat_image: Image.Image,
    role_name: str,
) -> Path:
    """Write the flat image + a patched .vtex stub next to the originals.

    ``role_name`` is folded into the output filenames so that two different
    roles (e.g. "color" and "normal") never collide even in the unusual
    case where they happened to point at the exact same source texture.

    Returns the path to the new .vtex stub.
    """
    suffix_tag = f"_flat_{role_name}"
    image_suffix = original_image_path.suffix or ".tga"
    new_image_name = f"{original_image_path.stem}{suffix_tag}{image_suffix}"
    new_image_path = original_image_path.with_name(new_image_name)
    save_kwargs = {}
    if image_suffix.lower() == ".tga":
        save_kwargs["format"] = "TGA"
    flat_image.save(new_image_path, **save_kwargs)

    vtex_text = original_vtex_path.read_text(encoding="utf-8", errors="ignore")
    patched_text = vtex_text.replace(original_image_path.name, new_image_name)
    if patched_text == vtex_text:
        LOGGER.warning(
            "Could not find %r inside %s to patch; new .vtex will still "
            "reference the old image name -- check this one by hand.",
            original_image_path.name,
            original_vtex_path,
        )

    new_vtex_name = f"{original_vtex_path.stem}{suffix_tag}{original_vtex_path.suffix}"
    new_vtex_path = original_vtex_path.with_name(new_vtex_name)
    new_vtex_path.write_text(patched_text, encoding="utf-8")
    return new_vtex_path


def process_vmat(
    vmat_path: Path,
    content_root: Path,
    roles: tuple[RoleConfig, ...],
    cache: dict[tuple[Path, str], tuple[Path, tuple[int, int, int]]],
    manifest: Manifest,
    dry_run: bool,
) -> str:
    """Process one .vmat file in place. Returns the (possibly unchanged) text."""
    text = vmat_path.read_text(encoding="utf-8", errors="ignore")
    rel_vmat = vmat_path.as_posix()
    # (span_start, span_end, replacement_text), applied by position at the
    # end so that two slots sharing an identical original path don't clobber
    # each other via a content-based string replace.
    pending_replacements: list[tuple[int, int, str]] = []

    for key, vtex_ref, span_start, span_end in extract_texture_refs(text):
        role = classify_key(key, roles)
        if role is None:
            continue

        if vtex_ref.endswith(f"_flat_{role.name}.vtex"):
            # Already flattened by a previous run of this script -- leave it
            # alone instead of flattening the flattened output again (which
            # would be a harmless no-op color-wise but would pile up
            # "_flat_color_flat_color..." filenames on every re-run).
            manifest.add(
                MaterialResult(
                    rel_vmat, role.name, key, vtex_ref, vtex_ref, None,
                    "already-flat", "reference already points at flattened output",
                )
            )
            continue

        vtex_path = resolve_under_root(vtex_ref, content_root)
        if vtex_path is None:
            manifest.add(
                MaterialResult(
                    rel_vmat, role.name, key, vtex_ref, None, None,
                    "skipped", "could not resolve .vtex path on disk",
                )
            )
            continue

        # Keyed by (file, role) rather than just file: the same texture can
        # in principle be bound into two different slots (e.g. a stray
        # material reusing a color map as its normal map), and those slots
        # must not silently share one flattening result.
        cache_key = (vtex_path, role.name)
        if cache_key in cache:
            new_vtex_path, avg_rgb = cache[cache_key]
        else:
            image_path = find_image_ref_in_vtex(vtex_path)
            if image_path is None:
                manifest.add(
                    MaterialResult(
                        rel_vmat, role.name, key, vtex_path.as_posix(), None, None,
                        "skipped", "could not find source image inside .vtex stub",
                    )
                )
                continue
            try:
                flat_image, avg_rgb = compute_flat_image(image_path, role)
            except Exception as exc:  # noqa: BLE001 - report and keep going
                manifest.add(
                    MaterialResult(
                        rel_vmat, role.name, key, image_path.as_posix(), None, None,
                        "error", f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if dry_run:
                new_vtex_path = vtex_path  # not actually written
            else:
                new_vtex_path = write_flat_replacement(
                    vtex_path, image_path, flat_image, role.name
                )
            cache[cache_key] = (new_vtex_path, avg_rgb)

        if not dry_run:
            new_ref = vtex_ref.replace(Path(vtex_ref).name, new_vtex_path.name)
            pending_replacements.append((span_start, span_end, new_ref))

        manifest.add(
            MaterialResult(
                rel_vmat,
                role.name,
                key,
                vtex_path.as_posix(),
                new_vtex_path.as_posix(),
                avg_rgb,
                "dry-run" if dry_run else "flattened",
            )
        )

    for start, end, replacement in sorted(pending_replacements, reverse=True):
        text = text[:start] + replacement + text[end:]

    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "content_root",
        type=Path,
        help="Folder containing your decompiled .vmat/.vtex/texture files.",
    )
    parser.add_argument(
        "--flatten-roughness",
        action="store_true",
        help="Also flatten roughness textures to their average value.",
    )
    parser.add_argument(
        "--flatten-ao",
        action="store_true",
        help="Also flatten ambient-occlusion textures to flat white.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing any files.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Where to write the JSON report (default: <content_root>/flatten_report.json).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    content_root: Path = args.content_root.resolve()
    if not content_root.is_dir():
        LOGGER.error("%s is not a directory", content_root)
        return 1

    roles = DEFAULT_ROLES
    if args.flatten_roughness:
        roles += (OPTIONAL_ROLES["roughness"],)
    if args.flatten_ao:
        roles += (OPTIONAL_ROLES["ao"],)

    vmat_files = find_vmat_files(content_root)
    if not vmat_files:
        LOGGER.error("No .vmat files found under %s", content_root)
        return 1
    LOGGER.info("Found %d material file(s)", len(vmat_files))

    manifest = Manifest()
    cache: dict[tuple[Path, str], tuple[Path, tuple[int, int, int]]] = {}

    for vmat_path in vmat_files:
        new_text = process_vmat(vmat_path, content_root, roles, cache, manifest, args.dry_run)
        if not args.dry_run and new_text != vmat_path.read_text(encoding="utf-8", errors="ignore"):
            vmat_path.write_text(new_text, encoding="utf-8")

    report_path = args.report or (content_root / "flatten_report.json")
    manifest.write(report_path)

    flattened = sum(1 for r in manifest.results if r.status in ("flattened", "dry-run"))
    already_flat = sum(1 for r in manifest.results if r.status == "already-flat")
    skipped = sum(1 for r in manifest.results if r.status == "skipped")
    errored = sum(1 for r in manifest.results if r.status == "error")
    LOGGER.info(
        "Done: %d texture slot(s) flattened, %d already flat, %d skipped, "
        "%d errored. Report: %s",
        flattened,
        already_flat,
        skipped,
        errored,
        report_path,
    )
    if skipped or errored:
        LOGGER.info(
            "Skipped/errored slots need a look -- open the report and check "
            "those .vmat/.vtex files by hand."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
