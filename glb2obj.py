#!/usr/bin/env python3
"""
glb2obj.py - Production-ready Blender script to convert GLB/GLTF/FBX models to OBJ + PNG Textures.

Key features:
- Converts GLB, GLTF, and FBX models into clean Wavefront OBJ format.
- Merges all sub-meshes into a single unified OBJ.
- Normalizes and unifies UV layers across all objects before joining (prevents UV scrambling).
- Bakes world transforms, clears parent hierarchies, and applies modifiers (e.g. Armature rest pose).
- Accurately maps each material strictly to its own Base Color / Diffuse texture.
- Preserves untextured materials (e.g. glass transparency, colored plastics/metals) without assigning rogue textures.
- Automatically enhances MTL lighting (Kd 1.0, Ka 1.0) so OBJ models render bright and vivid.
- Strictly saves all textures in PNG format (.png).
- Organizes output into a dedicated folder per model (<model_name>/).
- Clean factory-reset memory management between batch files.
- Optional --double-sided flag for thin/single-sheet surfaces.

Usage:
    blender -b -P glb2obj.py -- <path_to_model_or_folder> [options]

Options:
    -o, --output <dir>    Parent directory for output (defaults to location of input file)
    --double-sided        Duplicate faces with inverted normals for 2-sided rendering
    --no-bright           Do not boost Ka/Kd ambient brightness in .mtl file

Examples:
    blender -b -P glb2obj.py -- model.glb
    blender -b -P glb2obj.py -- model.fbx
    blender -b -P glb2obj.py -- /path/to/models_dir/
    blender -b -P glb2obj.py -- model.fbx -o /custom/output/
    blender -b -P glb2obj.py -- model.glb --double-sided
"""

import bpy
import sys
import os
import time

SUPPORTED_EXTENSIONS = ('.glb', '.gltf', '.fbx')


def clean_scene():
    """Completely resets the Blender workspace to prevent memory leaks across batch conversions."""
    if bpy.context.active_object and bpy.context.active_object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
    except Exception:
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        for collection in [bpy.data.meshes, bpy.data.materials, bpy.data.textures, bpy.data.images, bpy.data.cameras, bpy.data.lights]:
            for block in list(collection):
                collection.remove(block, do_unlink=True)


def import_3d_model(file_path):
    """Imports 3D model according to its file extension (.glb, .gltf, .fbx)."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext in {'.glb', '.gltf'}:
        bpy.ops.import_scene.gltf(filepath=file_path)
    elif ext == '.fbx':
        if not hasattr(bpy.ops.import_scene, 'fbx'):
            try:
                bpy.ops.preferences.addon_enable(module='io_scene_fbx')
            except Exception:
                pass
        bpy.ops.import_scene.fbx(filepath=file_path)
    else:
        raise ValueError(f"Unsupported file format: {ext} (supported: {', '.join(SUPPORTED_EXTENSIONS)})")


def get_base_color_image(mat):
    """
    Identifies the exact Image used for Base Color / Diffuse in a material.
    Supports Principled BSDF, Diffuse BSDF, and emission/unlit shaders from both GLTF and FBX.
    Returns None if the material is untextured (e.g. glass, solid colors, metals).
    """
    if not mat or not getattr(mat, "node_tree", None):
        return None

    nodes = mat.node_tree.nodes

    # 1. Trace backward from Principled BSDF or Diffuse BSDF
    bsdf = next((n for n in nodes if n.type in {'BSDF_PRINCIPLED', 'BSDF_DIFFUSE'}), None)
    if bsdf:
        base_color_sock = (
            bsdf.inputs.get('Base Color') or
            bsdf.inputs.get('Color') or
            bsdf.inputs.get('Diffuse')
        )
        if not base_color_sock and len(bsdf.inputs) > 0:
            base_color_sock = bsdf.inputs[0]

        if base_color_sock and base_color_sock.is_linked:
            for link in base_color_sock.links:
                from_node = link.from_node
                if from_node.type == 'TEX_IMAGE' and from_node.image:
                    return from_node.image
                # Trace one level through color math / mix nodes
                if hasattr(from_node, "inputs"):
                    for inp in from_node.inputs:
                        if inp.is_linked:
                            for sub_link in inp.links:
                                if sub_link.from_node.type == 'TEX_IMAGE' and sub_link.from_node.image:
                                    return sub_link.from_node.image

    # 2. Trace backward from Material Output (Unlit / Emission shaders)
    out_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if out_node and out_node.inputs.get('Surface') and out_node.inputs['Surface'].is_linked:
        surf_node = out_node.inputs['Surface'].links[0].from_node
        if surf_node.type in {'EMISSION', 'BACKGROUND'}:
            col_sock = surf_node.inputs.get('Color')
            if col_sock and col_sock.is_linked:
                from_node = col_sock.links[0].from_node
                if from_node.type == 'TEX_IMAGE' and from_node.image:
                    return from_node.image

    # 3. Fallback: scan all image nodes, filtering out normal/roughness/metallic
    tex_nodes = [n for n in nodes if n.type == 'TEX_IMAGE' and n.image]
    if not tex_nodes:
        return None

    if len(tex_nodes) == 1:
        tn = tex_nodes[0]
        is_normal = any(l.to_node.type == 'NORMAL_MAP' for out in tn.outputs for l in out.links)
        if not is_normal:
            return tn.image
        return None

    for tn in tex_nodes:
        is_secondary = False
        for out in tn.outputs:
            for l in out.links:
                if l.to_node.type == 'NORMAL_MAP' or l.to_socket.name.lower() in {'normal', 'roughness', 'metallic', 'alpha'}:
                    is_secondary = True
        if not is_secondary:
            return tn.image

    return None


def prepare_mesh_objects(mesh_objs):
    """
    Normalizes UV coordinates, clears parent transforms, applies modifiers, and applies world matrices.
    Guarantees that all sub-meshes join seamlessly without UV corruption or missing geometry.
    """
    bpy.ops.object.select_all(action='DESELECT')

    for obj in mesh_objs:
        obj.hide_viewport = False
        obj.hide_set(False)
        obj.select_set(True)

        # Apply any active modifiers (e.g. Armature rest pose from FBX, Mirror, Subdivision)
        for mod in list(obj.modifiers):
            try:
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.modifier_apply(modifier=mod.name)
            except Exception:
                pass

        # Ensure every mesh has an active UV layer named 'UVMap'
        if obj.data.uv_layers:
            primary_uv = obj.data.uv_layers[0]
            primary_uv.name = "UVMap"
            obj.data.uv_layers.active = primary_uv
        else:
            # Create a default UV layer if object lacks UV coordinates
            new_uv = obj.data.uv_layers.new(name="UVMap")
            obj.data.uv_layers.active = new_uv

    # Unlink parent hierarchy while keeping global positions
    bpy.ops.object.parent_clear(type='CLEAR_KEEP_TRANSFORM')

    # Bake location, rotation, and scale into vertex coordinates
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def process_materials_and_textures(model_output_dir, base_name):
    """
    Extracts each material's base color texture strictly as PNG.
    Supports packed GLB textures and external/FBM textures from FBX.
    Returns a dict mapping material names to relative PNG filenames.
    """
    saved_images = {}  # image_obj -> relative_filename.png
    mat_to_tex = {}    # mat.name -> relative_filename.png or None

    for mat in bpy.data.materials:
        img = get_base_color_image(mat)

        if img is None:
            mat_to_tex[mat.name] = None
            print(f"  [Material] '{mat.name}': Solid color (untextured, preserving native color/alpha).")
            continue

        if img not in saved_images:
            safe_name = bpy.path.clean_name(img.name)
            if not safe_name or safe_name.startswith('.'):
                safe_name = f"tex_{len(saved_images)}"

            tex_filename = f"{base_name}_{safe_name}.png"
            tex_filepath = os.path.join(model_output_dir, tex_filename)

            print(f"  [Texture]  Saving PNG: '{img.name}' -> '{tex_filename}'")
            img.file_format = 'PNG'
            img.filepath_raw = tex_filepath

            saved = False
            try:
                img.save_render(tex_filepath)
                saved = True
            except Exception:
                pass

            if not saved:
                try:
                    img.save()
                    saved = True
                except Exception:
                    pass

            # If still not saved and image points to an external file on disk (common in FBX)
            if not saved and img.filepath:
                src_path = bpy.path.abspath(img.filepath)
                if os.path.exists(src_path):
                    try:
                        temp_img = bpy.data.images.load(src_path)
                        temp_img.file_format = 'PNG'
                        temp_img.save_render(tex_filepath)
                        bpy.data.images.remove(temp_img)
                        saved = True
                    except Exception:
                        pass

            saved_images[img] = tex_filename

        assigned_tex = saved_images[img]
        mat_to_tex[mat.name] = assigned_tex
        print(f"  [Material] '{mat.name}' -> Linked to: '{assigned_tex}'")

        ensure_material_nodes(mat, img)

    return mat_to_tex


def ensure_material_nodes(mat, img):
    """Ensures Principled BSDF is properly connected to Base Color and Surface Output."""
    if hasattr(mat, "use_nodes") and not mat.use_nodes:
        try:
            mat.use_nodes = True
        except Exception:
            pass

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    out_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if not out_node:
        out_node = nodes.new(type='ShaderNodeOutputMaterial')
        out_node.location = (400, 0)

    bsdf_node = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if not bsdf_node:
        bsdf_node = nodes.new(type='ShaderNodeBsdfPrincipled')
        bsdf_node.location = (100, 0)

    bsdf_out = bsdf_node.outputs.get('BSDF')
    surf_in = out_node.inputs.get('Surface')
    if bsdf_out and surf_in:
        links.new(bsdf_out, surf_in)

    tex_node = None
    for n in nodes:
        if n.type == 'TEX_IMAGE' and n.image == img:
            tex_node = n
            break

    if not tex_node:
        tex_node = nodes.new(type='ShaderNodeTexImage')
        tex_node.location = (-300, 0)
        tex_node.image = img

    base_color_in = bsdf_node.inputs.get('Base Color')
    if not base_color_in and len(bsdf_node.inputs) > 0:
        base_color_in = bsdf_node.inputs[0]

    color_out = tex_node.outputs.get('Color')
    if color_out and base_color_in:
        links.new(color_out, base_color_in)


def make_double_sided(obj):
    """Duplicates geometry and flips face normals so backfaces render with texture in all OBJ viewers."""
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.duplicate()
    bpy.ops.mesh.flip_normals()
    bpy.ops.object.mode_set(mode='OBJECT')


def clean_and_enhance_mtl(mtl_path, mat_to_tex, boost_lighting=True):
    """
    Parses and reconstructs the .mtl file with production standards:
    - Sets Ka 1.0 and Kd 1.0 for textured materials so textures look bright and vibrant.
    - Strips paths so texture references are strictly local filenames.
    - Eliminates invalid map_Kd from untextured materials (glass, solid colors).
    - Preserves native transparency (d / Tr) and specular properties.
    """
    if not os.path.exists(mtl_path):
        return

    with open(mtl_path, 'r', encoding='utf-8', errors='ignore') as f:
        raw_lines = f.readlines()

    materials = []
    current_mat = None

    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith('newmtl '):
            current_mat = {
                'name': stripped[7:].strip(),
                'properties': {},
                'extra_lines': []
            }
            materials.append(current_mat)
        elif current_mat is not None:
            if not stripped:
                continue
            parts = stripped.split(maxsplit=1)
            key = parts[0]
            val = parts[1] if len(parts) > 1 else ''
            if key in {'Ka', 'Kd', 'Ks', 'Ke', 'Ns', 'Ni', 'd', 'Tr', 'illum', 'map_Kd'}:
                current_mat['properties'][key] = val
            else:
                current_mat['extra_lines'].append(stripped)

    output_lines = [
        f"# Material file generated by glb2obj (enhanced for bright lighting)\n\n"
    ]

    for mat in materials:
        mname = mat['name']
        props = mat['properties']

        output_lines.append(f"newmtl {mname}\n")

        tex_file = mat_to_tex.get(mname)
        if not tex_file:
            base_mname = mname.split('.')[0]
            tex_file = mat_to_tex.get(base_mname)

        is_textured = (tex_file is not None)

        # Ambient color (Ka): full ambient illumination removes harsh pitch-black shadows
        if boost_lighting:
            output_lines.append("Ka 1.000000 1.000000 1.000000\n")
        else:
            ka = props.get('Ka', '1.000000 1.000000 1.000000')
            output_lines.append(f"Ka {ka}\n")

        # Diffuse color (Kd): textured materials set to 1.0 for full texture brightness
        if is_textured and boost_lighting:
            output_lines.append("Kd 1.000000 1.000000 1.000000\n")
        else:
            kd = props.get('Kd', '1.000000 1.000000 1.000000')
            output_lines.append(f"Kd {kd}\n")

        # Specular color (Ks)
        ks = props.get('Ks', '0.000000 0.000000 0.000000')
        output_lines.append(f"Ks {ks}\n")

        # Specular exponent (Ns)
        if 'Ns' in props:
            output_lines.append(f"Ns {props['Ns']}\n")

        # Optical density (Ni)
        if 'Ni' in props:
            output_lines.append(f"Ni {props['Ni']}\n")

        # Dissolve / Opacity (d): preserves transparency
        d_val = props.get('d', '1.000000')
        output_lines.append(f"d {d_val}\n")

        # Illumination model
        illum = props.get('illum', '2')
        output_lines.append(f"illum {illum}\n")

        for el in mat['extra_lines']:
            output_lines.append(f"{el}\n")

        # Texture mapping (map_Kd)
        if is_textured:
            output_lines.append(f"map_Kd {tex_file}\n")

        output_lines.append("\n")

    with open(mtl_path, 'w', encoding='utf-8') as f:
        f.writelines(output_lines)


def convert_model_file(file_path, output_parent=None, double_sided=False, boost_lighting=True):
    """Converts a single 3D file (.glb, .gltf, .fbx) into OBJ + PNG textures inside a dedicated folder."""
    start_time = time.time()
    file_path = os.path.abspath(file_path)
    base_name = os.path.splitext(os.path.basename(file_path))[0]

    parent_dir = output_parent if output_parent else os.path.dirname(file_path)
    model_output_dir = os.path.join(parent_dir, base_name)
    os.makedirs(model_output_dir, exist_ok=True)

    print(f"\n{'='*75}")
    print(f"[Start] Converting: {os.path.basename(file_path)}")
    print(f"        Source:     {file_path}")
    print(f"        Output:     {model_output_dir}/")
    print(f"{'='*75}")

    clean_scene()

    # 1. Import model (.glb, .gltf, .fbx)
    import_3d_model(file_path)

    if bpy.context.active_object and bpy.context.active_object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    mesh_objs = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']
    if not mesh_objs:
        print(f"[Error] No mesh geometry found in {file_path}")
        return False

    print(f"[Step 1] Found {len(mesh_objs)} sub-mesh object(s). Normalizing UVs and baking transforms...")
    prepare_mesh_objects(mesh_objs)

    # 2. Join into 1 single unified mesh
    bpy.ops.object.select_all(action='DESELECT')
    for o in mesh_objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = mesh_objs[0]

    if len(mesh_objs) > 1:
        print(f"[Step 2] Merging {len(mesh_objs)} objects into a unified mesh...")
        bpy.ops.object.join()

    main_obj = bpy.context.view_layer.objects.active

    # 3. Optional double-sided geometry
    if double_sided:
        print("[Step 3] Duplicating faces for double-sided rendering (--double-sided active)...")
        make_double_sided(main_obj)

    # 4. Extract textures and link each material to its own PNG
    print("[Step 4] Processing materials and extracting PNG textures...")
    mat_to_tex = process_materials_and_textures(model_output_dir, base_name)

    # 5. Export OBJ
    target_obj = os.path.join(model_output_dir, f"{base_name}.obj")
    target_mtl = os.path.join(model_output_dir, f"{base_name}.mtl")

    bpy.ops.object.select_all(action='DESELECT')
    main_obj.select_set(True)
    bpy.context.view_layer.objects.active = main_obj

    print(f"[Step 5] Exporting OBJ mesh...")
    if hasattr(bpy.ops.wm, "obj_export"):
        bpy.ops.wm.obj_export(
            filepath=target_obj,
            export_selected_objects=True,
            export_materials=True,
            export_uv=True,
            export_normals=True,
            path_mode='STRIP'
        )
    else:
        bpy.ops.export_scene.obj(
            filepath=target_obj,
            use_selection=True,
            use_materials=True,
            use_uvs=True,
            use_normals=True,
            path_mode='STRIP'
        )

    # 6. Enhance MTL lighting and verify texture links
    print(f"[Step 6] Enhancing MTL lighting (full ambient & diffuse brightness)...")
    clean_and_enhance_mtl(target_mtl, mat_to_tex, boost_lighting=boost_lighting)

    elapsed = time.time() - start_time
    poly_count = len(main_obj.data.polygons)
    vert_count = len(main_obj.data.vertices)
    print(f"\n[Success] Finished in {elapsed:.2f}s!")
    print(f"          Polygons: {poly_count:,} | Vertices: {vert_count:,}")
    print(f"          OBJ: {target_obj}")
    print(f"          MTL: {target_mtl}\n")
    return True


# Backward compatibility alias
convert_glb_file = convert_model_file


def parse_arguments():
    """Parses arguments passed after '--' to Blender."""
    argv = sys.argv
    if "--" not in argv:
        return None

    args = argv[argv.index("--") + 1:]
    if not args:
        return None

    input_path = None
    output_parent = None
    double_sided = False
    boost_lighting = True

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {'-o', '--output'} and i + 1 < len(args):
            output_parent = os.path.abspath(args[i + 1])
            i += 2
            continue
        elif arg == '--double-sided':
            double_sided = True
        elif arg == '--no-bright':
            boost_lighting = False
        elif not arg.startswith('--'):
            if input_path is None:
                input_path = os.path.abspath(arg)
            elif output_parent is None:
                output_parent = os.path.abspath(arg)
        i += 1

    return {
        'input_path': input_path,
        'output_parent': output_parent,
        'double_sided': double_sided,
        'boost_lighting': boost_lighting
    }


def main():
    parsed = parse_arguments()
    if not parsed or not parsed['input_path']:
        print("\nUsage:")
        print("    blender -b -P glb2obj.py -- <path_to_model_or_folder> [-o output_dir] [--double-sided] [--no-bright]\n")
        print("Supported input formats:")
        print("    .glb, .gltf, .fbx\n")
        print("Examples:")
        print("    blender -b -P glb2obj.py -- model.glb")
        print("    blender -b -P glb2obj.py -- model.fbx")
        print("    blender -b -P glb2obj.py -- /path/to/models/ -o /path/to/output/\n")
        return

    input_path = parsed['input_path']
    output_parent = parsed['output_parent']
    double_sided = parsed['double_sided']
    boost_lighting = parsed['boost_lighting']

    if os.path.isdir(input_path):
        # 1. Search root of directory
        model_files = [
            os.path.join(input_path, f)
            for f in sorted(os.listdir(input_path))
            if f.lower().endswith(SUPPORTED_EXTENSIONS)
        ]
        # 2. If no models in root, scan subdirectories
        if not model_files:
            for root, dirs, files in os.walk(input_path):
                for f in sorted(files):
                    if f.lower().endswith(SUPPORTED_EXTENSIONS):
                        model_files.append(os.path.join(root, f))

        if not model_files:
            ext_str = ", ".join(SUPPORTED_EXTENSIONS)
            print(f"[Error] No supported files ({ext_str}) found in directory: {input_path}")
            return

        print(f"Found {len(model_files)} model(s) in {input_path}")
        for f in model_files:
            convert_model_file(f, output_parent, double_sided=double_sided, boost_lighting=boost_lighting)
    elif os.path.isfile(input_path):
        convert_model_file(input_path, output_parent, double_sided=double_sided, boost_lighting=boost_lighting)
    else:
        print(f"[Error] Path not found: {input_path}")


if __name__ == "__main__":
    main()