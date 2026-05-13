import argparse
import os
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd


def summarize_custom_polygons(
    polygon_path,
    statewide_patches_path,
    output_path,
    polygon_layer=None,
    statewide_layer="forest_priority",
    id_field=None,
):
    """
    Summarize statewide forest fragmentation metrics to custom polygons.

    Use case:
      - private forest patches
      - parcels
      - conservation target areas
      - municipal planning zones
      - user-provided candidate land polygons

    Inputs:
      polygon_path:
        Path to custom polygons. GeoPackage preferred, but shapefile,
        GeoJSON, or GDB layer can also work.

      statewide_patches_path:
        Output from the statewide fragmentation workflow:
        outputs/ri_statewide_forest_priority.gpkg

      output_path:
        GeoPackage output with fragmentation metrics joined to custom polygons.

    Output:
      A GeoPackage containing the original custom polygons plus summarized
      fragmentation metrics.
    """

    # ------------------------------------------------------------
    # Load custom polygons
    # ------------------------------------------------------------

    if polygon_layer:
        custom = gpd.read_file(polygon_path, layer=polygon_layer)
    else:
        custom = gpd.read_file(polygon_path)

    if custom.empty:
        raise ValueError("Custom polygon file is empty.")

    custom = custom[custom.geometry.notnull()].copy()
    custom = custom[~custom.geometry.is_empty].copy()
    custom["geometry"] = custom.geometry.buffer(0)

    # Create a stable ID if user did not provide one.
    if id_field and id_field in custom.columns:
        custom_id = id_field
    else:
        custom_id = "custom_id"
        custom[custom_id] = custom.index + 1

    # ------------------------------------------------------------
    # Load statewide fragmentation model output
    # ------------------------------------------------------------

    patches = gpd.read_file(
        statewide_patches_path,
        layer=statewide_layer,
    )

    if patches.empty:
        raise ValueError("Statewide forest patch layer is empty.")

    patches = patches[patches.geometry.notnull()].copy()
    patches = patches[~patches.geometry.is_empty].copy()
    patches["geometry"] = patches.geometry.buffer(0)

    # Match CRS
    custom = custom.to_crs(patches.crs)

    # Calculate original custom polygon area
    custom["custom_area_m2"] = custom.geometry.area
    custom["custom_area_ha"] = custom["custom_area_m2"] / 10_000

    # ------------------------------------------------------------
    # Intersect custom polygons with statewide forest patches
    # ------------------------------------------------------------

    joined = gpd.overlay(
        custom[[custom_id, "custom_area_m2", "custom_area_ha", "geometry"]],
        patches,
        how="intersection",
    )

    # If no custom polygons intersect forest patches, export polygons
    # with null/zero metrics so user still gets a valid output.
    if joined.empty:
        custom["forest_area_ha"] = 0
        custom["forest_cover_pct"] = 0
        custom["mean_fragmentation_score"] = np.nan
        custom["mean_road_impact_pct"] = np.nan
        custom["mean_core_area_ha"] = np.nan
        custom["best_statewide_rank"] = np.nan
        custom["dominant_priority_class"] = None

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        custom.to_file(
            output_path,
            layer="custom_polygon_fragmentation",
            driver="GPKG",
        )

        print("No intersections found with forest patches.")
        print(f"Saved: {output_path}")
        return

    joined["intersect_area_m2"] = joined.geometry.area
    joined["intersect_area_ha"] = joined["intersect_area_m2"] / 10_000

    # ------------------------------------------------------------
    # Area-weighted summary metrics
    # ------------------------------------------------------------

    def weighted_mean(group, value_col):
        valid = group[value_col].notnull()
        if valid.sum() == 0:
            return np.nan

        return np.average(
            group.loc[valid, value_col],
            weights=group.loc[valid, "intersect_area_m2"],
        )

    summary_rows = []

    for polygon_id, group in joined.groupby(custom_id):
        forest_area_m2 = group["intersect_area_m2"].sum()
        custom_area_m2 = group["custom_area_m2"].iloc[0]

        # Dominant priority class by largest intersected area
        dominant_priority_class = (
            group.groupby("priority_class")["intersect_area_m2"]
            .sum()
            .sort_values(ascending=False)
            .index[0]
            if "priority_class" in group.columns
            else None
        )

        summary_rows.append(
            {
                custom_id: polygon_id,
                "forest_area_ha": forest_area_m2 / 10_000,
                "forest_cover_pct": forest_area_m2 / custom_area_m2,
                "mean_fragmentation_score": weighted_mean(
                    group,
                    "conservation_score",
                ),
                "mean_road_impact_pct": weighted_mean(
                    group,
                    "road_impact_pct",
                ),
                "mean_connectivity_score": weighted_mean(
                    group,
                    "connectivity_score",
                ),
                "mean_core_score": weighted_mean(
                    group,
                    "core_score",
                ),
                "mean_area_score": weighted_mean(
                    group,
                    "area_score",
                ),
                "mean_compactness_score": weighted_mean(
                    group,
                    "compactness_score",
                ),
                "total_core_area_ha": group["core_area_ha"].sum()
                if "core_area_ha" in group.columns
                else np.nan,
                "best_statewide_rank": group["statewide_rank"].min()
                if "statewide_rank" in group.columns
                else np.nan,
                "dominant_priority_class": dominant_priority_class,
                "intersected_patch_count": group["patch_id"].nunique()
                if "patch_id" in group.columns
                else len(group),
            }
        )

    summary = pd.DataFrame(summary_rows)

    # ------------------------------------------------------------
    # Join metrics back to custom polygons
    # ------------------------------------------------------------

    output = custom.merge(
        summary,
        on=custom_id,
        how="left",
    )

    # Fill polygons that did not intersect forest
    output["forest_area_ha"] = output["forest_area_ha"].fillna(0)
    output["forest_cover_pct"] = output["forest_cover_pct"].fillna(0)

    # Optional class based on custom polygon summary score
    valid_scores = output["mean_fragmentation_score"].notnull()

    output["custom_fragmentation_class"] = None

    if valid_scores.sum() >= 4:
        output.loc[valid_scores, "custom_fragmentation_class"] = pd.qcut(
            output.loc[valid_scores, "mean_fragmentation_score"],
            q=4,
            labels=["Low", "Medium", "High", "Very High"],
            duplicates="drop",
        ).astype(str)

    # ------------------------------------------------------------
    # Export
    # ------------------------------------------------------------

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    output.to_file(
        output_path,
        layer="custom_polygon_fragmentation",
        driver="GPKG",
    )

    output.drop(columns="geometry").to_csv(
        str(output_path).replace(".gpkg", ".csv"),
        index=False,
    )

    print("Saved custom polygon fragmentation output:")
    print(output_path)
    print(str(output_path).replace(".gpkg", ".csv"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Summarize statewide forest fragmentation metrics to custom polygons."
    )

    parser.add_argument(
        "--polygons",
        required=True,
        help="Path to custom polygon file, e.g. data/private_forest_polygons.gpkg",
    )

    parser.add_argument(
        "--polygon-layer",
        default=None,
        help="Optional layer name if input is a GeoPackage or GDB.",
    )

    parser.add_argument(
        "--id-field",
        default=None,
        help="Optional unique ID field in custom polygons.",
    )

    parser.add_argument(
        "--statewide-patches",
        default="outputs/ri_statewide_forest_priority.gpkg",
        help="Statewide fragmentation GeoPackage from workflow.py.",
    )

    parser.add_argument(
        "--statewide-layer",
        default="forest_priority",
        help="Layer name in statewide fragmentation GeoPackage.",
    )

    parser.add_argument(
        "--output",
        default="outputs/custom_polygon_fragmentation.gpkg",
        help="Output GeoPackage path.",
    )

    args = parser.parse_args()

    summarize_custom_polygons(
        polygon_path=args.polygons,
        polygon_layer=args.polygon_layer,
        id_field=args.id_field,
        statewide_patches_path=args.statewide_patches,
        statewide_layer=args.statewide_layer,
        output_path=args.output,
    )