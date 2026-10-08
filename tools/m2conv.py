"""Convert a retail (MD21, v272+) static prop M2 into a 3.3.5a (MD20, v264) M2 + 00.skin.

Only what a rigid backpack needs survives: the skin-0 mesh, one diffuse texture per batch and
the batch materials. Bones, animations, particles, ribbons and second texture layers (retail
env/reflect maps) are dropped; every vertex is bound to one static bone in its rest pose.
"""
import math
import struct

from m2 import M2, Skin

HEADER_SIZE = 0x130
SEQ_FLAG_IN_M2 = 0x20  # sequence data lives in the .m2, not an .anim file


class Blob:
    def __init__(self):
        self.data = bytearray(HEADER_SIZE)

    def add(self, payload, count):
        """Append payload (16-byte aligned) and return the M2Array (count, offset)."""
        if not count:
            return (0, 0)
        while len(self.data) % 16:
            self.data.append(0)
        off = len(self.data)
        self.data += payload
        return (count, off)


SEQUENCE_HASH_SIZE = 8


def sequence_hash(anim_ids):
    """3.3.5's sequenceIdxHashById: an open-addressed hash (slot = id % size, quadratic probing)
    of sequence indexes, 0xFFFF = empty. The client probes until it finds the id or an empty
    slot, so the table must keep empty slots: a full one hangs it on any missing animation
    (spell kits ask attached models for more than Stand)."""
    table = [0xFFFF] * SEQUENCE_HASH_SIZE
    assert len(anim_ids) < SEQUENCE_HASH_SIZE
    for seq_index, anim_id in enumerate(anim_ids):
        slot, step = anim_id % SEQUENCE_HASH_SIZE, 1
        while table[slot] != 0xFFFF:
            slot = (slot + step * step) % SEQUENCE_HASH_SIZE
            step += 1
        table[slot] = seq_index
    return struct.pack("<%dH" % SEQUENCE_HASH_SIZE, *table)


def empty_track():
    return struct.pack("<HhIIII", 0, -1, 0, 0, 0, 0)


def bounds(points):
    xs, ys, zs = zip(*points)
    lo = (min(xs), min(ys), min(zs))
    hi = (max(xs), max(ys), max(zs))
    radius = max(math.sqrt(x * x + y * y + z * z) for x, y, z in points)
    return lo + hi, radius


def convert(retail_m2, retail_skin, texture_for, name, dz=0.0):
    """texture_for(retail_texture_index, retail_texture) -> new texture path or None.

    Returns (m2 bytes, skin bytes, [texture paths in M2 order]).
    """
    m = M2(retail_m2)
    sk = Skin(retail_skin)
    verts = m.vertices()
    vmap = sk.vertex_map()
    tex_lookup = m.lookup("texture_lookup")
    retail_textures = m.textures()
    retail_materials = m.materials()

    # Vertices: only those skin 0 uses, in skin order, so the skin's local indices stay valid.
    out_verts = []
    points = []
    for gi in vmap:
        v = verts[gi]
        pos = (v[0], v[1], v[2] + dz)
        points.append(pos)
        out_verts.append(struct.pack("<3f4B4B3f2f2f", *pos, 255, 0, 0, 0, 0, 0, 0, 0,
                                     v[11], v[12], v[13], v[14], v[15], v[16], v[17]))

    # Batches -> one diffuse texture + one material each.
    textures, tex_flags, materials, batches = [], [], [], []
    for b in sk.batches():
        flags, prio, shader, sub, geoset, color, mat, layer, count, combo, *_ = b
        if layer != 0 or retail_materials[mat][1] >= 2:
            continue  # retail glow/env passes have no 3.3.5 shader; keep opaque + alpha-key only
        ti = tex_lookup[combo]
        path = texture_for(ti, retail_textures[ti])
        if path is None:
            continue
        if path not in textures:
            textures.append(path)
            tex_flags.append(retail_textures[ti][1] & 0x3)  # wrap X/Y as retail (Climber's Pack clamps)
        mflags, blend = retail_materials[mat]
        material = (mflags & 0x1F, blend if blend <= 6 else 1)
        if material not in materials:
            materials.append(material)
        batches.append((sub, textures.index(path), materials.index(material), prio))

    indices = sk.indices()
    if len(out_verts) > 0xFFFF or len(indices) > 0xFFFF:
        raise ValueError("%s: too big for 16-bit skin indexes" % name)
    subs = []
    for x in sk.submeshes():
        sid, level, vstart, vcount, istart, icount = x[:6]
        if level:  # 3.3.5 reads skinSectionId + Level as one uint32 and has no index-start overflow
            raise ValueError("%s: submesh Level %d not supported" % (name, level))
        cx, cy, cz, sx, sy, sz, r = x[10:]
        subs.append((sid, level, vstart, vcount, istart, icount, (cx, cy, cz + dz), (sx, sy, sz + dz), r))
    if not batches:
        raise ValueError("%s: no drawable batch left" % name)
    m2 = write_model(name, out_verts, points, textures, materials, tex_flags)
    return m2, build_skin(len(out_verts), indices, subs, batches), textures


def vertex(pos, normal, uv):
    return struct.pack("<3f4B4B3f2f2f", *pos, 255, 0, 0, 0, 0, 0, 0, 0, *normal, *uv, 0.0, 0.0)


def quad(name, texture, size=0.3):
    """A flat square facing -x (behind the attachment): the smallest model the client can draw."""
    s = size
    corners = [(0.0, -s, -s), (0.0, s, -s), (0.0, s, s), (0.0, -s, s)]
    uvs = [(0, 1), (1, 1), (1, 0), (0, 0)]
    verts = [vertex(c, (-1.0, 0.0, 0.0), uv) for c, uv in zip(corners, uvs)]
    indices = [0, 1, 2, 0, 2, 3, 0, 2, 1, 0, 3, 2]  # both sides
    subs = [(0, 0, 0, 4, 0, len(indices), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), s * 1.5)]
    m2 = write_model(name, verts, corners, [texture], [(0, 0)])
    return m2, build_skin(4, indices, subs, [(0, 0, 0, 0)])


def write_model(name, out_verts, points, textures, materials, tex_flags=None):
    box, radius = bounds(points)
    blob = Blob()
    h = {}
    nm = (name + "\0").encode()
    h["name"] = blob.add(nm, len(nm))

    seq = struct.pack("<HHIfIhHIII6ffhH", 0, 0, 3333, 0.0, SEQ_FLAG_IN_M2, 0x7FFF, 0, 0, 0, 150,
                      *box, radius, -1, 0)
    h["sequences"] = blob.add(seq, 1)
    h["sequence_lookup"] = blob.add(sequence_hash([0]), SEQUENCE_HASH_SIZE)
    bone = struct.pack("<iIhHI", -1, 0, -1, 0, 0) + empty_track() * 3 + struct.pack("<3f", 0, 0, 0)
    h["bones"] = blob.add(bone, 1)
    h["key_bone_lookup"] = (0, 0)
    h["vertices"] = blob.add(b"".join(out_verts), len(out_verts))

    # Textures: type 0 (hardcoded file name).
    tex_names = []
    for path in textures:
        s = (path + "\0").encode()
        tex_names.append(blob.add(s, len(s)))
    tex_flags = tex_flags or [3] * len(textures)
    tex_rows = b"".join(struct.pack("<IIII", 0, f, *n) for f, n in zip(tex_flags, tex_names))
    h["textures"] = blob.add(tex_rows, len(textures))

    # One static texture weight (fully opaque).
    ts = blob.add(struct.pack("<I", 0), 1)
    vs = blob.add(struct.pack("<h", 0x7FFF), 1)
    ts_outer = blob.add(struct.pack("<II", *ts), 1)
    vs_outer = blob.add(struct.pack("<II", *vs), 1)
    weight = struct.pack("<HhIIII", 0, -1, *ts_outer, *vs_outer)
    h["texture_weights"] = blob.add(weight, 1)

    h["replaceable_lookup"] = blob.add(struct.pack("<h", -1), 1)
    h["materials"] = blob.add(b"".join(struct.pack("<HH", *x) for x in materials), len(materials))
    h["bone_lookup"] = blob.add(struct.pack("<h", 0), 1)
    h["texture_lookup"] = blob.add(struct.pack("<%dh" % len(textures), *range(len(textures))), len(textures))
    h["texunit_lookup"] = blob.add(struct.pack("<h", 0), 1)
    h["weight_lookup"] = blob.add(struct.pack("<h", 0), 1)
    h["transform_lookup"] = blob.add(struct.pack("<h", -1), 1)

    hdr = bytearray(b"MD20" + struct.pack("<I", 264))
    def arr(key):
        hdr.extend(struct.pack("<II", *h.get(key, (0, 0))))
    arr("name")
    hdr.extend(struct.pack("<I", 0))  # global flags
    for key in ("global_loops", "sequences", "sequence_lookup", "bones", "key_bone_lookup", "vertices"):
        arr(key)
    hdr.extend(struct.pack("<I", 1))  # one skin profile
    for key in ("colors", "textures", "texture_weights", "texture_transforms", "replaceable_lookup",
                "materials", "bone_lookup", "texture_lookup", "texunit_lookup", "weight_lookup",
                "transform_lookup"):
        arr(key)
    hdr.extend(struct.pack("<6ff6ff", *box, radius, *box, radius))
    hdr.extend(b"\0" * 8 * 11)  # collision (3), attachments, attachment lookup, events, lights,
                                # cameras, camera lookup, ribbons, particles
    assert len(hdr) == HEADER_SIZE, hex(len(hdr))
    blob.data[:HEADER_SIZE] = hdr

    return bytes(blob.data)


def build_skin(n, indices, subs, batches):
    out = bytearray(48)
    parts = []

    def add(payload, count):
        while (len(out)) % 16:
            out.append(0)
        off = len(out)
        out.extend(payload)
        return (count, off) if count else (0, 0)

    parts.append(add(struct.pack("<%dH" % n, *range(n)), n))
    parts.append(add(struct.pack("<%dH" % len(indices), *indices), len(indices)))
    parts.append(add(b"\0\0\0\0" * n, n))
    sub_rows = b""
    for sid, level, vstart, vcount, istart, icount, center, sort_center, r in subs:
        sub_rows += struct.pack("<HHHHHHHHHH3f3ff", sid, level, vstart, vcount, istart, icount,
                                1, 0, 1, 0, *center, *sort_center, r)
    parts.append(add(sub_rows, len(subs)))
    batch_rows = b""
    for sub, tex, mat, prio in batches:
        # flags 16 (static), shader 0 (single diffuse texture), color -1, uv set 0, weight 0, no transform
        batch_rows += struct.pack("<BbHHHhHHHHHHH", 16, prio, 0, sub, sub, -1, mat, 0, 1, tex, 0, 0, 0)
    parts.append(add(batch_rows, len(batches)))
    out[:48] = b"SKIN" + b"".join(struct.pack("<II", *p) for p in parts) + struct.pack("<I", 21)
    return bytes(out)
