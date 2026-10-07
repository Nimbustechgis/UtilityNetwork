import arcpy
import os
import re

# --- CONFIGURA ESTO ---
layer_names = ["Primario", "Secundario"]
out_folder = r"C:\Temp\ExportShape"
# ----------------------

arcpy.env.overwriteOutput = True

aprx = arcpy.mp.ArcGISProject("CURRENT")
m = aprx.activeMap

if not os.path.exists(out_folder):
    os.makedirs(out_folder)


def clean_field_name(name):
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not name[0].isalpha():
        name = "F_" + name
    return name[:10].upper()


def get_all_layers(container):
    result = []
    for lyr in container.listLayers():
        result.append(lyr)
        if lyr.isGroupLayer:
            result.extend(get_all_layers(lyr))
    return result


def is_previous_export(lyr):
    try:
        desc = arcpy.Describe(lyr)
        catalog_path = getattr(desc, "catalogPath", "")
        return catalog_path.lower().startswith(out_folder.lower())
    except Exception:
        return False


def find_toc_layer(name):
    candidates = []

    for lyr in get_all_layers(m):
        if lyr.name.lower() != name.lower():
            continue

        if lyr.isGroupLayer:
            continue

        if is_previous_export(lyr):
            continue

        try:
            desc = arcpy.Describe(lyr)
            if hasattr(desc, "shapeType"):
                candidates.append(lyr)
        except Exception:
            pass

    if not candidates:
        return None

    # Preferir capas dentro de un grupo, como:
    # Conductor Distribución Eléctrica\Primario
    for lyr in candidates:
        if "\\" in lyr.longName:
            return lyr

    return candidates[0]


def unique_field_name(name, used_names):
    base = clean_field_name(name)
    candidate = base
    i = 1

    while candidate in used_names:
        suffix = str(i)
        candidate = base[:10 - len(suffix)] + suffix
        i += 1

    used_names.add(candidate)
    return candidate


def add_output_field(out_shp, out_field, source_field):
    if source_field.type == "String":
        arcpy.management.AddField(out_shp, out_field, "TEXT", field_length=min(source_field.length or 254, 254))
    elif source_field.type == "SmallInteger":
        arcpy.management.AddField(out_shp, out_field, "SHORT")
    elif source_field.type == "Integer":
        arcpy.management.AddField(out_shp, out_field, "LONG")
    elif source_field.type == "BigInteger":
        arcpy.management.AddField(out_shp, out_field, "TEXT", field_length=30)
    elif source_field.type == "Single":
        arcpy.management.AddField(out_shp, out_field, "FLOAT")
    elif source_field.type == "Double":
        arcpy.management.AddField(out_shp, out_field, "DOUBLE")
    elif source_field.type == "Date":
        arcpy.management.AddField(out_shp, out_field, "DATE")
    elif source_field.type in ("GUID", "GlobalID"):
        arcpy.management.AddField(out_shp, out_field, "TEXT", field_length=38)
    else:
        arcpy.management.AddField(out_shp, out_field, "TEXT", field_length=254)


def null_value_for_type(field_type):
    if field_type in ("String", "BigInteger", "GUID", "GlobalID"):
        return ""
    if field_type in ("SmallInteger", "Integer", "Single", "Double"):
        return 0
    if field_type == "Date":
        return None
    return ""


def get_subtype_where_clause(source_fc, subtype_name):
    subtypes = arcpy.da.ListSubtypes(source_fc)

    subtype_field = None
    for code, info in subtypes.items():
        subtype_field = info.get("SubtypeField")
        if subtype_field:
            break

    if not subtype_field:
        return ""

    for code, info in subtypes.items():
        if info["Name"].lower() == subtype_name.lower():
            field_sql = arcpy.AddFieldDelimiters(source_fc, subtype_field)
            return f"{field_sql} = {code}"

    return ""


def export_toc_subtype_layer_to_shape(lyr):
    desc_layer = arcpy.Describe(lyr)
    source_fc = desc_layer.catalogPath

    desc = arcpy.Describe(source_fc)

    where_clause = get_subtype_where_clause(source_fc, lyr.name)

    out_name = clean_field_name(lyr.name) + ".shp"
    out_shp = os.path.join(out_folder, out_name)

    if arcpy.Exists(out_shp):
        arcpy.management.Delete(out_shp)

    arcpy.management.CreateFeatureclass(
        out_path=out_folder,
        out_name=out_name,
        geometry_type=desc.shapeType.upper(),
        spatial_reference=desc.spatialReference
    )

    input_fields = arcpy.ListFields(source_fc)
    field_types = {f.name: f.type for f in input_fields}

    used_names = set()
    field_map = {}

    skip_types = {"OID", "Geometry", "Blob", "Raster"}

    oid_field = desc.OIDFieldName
    old_oid_field = unique_field_name("OLD_OID", used_names)
    arcpy.management.AddField(out_shp, old_oid_field, "DOUBLE")
    field_map[oid_field] = old_oid_field

    for f in input_fields:
        if f.type in skip_types:
            continue

        out_field = unique_field_name(f.name, used_names)
        add_output_field(out_shp, out_field, f)
        field_map[f.name] = out_field

    source_fields = ["SHAPE@"] + list(field_map.keys())
    target_fields = ["SHAPE@"] + list(field_map.values())

    count = 0

    print(f"Procesando: {lyr.longName}")
    print(f"Origen real: {source_fc}")
    print(f"Filtro subtipo: {where_clause if where_clause else 'Sin filtro detectado'}")

    with arcpy.da.SearchCursor(source_fc, source_fields, where_clause) as s_cur:
        with arcpy.da.InsertCursor(out_shp, target_fields) as i_cur:
            for row in s_cur:
                new_row = list(row)

                for idx, src_name in enumerate(source_fields):
                    if src_name == "SHAPE@":
                        continue

                    field_type = field_types.get(src_name)

                    if new_row[idx] is None:
                        new_row[idx] = null_value_for_type(field_type)
                    elif field_type in ("BigInteger", "GUID", "GlobalID"):
                        new_row[idx] = str(new_row[idx])

                i_cur.insertRow(new_row)
                count += 1

    print(f"Exportado: {out_shp} | Registros: {count}")


for name in layer_names:
    lyr = find_toc_layer(name)

    if lyr is None:
        print(f"No se encontro una subcapa original en la TOC para: {name}")
        continue

    export_toc_subtype_layer_to_shape(lyr)

print("Proceso terminado.")