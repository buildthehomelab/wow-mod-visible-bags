#!/usr/bin/env python3
"""Download retail backpack models (M2 + skin 0 + textures) from wago.tools."""
import json, os, struct, sys, urllib.request

CASC = "https://wago.tools/api/casc/{}"

# name: (model fdid, item texture fdid or None)
PACKS = {
    "tuskarr_traders_pack": (4571491, 4571788),
    "tuskarr_backpack":     (4571492, 4571784),
    "fishermans_pack":      (4571490, 4571792),
    "climbers_pack":        (4561104, None),
    "kaldorei_backpack":    (5489256, None),
    "dragonscale_backpack": (3166251, 3180240),
    "radiant_backpack":     (5364434, 6322052),
    "halfhill_backpack":    (5753459, 5753461),
}

# Textures the item system fills in that aren't in TXID (retail texture type 3 on the chicken).
EXTRA_TEXTURES = {
    "halfhill_backpack": {2: 5753462},  # cape/chicken2_white.blp
}

def get(fdid, path):
    if not os.path.exists(path):
        req = urllib.request.Request(CASC.format(fdid), headers={"User-Agent": "mod-visible-bags/1.0"})
        with urllib.request.urlopen(req) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(data)
    return open(path, "rb").read()

def chunks(data):
    out, o = {}, 0
    while o + 8 <= len(data):
        tag, size = struct.unpack_from("<4sI", data, o)
        out[tag.decode()] = data[o + 8:o + 8 + size]
        o += 8 + size
    return out

def main(out):
    os.makedirs(out, exist_ok=True)
    manifest = {}
    for name, (model, itex) in PACKS.items():
        d = os.path.join(out, name)
        os.makedirs(d, exist_ok=True)
        c = chunks(get(model, os.path.join(d, "model.m2")))
        sfid = struct.unpack("<%dI" % (len(c["SFID"]) // 4), c["SFID"])
        get(sfid[0], os.path.join(d, "model00.skin"))
        txid = list(struct.unpack("<%dI" % (len(c.get("TXID", b"")) // 4), c.get("TXID", b"")))
        tex = {}
        for i, f in enumerate(txid):
            if f:
                get(f, os.path.join(d, "tex%d.blp" % i))
                tex[i] = f
        if itex:
            get(itex, os.path.join(d, "item.blp"))
        for i, f in EXTRA_TEXTURES.get(name, {}).items():
            get(f, os.path.join(d, "tex%d.blp" % i))
        manifest[name] = {"model": model, "skin": sfid[0], "txid": txid, "item_tex": itex}
        print(name, manifest[name])
    json.dump(manifest, open(os.path.join(out, "manifest.json"), "w"), indent=1)

if __name__ == "__main__":
    main(sys.argv[1])
