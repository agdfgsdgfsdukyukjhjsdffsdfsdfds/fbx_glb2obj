# glb2obj

A headless Blender script to convert **GLB**, **glTF**, and **FBX** 3D models into Wavefront `.obj` format with properly mapped `.png` textures.

Works with both single-mesh assets and complex multi-part, multi-material models.

---

## Features

- **Multi-Format Input**: Seamlessly converts `.glb`, `.gltf`, and `.fbx` files.
- **Accurate Per-Material Texture Mapping**: Each material is strictly mapped to its own diffuse / base-color texture. Untextured materials (solid colors, transparent elements, metals) preserve their native appearance without incorrectly inheriting textures from other parts of the mesh.
- **Enhanced Lighting in OBJ**: Automatically optimizes ambient (`Ka 1.0`) and diffuse (`Kd 1.0`) coefficients in `.mtl` so textures render at full brightness without dark, muddy shadows in standard OBJ viewers.
- **UV Layer Synchronization**: Unifies UV channel names across all sub-meshes before merging, preventing UV map corruption on multi-part models.
- **Hierarchy & Transform Baking**: Unlinks nested parent hierarchies, bakes modifiers (e.g. Armature rest poses from FBX), and bakes world transforms directly into vertex coordinates.
- **Strictly PNG Textures**: All textures (embedded or external) are extracted and saved as `.png` images with clean relative paths in `.mtl`.
- **Dedicated Output Folder**: Each model is saved into its own directory named after the file (`<model_name>/`).
- **Memory-Safe Batch Processing**: Resets scene data between files to prevent memory leaks during large batch conversions of folders.
- **Optional Double-Sided Geometry**: Optional `--double-sided` flag to duplicate and flip face normals for thin/single-sheet surfaces.

---

## Output Structure

Converting `model.glb` or `model.fbx` generates a folder named `model/`:

```text
model/
├── model.obj              # Unified 3D mesh
├── model.mtl              # Material definitions
├── model_texture_0.png    # Material texture
└── model_texture_1.png    # Additional material textures (if present)
```

---

## Requirements

- **Blender** (fully tested on Blender 5.2.1).
- No additional Python packages required.

---

## Usage

Run Blender in background mode (`-b`) with the script (`-P glb2obj.py`):

### 1. Convert a single model (.glb or .fbx)

```bash
# Convert GLB:
blender -b -P glb2obj.py -- /path/to/model.glb

# Convert FBX:
blender -b -P glb2obj.py -- /path/to/model.fbx
```

The output folder will be created alongside the source file at `/path/to/model/`.

### 2. Batch convert an entire folder

Scans the folder for all `.glb`, `.gltf`, and `.fbx` models and converts each into its own subfolder:

```bash
blender -b -P glb2obj.py -- /path/to/models_directory/
```

### 3. Specify custom output directory

```bash
blender -b -P glb2obj.py -- /path/to/model.fbx -o /path/to/output_dir/
```

### 4. Enable double-sided geometry

For models with single-sided surfaces where reverse faces should be visible:

```bash
blender -b -P glb2obj.py -- /path/to/model.glb --double-sided
```

### 5. Disable lighting boost

If you prefer standard default shading without ambient/diffuse boost:

```bash
blender -b -P glb2obj.py -- /path/to/model.fbx --no-bright
```
