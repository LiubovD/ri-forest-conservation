import os
import requests
import geopandas as gpd
import rasterio

from rasterio.mask import mask

os.makedirs("data", exist_ok=True)

# ---------------------------------------------------
# NLCD 2021 download
# ---------------------------------------------------

url = (
    "https://www.mrlc.gov/downloads/sciweb1/shared/"
    "mrlc/data-bundles/"
    "Annual_NLCD_LndCov_2021_CU_C1V1.zip"
)

zip_path = "data/nlcd_2021.zip"

# ---------------------------------------------------
# Download NLCD zip
# ---------------------------------------------------

if not os.path.exists(zip_path):

    print("Downloading NLCD zip...")

    r = requests.get(url, stream=True)
    r.raise_for_status()

    with open(zip_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

else:
    print("NLCD zip already exists")

# ---------------------------------------------------
# Unzip
# ---------------------------------------------------

unzip_dir = "data/nlcd_2021"

if not os.path.exists(unzip_dir):

    print("Unzipping...")

    os.system(
        f'unzip -o "{zip_path}" -d "{unzip_dir}"'
    )

else:
    print("NLCD already unzipped")

# ---------------------------------------------------
# Find NLCD raster
# ---------------------------------------------------

nlcd_file = None

for root, dirs, files in os.walk(unzip_dir):
    for file in files:

        if file.endswith(".tif"):
            nlcd_file = os.path.join(root, file)

if nlcd_file is None:
    raise RuntimeError(
        "Could not find NLCD .tif file after unzip."
    )

print("Found raster:")
print(nlcd_file)

# ---------------------------------------------------
# Load RI boundary
# ---------------------------------------------------

ri = gpd.read_file(
    "data/ri_boundary.gpkg"
)

# ---------------------------------------------------
# Clip raster to Rhode Island
# ---------------------------------------------------

with rasterio.open(nlcd_file) as src:

    ri = ri.to_crs(src.crs)

    geoms = [geom for geom in ri.geometry]

    clipped, transform = mask(
        src,
        geoms,
        crop=True
    )

    profile = src.profile.copy()

    profile.update(
        {
            "height": clipped.shape[1],
            "width": clipped.shape[2],
            "transform": transform,
            "driver": "GTiff",
        }
    )

    output_path = "data/ri_nlcd_landcover.tif"

    with rasterio.open(
        output_path,
        "w",
        **profile
    ) as dst:

        dst.write(clipped)

print("Saved:")
print("data/ri_nlcd_landcover.tif")