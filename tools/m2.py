"""Minimal M2 (retail MD21 v272+ and classic MD20 v264) reader for static props."""
import struct

HEADER = [  # (name, kind) after magic/version; 'a' = M2Array, 'u' = uint32
    ("name", "a"), ("global_flags", "u"), ("global_loops", "a"), ("sequences", "a"),
    ("sequence_lookup", "a"), ("bones", "a"), ("key_bone_lookup", "a"), ("vertices", "a"),
    ("num_skins", "u"), ("colors", "a"), ("textures", "a"), ("texture_weights", "a"),
    ("texture_transforms", "a"), ("replaceable_lookup", "a"), ("materials", "a"),
    ("bone_lookup", "a"), ("texture_lookup", "a"), ("texunit_lookup", "a"),
    ("weight_lookup", "a"), ("transform_lookup", "a"),
]

def chunks(data):
    out, o = {}, 0
    while o + 8 <= len(data):
        tag, size = struct.unpack_from("<4sI", data, o)
        out[tag.decode()] = data[o + 8:o + 8 + size]
        o += 8 + size
    return out

class M2:
    def __init__(self, data):
        if data[:4] == b"MD21":
            self.chunks = chunks(data)
            self.md = self.chunks["MD21"]
        else:
            self.chunks = {}
            self.md = data
        self.version = struct.unpack_from("<I", self.md, 4)[0]
        o, h = 8, {}
        for name, kind in HEADER:
            if kind == "a":
                h[name] = struct.unpack_from("<II", self.md, o); o += 8
            else:
                h[name] = struct.unpack_from("<I", self.md, o)[0]; o += 4
        h["bbox"] = struct.unpack_from("<6f", self.md, o); o += 24
        h["bradius"] = struct.unpack_from("<f", self.md, o)[0]; o += 4
        self.h = h
        self.txid = self._u32s("TXID")

    def _u32s(self, tag):
        c = self.chunks.get(tag, b"")
        return list(struct.unpack("<%dI" % (len(c) // 4), c))

    def arr(self, name, fmt):
        n, off = self.h[name]
        size = struct.calcsize(fmt)
        return [struct.unpack_from(fmt, self.md, off + i * size) for i in range(n)]

    def vertices(self):
        # pos3f, boneWeights4B, boneIndices4B, normal3f, uv2f, uv2f
        return self.arr("vertices", "<3f4B4B3f2f2f")

    def textures(self):
        return self.arr("textures", "<IIII")  # type, flags, name count, name offset

    def materials(self):
        return self.arr("materials", "<HH")  # flags, blend mode

    def lookup(self, name):
        return [x[0] for x in self.arr(name, "<h")]

class Skin:
    def __init__(self, data):
        assert data[:4] == b"SKIN", data[:4]
        (self.vert_a, self.idx_a, self.bone_a, self.sub_a, self.batch_a) = [
            struct.unpack_from("<II", data, 4 + i * 8) for i in range(5)]
        self.bone_count_max = struct.unpack_from("<I", data, 44)[0]
        self.data = data

    def _arr(self, a, fmt):
        n, off = a
        size = struct.calcsize(fmt)
        return [struct.unpack_from(fmt, self.data, off + i * size) for i in range(n)]

    def vertex_map(self):
        return [x[0] for x in self._arr(self.vert_a, "<H")]

    def indices(self):
        return [x[0] for x in self._arr(self.idx_a, "<H")]

    def bones(self):
        return self._arr(self.bone_a, "<4B")

    def submeshes(self):
        # id, level, vstart, vcount, istart, icount, bonecount, bonecombo, influences, centerbone, center3f, sortcenter3f, radius
        return self._arr(self.sub_a, "<HHHHHHHHHH3f3ff")

    def batches(self):
        # flags, priority, shader, submesh, geosetIndex, color, material, materialLayer, texCount, texCombo, texCoordCombo, weightCombo, transformCombo
        return self._arr(self.batch_a, "<BbHHHhHHHHHHH")
