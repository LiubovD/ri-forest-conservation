# Builds a statewide forest fragmentation model from NLCD forest cover,
# road impact, core forest, and patch connectivity metrics.

import os
import warnings
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
import matplotlib.pyplot as plt

from rasterio.mask import mask
from rasterio.features import shapes
from shapely.geometry import shape
from scipy.ndimage import label, distance_transform_edt

warnings.filterwarnings("ignore")
os.makedirs("outputs", exist_ok=True)

RI_BOUNDARY = "data/ri_boundary.gpkg"
NLCD = "data/ri_nlcd_landcover.tif"
ROADS = "data/ri_roads.gpkg"
PRIVATE_FORESTS = "data/private_forest_polygons.gpkg"  # optional

target_crs = "EPSG:3438"

# -----------------------------
# Load data
# -----------------------------

ri = gpd.read_file(RI_BOUNDARY)
roads = gpd.read_file(ROADS)

private_exists = os.path.exists(PRIVATE_FORESTS)

if private_exists:
    private_forests = gpd.read_file(PRIVATE_FORESTS)

src = rasterio.open(NLCD)

ri = ri.to_crs(src.crs)
roads = roads.to_crs(src.crs)

if private_exists:
    private_forests = private_forests.to_crs(src.crs)

# -----------------------------
# Clip NLCD to RI
# -----------------------------

geoms = [geom for geom in ri.geometry]

clipped, transform = mask(src, geoms, crop=True)
landcover = clipped[0]

# -----------------------------
# Extract forest
# NLCD forest classes:
# 41 = deciduous forest
# 42 = evergreen forest
# 43 = mixed forest
# -----------------------------

forest_classes = [41, 42, 43]
forest_binary = np.isin(landcover, forest_classes).astype(np.uint8)

profile = src.profile.copy()
profile.update(
    {
        "height": forest_binary.shape[0],
        "width": forest_binary.shape[1],
        "transform": transform,
        "count": 1,
        "dtype": "uint8",
        "driver": "GTiff",
    }
)

with rasterio.open("outputs/ri_forest_binary.tif", "w", **profile) as dst:
    dst.write(forest_binary, 1)

# -----------------------------
# Forest patch labeling
# -----------------------------

structure = np.ones((3, 3), dtype=np.int32)

labeled_forest, num_patches = label(
    forest_binary,
    structure=structure,
)

print(f"Total forest patches: {num_patches}")

pixel_area = abs(transform.a * transform.e)

patch_ids, patch_counts = np.unique(
    labeled_forest,
    return_counts=True,
)

patch_table = pd.DataFrame(
    {
        "patch_id": patch_ids,
        "pixel_count": patch_counts,
    }
)

patch_table = patch_table[patch_table["patch_id"] != 0]
patch_table["area_m2"] = patch_table["pixel_count"] * pixel_area
patch_table["area_ha"] = patch_table["area_m2"] / 10_000

# -----------------------------
# Core forest
# -----------------------------

edge_distance_m = 100
pixel_size = abs(transform.a)
edge_pixels = max(1, int(edge_distance_m / pixel_size))

distance_to_edge = distance_transform_edt(forest_binary)

core_forest = (distance_to_edge >= edge_pixels).astype(np.uint8)

with rasterio.open("outputs/ri_core_forest.tif", "w", **profile) as dst:
    dst.write(core_forest, 1)

# -----------------------------
# Vectorize forest patches
# -----------------------------

records = []

for geom, val in shapes(
    labeled_forest.astype(np.int32),
    mask=labeled_forest > 0,
    transform=transform,
):
    records.append(
        {
            "geometry": shape(geom),
            "patch_id": int(val),
        }
    )

forest_patches = gpd.GeoDataFrame(records, crs=src.crs)
forest_patches = forest_patches.merge(patch_table, on="patch_id")

# Use metric CRS for area, distance, buffer calculations

forest_patches = forest_patches.to_crs(target_crs)
roads = roads.to_crs(target_crs)
ri = ri.to_crs(target_crs)

if private_exists:
    private_forests = private_forests.to_crs(target_crs)

# -----------------------------
# Road impact
# -----------------------------

road_buffer_distance = 100

roads_buffered = roads.copy()
roads_buffered["geometry"] = roads_buffered.geometry.buffer(
    road_buffer_distance
)

roads_union = roads_buffered.union_all()

forest_patches["perimeter_m"] = forest_patches.geometry.length

forest_patches["compactness"] = (
    4
    * np.pi
    * forest_patches.geometry.area
    / (forest_patches["perimeter_m"] ** 2)
)

forest_patches["compactness"] = forest_patches["compactness"].clip(0, 1)

forest_patches["road_overlap_m2"] = forest_patches.geometry.intersection(
    roads_union
).area

forest_patches["road_impact_pct"] = (
    forest_patches["road_overlap_m2"] / forest_patches.geometry.area
)

# -----------------------------
# Core area per patch
# -----------------------------

core_records = []

for geom, val in shapes(
    core_forest.astype(np.int32),
    mask=core_forest > 0,
    transform=transform,
):
    core_records.append(
        {
            "geometry": shape(geom),
            "core": 1,
        }
    )

core_gdf = gpd.GeoDataFrame(core_records, crs=src.crs).to_crs(target_crs)

forest_patches["core_area_m2"] = 0.0

for idx, row in forest_patches.iterrows():
    possible = core_gdf[core_gdf.intersects(row.geometry)]

    if len(possible) > 0:
        forest_patches.loc[idx, "core_area_m2"] = possible.intersection(
            row.geometry
        ).area.sum()

forest_patches["core_area_ha"] = forest_patches["core_area_m2"] / 10_000

# -----------------------------
# Connectivity:
# nearby forest patches within 250 m
# -----------------------------

connectivity_distance_m = 250

forest_patches["nearby_patch_count"] = 0

spatial_index = forest_patches.sindex

for idx, row in forest_patches.iterrows():
    buffer_geom = row.geometry.buffer(connectivity_distance_m)

    candidate_idx = list(spatial_index.intersection(buffer_geom.bounds))
    candidates = forest_patches.iloc[candidate_idx]

    nearby = candidates[candidates.intersects(buffer_geom)]

    forest_patches.loc[idx, "nearby_patch_count"] = max(
        len(nearby) - 1,
        0,
    )

# -----------------------------
# Normalize scores
# -----------------------------

def normalize_high_good(series):
    if series.max() == series.min():
        return pd.Series(1, index=series.index)

    return (series - series.min()) / (series.max() - series.min())


def normalize_low_good(series):
    if series.max() == series.min():
        return pd.Series(1, index=series.index)

    return 1 - ((series - series.min()) / (series.max() - series.min()))


forest_patches["area_score"] = normalize_high_good(
    forest_patches["area_ha"]
)

forest_patches["core_score"] = normalize_high_good(
    forest_patches["core_area_ha"]
)

forest_patches["compactness_score"] = normalize_high_good(
    forest_patches["compactness"]
)

forest_patches["road_score"] = normalize_low_good(
    forest_patches["road_impact_pct"]
)

forest_patches["connectivity_score"] = normalize_high_good(
    forest_patches["nearby_patch_count"]
)

# -----------------------------
# Conservation score
# -----------------------------

forest_patches["conservation_score"] = (
    0.35 * forest_patches["core_score"]
    + 0.25 * forest_patches["area_score"]
    + 0.15 * forest_patches["connectivity_score"]
    + 0.15 * forest_patches["road_score"]
    + 0.10 * forest_patches["compactness_score"]
)

forest_patches["priority_class"] = pd.qcut(
    forest_patches["conservation_score"],
    q=4,
    labels=["Low", "Medium", "High", "Very High"],
    duplicates="drop",
)

forest_patches = forest_patches.sort_values(
    "conservation_score",
    ascending=False,
)

forest_patches["statewide_rank"] = range(
    1,
    len(forest_patches) + 1,
)

# -----------------------------
# Export statewide results
# -----------------------------

forest_patches.to_file(
    "outputs/ri_statewide_forest_priority.gpkg",
    layer="forest_priority",
    driver="GPKG",
)

forest_patches.drop(columns="geometry").to_csv(
    "outputs/ri_statewide_forest_priority.csv",
    index=False,
)

# -----------------------------
# Optional private forest clip
# -----------------------------

if private_exists:
    private_forests["private_id"] = private_forests.index + 1

    private_priority = gpd.overlay(
        private_forests,
        forest_patches,
        how="intersection",
    )

    private_priority["intersect_area_m2"] = private_priority.geometry.area

    summary = (
        private_priority.groupby("private_id")
        .apply(
            lambda x: pd.Series(
                {
                    "mean_conservation_score": np.average(
                        x["conservation_score"],
                        weights=x["intersect_area_m2"],
                    ),
                    "forest_area_ha": x["intersect_area_m2"].sum()
                    / 10_000,
                    "best_statewide_rank": x["statewide_rank"].min(),
                }
            )
        )
        .reset_index()
    )

    private_forests = private_forests.merge(
        summary,
        on="private_id",
        how="left",
    )

    private_forests["private_priority_class"] = pd.qcut(
        private_forests["mean_conservation_score"],
        q=4,
        labels=["Low", "Medium", "High", "Very High"],
        duplicates="drop",
    )

    private_forests.to_file(
        "outputs/private_forest_priority.gpkg",
        layer="private_priority",
        driver="GPKG",
    )

    private_forests.drop(columns="geometry").to_csv(
        "outputs/private_forest_priority.csv",
        index=False,
    )

# -----------------------------
# Map
# -----------------------------

fig, ax = plt.subplots(figsize=(12, 12))

forest_patches.plot(
    column="priority_class",
    ax=ax,
    legend=True,
    linewidth=0,
)

roads.plot(
    ax=ax,
    linewidth=0.15,
)

ri.boundary.plot(
    ax=ax,
    linewidth=0.8,
)

ax.set_title("Rhode Island Forest Conservation Priority")
ax.axis("off")

plt.savefig(
    "outputs/ri_conservation_priority_map.png",
    dpi=300,
    bbox_inches="tight",
)

# -----------------------------
# Print summary
# -----------------------------

print("Saved outputs:")
print("outputs/ri_forest_binary.tif")
print("outputs/ri_core_forest.tif")
print("outputs/ri_statewide_forest_priority.gpkg")
print("outputs/ri_statewide_forest_priority.csv")
print("outputs/ri_conservation_priority_map.png")

print("\nTop 20 forest patches:")
print(
    forest_patches[
        [
            "patch_id",
            "statewide_rank",
            "area_ha",
            "core_area_ha",
            "road_impact_pct",
            "nearby_patch_count",
            "conservation_score",
            "priority_class",
        ]
    ].head(20)
)