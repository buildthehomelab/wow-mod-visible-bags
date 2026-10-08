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
    assert len(set(anim_ids)) < SEQUENCE_HASH_SIZE
    seen = set()
    for seq_index, anim_id in enumerate(anim_ids):
        if anim_id in seen:
            continue  # a variation; the client reaches it through variationNext
        seen.add(anim_id)
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


def convert(retail_m2, retail_skin, texture_for, name, dz=0.0, animate=False, sway=None):
    """texture_for(retail_texture_index, retail_texture) -> new texture path or None.

    animate keeps the retail bones and the Stand (id 0) sequences with their keyframes, so a
    model with an idle loop (the Halfhill chicken) keeps moving; otherwise one static bone.
    sway = {bone: (degrees, swings per loop, phase 0..1, (axis x, y, z))} adds a pendulum swing
    to bones retail moved with physics (the 3.3.5 client has none).

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
        weights, bone_ids = (v[3:7], v[7:11]) if animate else ((255, 0, 0, 0), (0, 0, 0, 0))
        out_verts.append(struct.pack("<3f4B4B3f2f2f", *pos, *weights, *bone_ids,
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
    sub_bones = []
    for x in sk.submeshes():
        sid, level, vstart, vcount, istart, icount = x[:6]
        sub_bones.append(x[6:10])  # boneCount, boneComboIndex, boneInfluences, centerBoneIndex
        if level:  # 3.3.5 reads skinSectionId + Level as one uint32 and has no index-start overflow
            raise ValueError("%s: submesh Level %d not supported" % (name, level))
        cx, cy, cz, sx, sy, sz, r = x[10:]
        subs.append((sid, level, vstart, vcount, istart, icount, (cx, cy, cz + dz), (sx, sy, sz + dz), r))
    if not batches:
        raise ValueError("%s: no drawable batch left" % name)
    if not animate:
        m2 = write_model(name, out_verts, points, textures, materials, tex_flags)
        return m2, build_skin(len(out_verts), indices, subs, batches), textures

    rig = retail_rig(m, dz, sway or {})
    m2 = write_model(name, out_verts, points, textures, materials, tex_flags, rig)
    skin = build_skin(len(out_verts), indices, subs, batches,
                      vertex_bones=b"".join(struct.pack("<4B", *x) for x in sk.bones()),
                      sub_bones=sub_bones)
    return m2, skin, textures


TRACK = "<HhIIII"  # interpolation, global sequence, timestamps M2Array, values M2Array
BONE = "<iIhHI" + TRACK[1:] * 3 + "3f"
SEQUENCE = "<HHIfIhHIIHH6ffhH"  # retail (Legion+) layout: blendTimeIn/Out as two uint16
BONE_FLAGS_KEPT = 0x3FF  # inherit/billboard/transformed bits; retail 0x400 (physics) has no 3.3.5 meaning


IDENTITY = {12: struct.pack("<3f", 0, 0, 0), 8: struct.pack("<4h", 32767, 32767, 32767, -1)}
SCALE_ONE = struct.pack("<3f", 1, 1, 1)


BLEND_MS = 150  # retail blends 150 ms between variations; the loop gets that tail to wrap smoothly


def compress_quat(q):
    """Inverse of the client's M2CompQuat decode (u16 / 32767.5 - 1)."""
    return struct.pack("<4H", *(max(0, min(65535, int(round((c + 1.0) * 32767.5)))) for c in q))


def sway_track(total, degrees, swings, phase, axis, keys_per_swing=12):
    ax = [a / math.sqrt(sum(c * c for c in axis)) for a in axis]
    times, values = [], []
    n = swings * keys_per_swing
    for k in range(n):
        t = total * k // n
        angle = math.radians(degrees) * math.sin(2 * math.pi * (k / n * swings + phase))
        h = angle / 2
        values.append(compress_quat((ax[0] * math.sin(h), ax[1] * math.sin(h), ax[2] * math.sin(h), math.cos(h))))
        times.append(t)
    times.append(total - 1)  # close the loop on the first pose
    values.append(values[0])
    return (1, [(len(times), struct.pack("<%dI" % len(times), *times), b"".join(values))], 0)


def retail_rig(m, dz, sway):
    """Bones and keyframes of a retail model, ready for write_model.

    The retail Stand variations are laid end to end on one global loop, so the bones move
    whatever animation the client asks the attached model for (spell kits don't just play
    Stand, and global loops tick regardless). The model keeps a single static Stand sequence.
    """
    md = m.md
    seqs = m.arr("sequences", SEQUENCE)
    kept = [i for i, x in enumerate(seqs) if x[0] == 0 and x[2] > 0]  # Stand and its variations
    if not kept:
        raise ValueError("no Stand sequence with a duration")
    starts, total = [], 0
    for old in kept:
        starts.append(total)
        total += seqs[old][2]
    loop = total + BLEND_MS
    first = seqs[kept[0]]
    box = (first[11], first[12], first[13] + dz, first[14], first[15], first[16] + dz)
    sequences = [dict(id=0, variation=0, duration=loop, movespeed=0.0, frequency=0x7FFF,
                      replay=(0, 0), box=box, radius=first[17], var_next=-1, alias=0)]

    def track(fields, value_size, rest):
        interp, gseq, tsn, tso, vn, vo = fields
        if gseq != -1:
            return (0, [])  # already on a retail global loop we don't carry over; keep rest pose
        if interp not in (0, 1):
            raise ValueError("interpolation %d not supported" % interp)
        times, values = [], []
        for old, start in zip(kept, starts):
            dur = seqs[old][2]
            n, off = struct.unpack_from("<II", md, tso + 8 * old) if old < tsn else (0, 0)
            vn2, voff = struct.unpack_from("<II", md, vo + 8 * old) if old < vn else (0, 0)
            if vn2 != n:
                raise ValueError("track with %d times but %d values" % (n, vn2))
            keys = [(struct.unpack_from("<I", md, off + 4 * i)[0],
                     md[voff + value_size * i:voff + value_size * (i + 1)]) for i in range(n)]
            if not keys:
                keys = [(0, rest)]
            # Pin both ends of this variation's window so neighbours don't blend into it.
            if keys[0][0] > 0:
                keys.insert(0, (0, keys[0][1]))
            if keys[-1][0] < dur - 1:
                keys.append((dur - 1, keys[-1][1]))
            for t, v in keys:
                times.append(start + min(t, dur - 1))
                values.append(v)
        if all(v == rest for v in values):
            return (0, [])
        times.append(loop - 1)  # blend back into the first pose before the loop wraps
        values.append(values[0])
        return (interp, [(len(times), struct.pack("<%dI" % len(times), *times), b"".join(values))], 0)

    retail_bones = m.arr("bones", BONE)
    unknown = set(sway) - set(range(len(retail_bones)))
    if unknown:
        raise ValueError("sway for missing bones %s" % sorted(unknown))
    bones = []
    for i, b in enumerate(retail_bones):
        key, flags, parent, submesh, crc = b[:5]
        tracks = [track(b[5:11], 12, IDENTITY[12]), track(b[11:17], 8, IDENTITY[8]),
                  track(b[17:23], 12, SCALE_ONE)]
        flags &= BONE_FLAGS_KEPT
        if i in sway:
            if tracks[1][1]:
                raise ValueError("bone %d already rotates; won't add a sway" % i)
            tracks[1] = sway_track(loop, *sway[i])
            flags |= 0x200  # the client only animates bones flagged transformed (0x200) or 0x80
        bones.append(dict(flags=flags, parent=parent, submesh=submesh, tracks=tracks,
                          pivot=(b[23], b[24], b[25] + dz)))
    return dict(sequences=sequences, bones=bones, bone_lookup=m.lookup("bone_lookup"),
                global_loops=[loop])


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


def write_model(name, out_verts, points, textures, materials, tex_flags=None, rig=None):
    box, radius = bounds(points)
    blob = Blob()
    h = {}
    nm = (name + "\0").encode()
    h["name"] = blob.add(nm, len(nm))

    if rig is None:
        rig = dict(sequences=[dict(id=0, variation=0, duration=3333, movespeed=0.0, frequency=0x7FFF,
                                   replay=(0, 0), box=box, radius=radius, var_next=-1, alias=0)],
                   bones=[dict(flags=0, parent=-1, submesh=0, tracks=[(0, [])] * 3, pivot=(0.0, 0.0, 0.0))],
                   bone_lookup=[0])
    seq_rows = b"".join(
        struct.pack("<HHIfIhHIII6ffhH", x["id"], x["variation"], x["duration"], x["movespeed"],
                    SEQ_FLAG_IN_M2, x["frequency"], 0, x["replay"][0], x["replay"][1], 150,
                    *x["box"], x["radius"], x["var_next"], x["alias"])
        for x in rig["sequences"])
    h["sequences"] = blob.add(seq_rows, len(rig["sequences"]))
    h["sequence_lookup"] = blob.add(sequence_hash([x["id"] for x in rig["sequences"]]), SEQUENCE_HASH_SIZE)

    def write_track(interp, per_seq, global_loop=-1):
        if not per_seq:
            return empty_track()
        times = [blob.add(t, n) for n, t, _ in per_seq]
        values = [blob.add(v, n) for n, _, v in per_seq]
        ts = blob.add(b"".join(struct.pack("<II", *x) for x in times), len(per_seq))
        vs = blob.add(b"".join(struct.pack("<II", *x) for x in values), len(per_seq))
        return struct.pack(TRACK, interp, global_loop, *ts, *vs)

    bone_rows = b""
    for bone in rig["bones"]:
        tracks = b"".join(write_track(*t) for t in bone["tracks"])
        bone_rows += struct.pack("<iIhHI", -1, bone["flags"], bone["parent"], bone["submesh"], 0) \
            + tracks + struct.pack("<3f", *bone["pivot"])
    h["bones"] = blob.add(bone_rows, len(rig["bones"]))
    loops = rig.get("global_loops", [])
    h["global_loops"] = blob.add(struct.pack("<%dI" % len(loops), *loops), len(loops))
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
    h["bone_lookup"] = blob.add(struct.pack("<%dH" % len(rig["bone_lookup"]), *rig["bone_lookup"]),
                                len(rig["bone_lookup"]))
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


def build_skin(n, indices, subs, batches, vertex_bones=None, sub_bones=None):
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
    parts.append(add(vertex_bones or b"\0\0\0\0" * n, n))
    sub_rows = b""
    for i, (sid, level, vstart, vcount, istart, icount, center, sort_center, r) in enumerate(subs):
        bone_count, bone_combo, influences, center_bone = sub_bones[i] if sub_bones else (1, 0, 1, 0)
        sub_rows += struct.pack("<HHHHHHHHHH3f3ff", sid, level, vstart, vcount, istart, icount,
                                bone_count, bone_combo, influences, center_bone, *center, *sort_center, r)
    parts.append(add(sub_rows, len(subs)))
    batch_rows = b""
    for sub, tex, mat, prio in batches:
        # flags 16 (static), shader 0 (single diffuse texture), color -1, uv set 0, weight 0, no transform
        batch_rows += struct.pack("<BbHHHhHHHHHHH", 16, prio, 0, sub, sub, -1, mat, 0, 1, tex, 0, 0, 0)
    parts.append(add(batch_rows, len(batches)))
    out[:48] = b"SKIN" + b"".join(struct.pack("<II", *p) for p in parts) + struct.pack("<I", 21)
    return bytes(out)
