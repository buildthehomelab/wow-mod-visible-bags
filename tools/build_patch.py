#!/usr/bin/env python3
"""Build the visible-backpacks client patch (and the server's spell_dbc SQL).

    python3 build_patch.py --patch-p patch-P.MPQ --stock-dbc DIR --models DIR --out patch-V.MPQ
    python3 build_patch.py ... --sql spell_dbc.sql

--patch-p    the live patch-P.MPQ; its Spell.dbc is the base, so the new patch can load after it
--stock-dbc  folder with the stock SpellVisual.dbc, SpellVisualKit.dbc, SpellVisualEffectName.dbc
             and SpellVisualKitModelAttach.dbc (patch-P doesn't carry these)
--models     output of fetch_retail.py

Each backpack is a hidden dummy aura (cloned from stock 71589 "Plaguethrower Backpack") whose
SpellVisual state kit attaches the converted model to the character's Back attachment (12).
"""
import argparse
import ctypes
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m2 import M2  # noqa: E402
from m2conv import convert, quad  # noqa: E402

TEMPLATE_SPELL = 71589      # Plaguethrower Backpack: hidden permanent dummy aura on self
TEMPLATE_VISUAL = 15137     # its SpellVisual (StateKit = 13991)
TEMPLATE_KIT = 13991

ATTACH_BACK = 12
ATTACH_CHEST = 34

MODEL_DIR = "Item\\ObjectComponents\\VisibleBags"

# spell id: (pack folder, file stem, spell name, dz baked into the mesh, attachment, offset xyz, yaw)
# dz lowers the smaller packs: on Wowhead's retail model only the Tuskarr Trader's Pack sat at the
# right height, so each pack's bottom is moved to the Trader's Pack bottom (z -0.55), never raised.
PACKS = {
    90170: ("tuskarr_traders_pack", "VB_TuskarrTradersPack", "Tuskarr Trader's Pack", 0.0),
    90171: ("tuskarr_backpack", "VB_TuskarrBackpack", "Tuskarr Backpack", -0.17),
    90172: ("fishermans_pack", "VB_FishermansPack", "Fisherman's Pack", -0.23),
    90173: ("climbers_pack", "VB_ClimbersPack", "Climber's Pack", 0.0),
    90174: ("kaldorei_backpack", "VB_KaldoreiBackpack", "Kaldorei Backpack", -0.28),
    90175: ("dragonscale_backpack", "VB_DragonscaleBackpack", "Dragonscale Backpack", -0.05),
    90176: ("radiant_backpack", "VB_RadiantBackpack", "Radiant Traveler's Backpack", -0.09),
    90177: ("halfhill_backpack", "VB_HalfhillBackpack", "Halfhill Farmer's Backpack", 0.0),
}
# Packs that keep their retail bones and Stand animations (the chicken's three idle loops).
ANIMATED = {"halfhill_backpack"}
# Hand-made pendulum swings for the parts retail moves with physics:
# bone: (degrees, swings per loop, phase, swing axis, droop degrees, droop axis). The loop is
# about 6 s. Droop tilts a part down on its hook: retail rests the pan and basket sticking out
# and lets physics gravity pull them down, so without a droop they float beside the pack.
SWAY = {
    "halfhill_backpack": {
        8: (7.0, 4, 0.00, (0.5, 1.0, 0.0), -40.0, (1.0, 0.0, 0.0)),   # frying pan (+y side)
        10: (5.0, 4, 0.35, (0.5, -1.0, 0.0), 25.0, (1.0, 0.0, 0.0)),  # egg basket (-y side); 40 sank it into the pack
        9: (12.0, 6, 0.15, (1.0, 0.3, 0.0)),                          # tassel
    },
}
# One-piece packs: mesh pieces inside box move onto a new swinging bone hung from their top.
PARTS = {
    "dragonscale_backpack": [
        # the glass lantern hanging low on the +y side
        # small swing + 6 degree outward tilt: retail already rests it 4 cm into the pack side
        dict(box=(-0.36, -0.15, 0.14, 0.34, -0.51, -0.21),
             sway=(6.0, 5, 0.0, (1.0, 0.4, 0.0), 6.0, (1.0, 0.0, 0.0))),
    ],
}

# Placement: spell id -> (attachment, (x, y, z), yaw). Probes 90186/90187 reuse the Tuskarr
# Backpack model to check the attachment's orientation in the 3.3.5 client.
PLACEMENT = {sid: (ATTACH_BACK, (0.0, 0.0, 0.0), 0.0) for sid in PACKS}
PROBES = {
    90186: (90171, "Tuskarr Backpack (probe: turned 180)", (ATTACH_BACK, (0.0, 0.0, 0.0), 3.14159265)),
    90187: (90171, "Tuskarr Backpack (probe: chest anchor)", (ATTACH_CHEST, (-0.25, 0.0, 0.0), 0.0)),
}

# Diagnostics for a client freeze: each swaps one part for a stock one.
STOCK_MODEL = "World\\Generic\\Ogre\\Passive Doodads\\OgreBackpacks\\OgreBackpack01.mdx"
STOCK_TEXTURE = "Textures\\ShaneCube.blp"
DIAG = {
    90180: "Diag: stock ogre backpack model",
    90181: "Diag: Tuskarr Backpack mesh, stock texture",
    90182: "Diag: test square, Tuskarr texture",
    90183: "Diag: test square, stock texture",
}

# New DBC ids: base + (spell - 90170).
VISUAL_BASE, KIT_BASE, EFFECT_BASE, ATTACH_BASE = 16800, 15600, 7200, 4800

SPELL_FIELDS = 234
F_DURATION, F_VISUAL, F_NAME, F_NAME_FLAGS = 40, 131, 136, 152
F_DESCRIPTION = 170
DURATION_INFINITE = 21

# --- DBC ---------------------------------------------------------------------------------------

def read_dbc(path):
    data = open(path, "rb").read()
    magic, count, fields, size, strsize = struct.unpack_from("<4s4I", data, 0)
    assert magic == b"WDBC" and size == fields * 4, path
    rows = [list(struct.unpack_from("<%dI" % fields, data, 20 + i * size)) for i in range(count)]
    strings = bytearray(data[20 + count * size:20 + count * size + strsize])
    return rows, strings, fields


def write_dbc(path, rows, strings, fields):
    rows = sorted(rows, key=lambda r: r[0])
    with open(path, "wb") as f:
        f.write(struct.pack("<4s4I", b"WDBC", len(rows), fields, fields * 4, len(strings)))
        for r in rows:
            f.write(struct.pack("<%dI" % fields, *r))
        f.write(strings)


def add_string(strings, text):
    at = len(strings)
    strings.extend(text.encode("utf-8") + b"\0")
    return at


def f32(v):
    return struct.unpack("<I", struct.pack("<f", v))[0]


def upsert(rows, row):
    rows[:] = [r for r in rows if r[0] != row[0]]
    rows.append(row)


def template(rows, row_id):
    row = next((r for r in rows if r[0] == row_id), None)
    if row is None:
        sys.exit("template row %d missing" % row_id)
    return list(row)

# --- MPQ ---------------------------------------------------------------------------------------

def stormlib():
    lib = ctypes.CDLL(os.environ.get("STORMLIB", "/usr/local/lib/libstorm.dylib"))
    h = ctypes.c_void_p
    lib.SFileOpenArchive.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(h)]
    lib.SFileCreateArchive.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(h)]
    lib.SFileExtractFile.argtypes = [h, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    lib.SFileAddFileEx.argtypes = [h, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
    lib.SFileCloseArchive.argtypes = [h]
    for fn in (lib.SFileOpenArchive, lib.SFileCreateArchive, lib.SFileExtractFile,
               lib.SFileAddFileEx, lib.SFileCloseArchive):
        fn.restype = ctypes.c_bool
    return lib, h


def extract(mpq, name, dst):
    lib, H = stormlib()
    h = H()
    if not lib.SFileOpenArchive(mpq.encode(), 0, 0x100, ctypes.byref(h)):
        sys.exit("can't open " + mpq)
    ok = lib.SFileExtractFile(h, name.encode(), dst.encode(), 0)
    lib.SFileCloseArchive(h)
    if not ok:
        sys.exit("can't extract %s from %s" % (name, mpq))


def pack(out, files):
    lib, H = stormlib()
    if os.path.exists(out):
        os.remove(out)
    h = H()
    # MPQ v1 with a listfile and attributes; each file zlib-compressed, like the realm's patches.
    if not lib.SFileCreateArchive(out.encode(), 0x00300000, max(16, len(files) * 2), ctypes.byref(h)):
        sys.exit("can't create " + out)
    for src, name in files:
        if not lib.SFileAddFileEx(h, src.encode(), name.encode(), 0x80000200, 0x02, 0x02):
            lib.SFileCloseArchive(h)
            os.remove(out)
            sys.exit("can't add " + name)
    if not lib.SFileCloseArchive(h):
        os.remove(out)
        sys.exit("can't finish " + out)

# --- models ------------------------------------------------------------------------------------

def convert_pack(models, folder, stem, dz, work, force_texture=None):
    d = os.path.join(models, folder)
    manifest = json.load(open(os.path.join(models, "manifest.json")))[folder]
    files = []

    def texture_for(index, tex):
        if force_texture:
            return force_texture
        tex_type = tex[0]
        src = os.path.join(d, "item.blp") if tex_type == 2 else os.path.join(d, "tex%d.blp" % index)
        if not os.path.exists(src):
            sys.exit("%s: texture %d (type %d) has no file %s" % (folder, index, tex_type, src))
        name = "%s\\%s_%d.blp" % (MODEL_DIR, stem, index)
        files.append((src, name))
        return name

    m2, skin, used = convert(open(os.path.join(d, "model.m2"), "rb").read(),
                             open(os.path.join(d, "model00.skin"), "rb").read(),
                             texture_for, stem, dz, animate=folder in ANIMATED and not force_texture,
                             sway=SWAY.get(folder), parts=None if force_texture else PARTS.get(folder))
    m2_path = os.path.join(work, stem + ".m2")
    skin_path = os.path.join(work, stem + "00.skin")
    open(m2_path, "wb").write(m2)
    open(skin_path, "wb").write(skin)
    files = [f for f in files if f[1] in used]
    files = list(dict(((n, (s, n)) for s, n in files)).values())
    files += [(m2_path, "%s\\%s.m2" % (MODEL_DIR, stem)), (skin_path, "%s\\%s00.skin" % (MODEL_DIR, stem))]
    check = M2(m2)
    print("  %-22s retail %d -> verts %d, textures %d, bbox z %.2f..%.2f" % (
        folder, manifest["model"], check.h["vertices"][0], len(used), check.h["bbox"][2], check.h["bbox"][5]))
    return files

# --- main --------------------------------------------------------------------------------------

def build(args):
    work = tempfile.mkdtemp(prefix="visible-bags-")
    try:
        _build(args, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _build(args, work):
    # patch-V carries a whole Spell.dbc, so it is only valid on top of this exact patch-P.
    base_sha = hashlib.sha256(open(args.patch_p, "rb").read()).hexdigest()
    print("patch-P base", base_sha[:8])
    if args.expect_patch_p and not base_sha.startswith(args.expect_patch_p):
        sys.exit("patch-P is %s, expected %s" % (base_sha[:8], args.expect_patch_p))
    spell_src = os.path.join(work, "Spell.src.dbc")
    extract(args.patch_p, "DBFilesClient\\Spell.dbc", spell_src)

    spells, spell_str, sf = read_dbc(spell_src)
    assert sf == SPELL_FIELDS
    visuals, visual_str, vf = read_dbc(os.path.join(args.stock_dbc, "SpellVisual.dbc"))
    kits, kit_str, kf = read_dbc(os.path.join(args.stock_dbc, "SpellVisualKit.dbc"))
    effects, effect_str, ef = read_dbc(os.path.join(args.stock_dbc, "SpellVisualEffectName.dbc"))
    attaches, attach_str, af = read_dbc(os.path.join(args.stock_dbc, "SpellVisualKitModelAttach.dbc"))

    for table, base in ((visuals, VISUAL_BASE), (kits, KIT_BASE), (effects, EFFECT_BASE), (attaches, ATTACH_BASE)):
        clash = [r[0] for r in table if base <= r[0] < base + 20]
        if clash:
            sys.exit("id clash in stock DBC: %s" % clash)

    files = []
    stems = {}
    print("models:")
    for sid, (folder, stem, _name, dz) in PACKS.items():
        files += convert_pack(args.models, folder, stem, dz, work)
        stems[sid] = stem

    def model_path(stem):
        return "%s\\%s.mdx" % (MODEL_DIR, stem)

    entries = {sid: (model_path(stems[sid]), PACKS[sid][2], PLACEMENT[sid]) for sid in PACKS}
    for sid, (model_of, name, placement) in PROBES.items():
        entries[sid] = (model_path(stems[model_of]), name, placement)

    if args.diag:
        back = (ATTACH_BACK, (0.0, 0.0, 0.0), 0.0)
        entries[90180] = (STOCK_MODEL, DIAG[90180], back)
        files += convert_pack(args.models, "tuskarr_backpack", "VB_DiagMeshStockTex", -0.17, work, STOCK_TEXTURE)
        entries[90181] = (model_path("VB_DiagMeshStockTex"), DIAG[90181], back)
        tusk_tex = "%s\\VB_TuskarrBackpack_0.blp" % MODEL_DIR
        for sid, stem, tex in ((90182, "VB_DiagSquare", tusk_tex), (90183, "VB_DiagSquareStockTex", STOCK_TEXTURE)):
            m2, skin = quad(stem, tex)
            for data, suffix in ((m2, ".m2"), (skin, "00.skin")):
                path = os.path.join(work, stem + suffix)
                open(path, "wb").write(data)
                files.append((path, "%s\\%s%s" % (MODEL_DIR, stem, suffix)))
            entries[sid] = (model_path(stem), DIAG[sid], back)

    new_spell_rows = []
    for sid, (model, name, (attachment, (x, y, z), yaw)) in sorted(entries.items()):
        i = sid - 90170
        effect = [EFFECT_BASE + i, add_string(effect_str, "Visible Bag: " + name),
                  add_string(effect_str, model),
                  f32(0.0), f32(1.0), f32(0.01), f32(100.0)]
        upsert(effects, effect)

        kit = template(kits, TEMPLATE_KIT)
        kit[0] = KIT_BASE + i
        upsert(kits, kit)

        upsert(attaches, [ATTACH_BASE + i, KIT_BASE + i, EFFECT_BASE + i, attachment,
                          f32(x), f32(y), f32(z), f32(yaw), f32(0.0), f32(0.0)])

        visual = template(visuals, TEMPLATE_VISUAL)
        visual[0] = VISUAL_BASE + i
        visual[4] = KIT_BASE + i  # StateKit
        upsert(visuals, visual)

        spell = template(spells, TEMPLATE_SPELL)
        spell[0] = sid
        spell[F_DURATION] = DURATION_INFINITE
        spell[F_VISUAL] = VISUAL_BASE + i
        spell[F_VISUAL + 1] = 0
        for loc in range(16):
            spell[F_NAME + loc] = 0
            spell[F_DESCRIPTION + loc] = 0
        spell[F_NAME] = add_string(spell_str, "Visible Bag: " + name)
        upsert(spells, spell)
        new_spell_rows.append((spell, "Visible Bag: " + name))

    if args.sql:
        write_sql(args.sql, new_spell_rows)
        print("wrote", args.sql)

    if args.out:
        out_dbc = []
        for fname, rows, strings, fields in (("Spell.dbc", spells, spell_str, sf),
                                             ("SpellVisual.dbc", visuals, visual_str, vf),
                                             ("SpellVisualKit.dbc", kits, kit_str, kf),
                                             ("SpellVisualEffectName.dbc", effects, effect_str, ef),
                                             ("SpellVisualKitModelAttach.dbc", attaches, attach_str, af)):
            p = os.path.join(work, fname)
            write_dbc(p, rows, strings, fields)
            out_dbc.append((p, "DBFilesClient\\" + fname))
        pack(args.out, out_dbc + files)
        print("wrote", args.out, "(%d files)" % len(out_dbc + files))

# --- SQL ---------------------------------------------------------------------------------------

STRING_FIELDS = set(range(136, 152)) | set(range(153, 169)) | set(range(170, 186)) | set(range(187, 203))
FLOAT_FIELDS = {47, 77, 78, 79, 101, 102, 103, 119, 120, 121, 216, 217, 218, 229, 230, 231}
# Plain `int` columns of AC's spell_dbc; every other integer column is `int unsigned`/`bigint unsigned`.
SIGNED_FIELDS = {0, 13, 15, 41, *range(52, 71), 74, 75, 76, 80, 81, 82, *range(110, 116), 224, 228}


def write_sql(path, rows):
    def value(i, v):
        if i in STRING_FIELDS:
            return "''"
        if i in FLOAT_FIELDS:
            return repr(round(struct.unpack("<f", struct.pack("<I", v))[0], 6))
        if i in SIGNED_FIELDS:
            return str(struct.unpack("<i", struct.pack("<I", v))[0])
        return str(v)

    lines = []
    for row, name in rows:
        vals = [value(i, v) for i, v in enumerate(row)]
        vals[F_NAME] = "'" + name.replace("'", "''") + "'"
        lines.append("(" + ", ".join(vals) + ")")
    ids = ", ".join(str(r[0][0]) for r in rows)
    with open(path, "w") as f:
        f.write("-- mod-visible-bags: hidden dummy-aura spells that show a backpack (client: patch Spell.dbc)\n")
        f.write("DELETE FROM `spell_dbc` WHERE `ID` IN (%s);\n" % ids)
        f.write("INSERT INTO `spell_dbc` VALUES\n" + ",\n".join(lines) + ";\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patch-p", required=True)
    ap.add_argument("--stock-dbc", required=True)
    ap.add_argument("--models", required=True)
    ap.add_argument("--out")
    ap.add_argument("--sql")
    ap.add_argument("--expect-patch-p", help="refuse to build unless patch-P's sha256 starts with this")
    ap.add_argument("--diag", action="store_true", help="add the freeze diagnostics 90180-90183")
    build(ap.parse_args())


if __name__ == "__main__":
    main()
