import os
import geopandas as gpd
import osmnx as ox

os.makedirs("data", exist_ok=True)

# -----------------------------
# 1. RIGIS / URI boundary
# -----------------------------
ri_url = (
    "https://maps.edc.uri.edu/arcgis/rest/services/"
    "Atlas_boundaries/Municipal_Boundaries/MapServer/0/query"
    "?where=1%3D1&outFields=*&f=geojson"
)

municipal = gpd.read_file(ri_url)
ri_boundary = municipal.dissolve()

ri_boundary.to_file("data/ri_boundary.gpkg", driver="GPKG")

# -----------------------------
# 2. Roads from OpenStreetMap
# -----------------------------
roads = ox.features_from_place(
    "Rhode Island, USA",
    tags={"highway": True}
)

roads = roads[roads.geometry.type.isin(["LineString", "MultiLineString"])]
roads.to_file("data/ri_roads.gpkg", driver="GPKG")

print("Downloaded:")
print("data/ri_boundary.gpkg")
print("data/ri_roads.gpkg")