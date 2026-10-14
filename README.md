# mod-visible-bags

Tools that put Blizzard's newer backpack cloaks on 3.3.5a characters. They download the
retail models, convert them to the 3.3.5a model format, and build the client patch: hidden aura
spells whose spell visual attaches a backpack to the character's back. Everyone with the patch
sees everyone's backpack, with no client hacks.

The server side (who owns which backpack, the Backpacks tab, hiding the cloak) lives in
[wow-mod-transmog-plus](https://github.com/buildthehomelab/wow-mod-transmog-plus). This repo is
only the client patch toolchain.

## Backpacks

| Spell | Backpack | Retail model |
|---|---|---|
| 90170 | Tuskarr Trader's Pack | cape_special_tuskarr_b_02 |
| 90171 | Tuskarr Backpack | cape_special_tuskarr_b_03 |
| 90172 | Fisherman's Pack | cape_special_tuskarr_b_01 |
| 90173 | Climber's Pack | cape_special_climbingpack_b_01 |
| 90174 | Kaldorei Backpack | misc_nightelfbackpack02 |
| 90175 | Dragonscale Backpack (swinging lantern) | backpack_wrathion_c_01 |
| 90176 | Radiant Traveler's Backpack | cape_special_arathor_d_01_purple |
| 90177 | Halfhill Farmer's Backpack (animated chicken, swinging pan, basket and tassel) | cape_special_chickenbackpack_c_01 |
| 90178 | Backpack Appearance: the use spell of the intro item 27621 | |

## How it works

- `fetch_retail.py` downloads each model, its first .skin and its textures from
  [wago.tools](https://wago.tools). Nothing from Blizzard is stored in this repo.
- `m2.py` reads retail (MD21, v274) and 3.3.5 (MD20, v264) models.
- `m2conv.py` rewrites a model as v264 plus `00.skin`:
  - It keeps the skin-0 mesh and one diffuse texture per batch. Retail env/glow passes have no 3.3.5 shader and are dropped.
  - Static packs get one bone.
  - `animate=True` keeps the retail bones and lays the Stand variations end to end on a global loop.
  - `sway` adds pendulum swings to bones that retail moves with physics.
  - `parts` splits a loose piece (a mesh island inside a box) off a one-piece model onto its own swinging bone.
- `build_patch.py` clones the stock "Plaguethrower Backpack" chain into new rows:
  - Spell 71589, SpellVisual 15137 and kit 13991.
  - Plus a SpellVisualEffectName and a SpellVisualKitModelAttach row on the Back attachment (12).
  - It also adds the intro item's icon: Item.dbc 27621 and ItemDisplayInfo 68743.
  - It writes the MPQ and the matching `spell_dbc` SQL.

### 3.3.5 quirks it handles

- `sequenceIdxHashById` is a hash with quadratic probing. The client probes until it finds the
  animation or an empty (0xFFFF) slot, so a full table hangs it the moment a spell visual asks
  for an animation the model lacks. The converter writes 8 slots.
- An attached spell model's own animation time doesn't advance, but global loops always do.
  That's why moving parts use one global loop.
- The client only animates bones flagged 0x200 (or 0x80).

## Requirements

- Python 3.10+ (standard library only).
- [StormLib](https://github.com/ladislav-zezula/StormLib) as a shared library. The default is
  `/usr/local/lib/libstorm.dylib`; point `STORMLIB` at yours.
- The client patch that already ships your Spell.dbc and Item.dbc (its copies are the base;
  the examples call it `patch-P.MPQ`), and the stock `SpellVisual.dbc`, `SpellVisualKit.dbc`,
  `SpellVisualEffectName.dbc`, `SpellVisualKitModelAttach.dbc` and `ItemDisplayInfo.dbc`
  (from your server's `env/dist/data/dbc`).

## Usage

```bash
python3 tools/fetch_retail.py retail-models
python3 tools/build_patch.py --patch-p patch-P.MPQ --expect-patch-p <sha prefix> \
    --stock-dbc dbc --models retail-models --full --out patch-P.MPQ --sql spell_dbc.sql
```

The patch letters are examples; use the names your client loads.

- `--full` copies patch-P's other files, so the output replaces patch-P itself.
- Without `--full` you get a test patch. Save it as a later letter (for example `patch-V.MPQ`) on your own client only. It carries its own copy of Spell.dbc, so it hides any newer patch-P until you remove it.
- `--expect-patch-p` refuses a different base.
- `--diag` adds the placement probes (90186/90187) and the freeze diagnostics (90180-90183).

Tuning lives at the top of `build_patch.py`:
- `PACKS`: the height offset baked into each mesh.
- `SWAY`: degrees, swings per loop, phase, axis, and the droop that hangs a part on its hook.
- `PARTS`: which pieces of a one-piece model swing.

## Credits

- Models and textures: Blizzard Entertainment, downloaded from wago.tools.
- File IDs: [wowdev/wow-listfile](https://github.com/wowdev/wow-listfile).
- Client behaviour checked against [whoa](https://github.com/thunderbrewhq/whoa), a 3.3.5a
  client reimplementation, and [pywowlib](https://github.com/wowdev/pywowlib).

## License

MIT for the tools in this repo. The models and textures they download belong to Blizzard
Entertainment.
