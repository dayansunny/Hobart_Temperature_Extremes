"""Extract daily maximum and minimum temperature for Hobart from NCI's public THREDDS server.

Pulls one grid cell per run, cut on the server via OPeNDAP, so nothing large is
downloaded. No NCI account is needed.

* BARPA-R driven by ERA5 (evaluation run), 1985-2014
* BARPA-R driven by ACCESS-CM2 r4i1p1f1: historical 1985-2014 and SSP3-7.0 2015-2100
* AGCD v1.0.3 gridded observations, 1985-2014, averaged over the BARPA-R cell

The BARPA-R cell is the one nearest Hobart with at least half its area on land
(BARPA-R's own land fraction). Values are converted to degrees Celsius.

Outputs go to data/, one file per run with `tasmax` and `tasmin`. Existing
files are skipped, so delete one to fetch it again. Takes roughly 5 minutes.

Usage:  python scripts/fetch_hobart.py
"""

import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import xarray as xr

HOBART = {"lat": -42.88, "lon": 147.32}
MIN_LAND_FRACTION = 50  # percent

THREDDS = "https://thredds.nci.org.au/thredds/dodsC"
BARPA_ROOT = f"{THREDDS}/py18/BARPA/output-CMIP6/DD/AUS-15/BOM"
AGCD_URL = f"{THREDDS}/zv2/agcd/v1-0-3/{{var}}/mean/r005/01day/agcd_v1_{{var}}_mean_r005_daily_{{year}}.nc"
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
WORKERS = 4  # be polite to a shared public server

BARPA_RUNS = {
    # name: (driving model, experiment, member, data version, first year, last year)
    "era5_evaluation": ("ERA5", "evaluation", "r1i1p1f1", "v20240801", 1985, 2014),
    "access-cm2_historical": ("ACCESS-CM2", "historical", "r4i1p1f1", "v20231001", 1985, 2014),
    "access-cm2_ssp370": ("ACCESS-CM2", "ssp370", "r4i1p1f1", "v20231001", 2015, 2100),
}
AGCD_YEARS = (1985, 2014)
VARIABLES = {"tasmax": "tmax", "tasmin": "tmin"}  # BARPA name: AGCD name


def barpa_url(gcm, experiment, member, version, freq, variable, suffix):
    stem = f"{variable}_AUS-15_{gcm}_{experiment}_{member}_BOM_BARPA-R_v1-r1_{freq}{suffix}"
    return f"{BARPA_ROOT}/{gcm}/{experiment}/{member}/BARPA-R/v1-r1/{freq}/{variable}/{version}/{stem}.nc"


def with_retries(func, *args, retries=3):
    for attempt in range(1, retries + 1):
        try:
            return func(*args)
        except Exception as err:  # network hiccups are common on long runs
            if attempt == retries:
                raise RuntimeError(f"Failed after {retries} attempts: {args[0]}") from err
            time.sleep(10 * attempt)


def hobart_cell():
    """Centre and half-width of the BARPA-R cell nearest Hobart that is mostly land."""
    url = barpa_url("ACCESS-CM2", "historical", "r4i1p1f1", "v20231001", "fx", "sftlf", "")
    with xr.open_dataset(url) as ds:
        box = ds["sftlf"].sel(lat=slice(HOBART["lat"] - 0.5, HOBART["lat"] + 0.5),
                              lon=slice(HOBART["lon"] - 0.5, HOBART["lon"] + 0.5)).load()
    distance = np.hypot(box.lat - HOBART["lat"], (box.lon - HOBART["lon"]) * np.cos(np.radians(HOBART["lat"])))
    distance = distance.where(box >= MIN_LAND_FRACTION)
    nearest = distance.argmin(dim=["lat", "lon"])
    cell = box.isel(nearest)
    half_width = float(np.diff(box.lat).mean()) / 2
    return float(cell.lat), float(cell.lon), half_width, float(cell)


def fetch_barpa_year(url, variable, lat, lon):
    with xr.open_dataset(url) as ds:
        da = ds[variable].sel(lat=lat, lon=lon).load()
    # Files differ in extra metadata coordinates (e.g. crs, height), so keep only time.
    return da.drop_vars([c for c in da.coords if c != "time"])


def fetch_agcd_year(url, variable, lat, lon, half_width):
    with xr.open_dataset(url) as ds:
        box = ds[variable].sel(lat=slice(lat - half_width, lat + half_width),
                               lon=slice(lon - half_width, lon + half_width)).load()
    # Mean of the 0.05 degree land points inside the BARPA-R cell (sea points are NaN).
    return box.mean(["lat", "lon"], skipna=True)


def to_celsius(da, name, kelvin):
    attrs = {k: v for k, v in da.attrs.items() if k in ("long_name", "standard_name", "cell_methods")}
    da = (da - 273.15) if kelvin else da
    return da.rename(name).assign_attrs(attrs, units="degC")


def main():
    OUT_DIR.mkdir(exist_ok=True)
    lat, lon, half_width, land = hobart_cell()
    print(f"BARPA-R cell: {lat:.3f}, {lon:.3f} ({land:.0f}% land)")
    cell_attrs = {"cell_lat": lat, "cell_lon": lon, "cell_land_fraction_percent": land,
                  "site": "Hobart, Tasmania", "site_lat": HOBART["lat"], "site_lon": HOBART["lon"]}

    jobs = {}  # output file -> {variable: (fetch function, [args per year]), ...}, global attrs
    for name, (gcm, experiment, member, version, first, last) in BARPA_RUNS.items():
        target = OUT_DIR / f"hobart_barpa-r_{name}_{first}-{last}.nc"
        per_var = {
            var: (fetch_barpa_year,
                  [(barpa_url(gcm, experiment, member, version, "day", var, f"_{year}01-{year}12"), var, lat, lon)
                   for year in range(first, last + 1)])
            for var in VARIABLES
        }
        source = f"BARPA-R v1-r1 AUS-15 driven by {gcm} {experiment} {member} ({version}), NCI THREDDS py18"
        jobs[target] = (per_var, True, source)

    first, last = AGCD_YEARS
    jobs[OUT_DIR / f"hobart_agcd_{first}-{last}.nc"] = (
        {var: (fetch_agcd_year,
               [(AGCD_URL.format(var=agcd_var, year=year), agcd_var, lat, lon, half_width)
                for year in range(first, last + 1)])
         for var, agcd_var in VARIABLES.items()},
        False,
        "AGCD v1.0.3 daily 0.05 degree, mean of land points within the BARPA-R cell, NCI THREDDS zv2",
    )

    # Separate processes, not threads: the netCDF C library is not thread-safe.
    with ProcessPoolExecutor(WORKERS) as pool:
        for target, (per_var, kelvin, source) in jobs.items():
            if target.exists():
                print(f"skip {target.name} (exists)")
                continue
            series = {}
            for var, (func, arg_list) in per_var.items():
                years = list(pool.map(with_retries, [func] * len(arg_list), *zip(*arg_list)))
                series[var] = to_celsius(xr.concat(years, dim="time"), var, kelvin)
            ds = xr.Dataset(series).assign_attrs(cell_attrs, source=source)
            ds.to_netcdf(target)
            print(f"wrote {target.name}: {ds.sizes['time']} days")


if __name__ == "__main__":
    main()
