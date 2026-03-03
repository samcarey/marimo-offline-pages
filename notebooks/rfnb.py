import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(
        r"""
        # RF Notebook (rfnb) Demo

        This notebook demonstrates the **rfnb** package running entirely in the
        browser via WebAssembly.  All assets are served from this static site —
        no internet connection is required.

        rfnb provides tools for RF link analysis, orbital mechanics, coordinate
        conversions, and 3D scenario visualisation built on top of **Polars**
        and **marimo**.
        """
    )
    return


@app.cell
def _():
    import polars as pl
    import rfnb.columns as col
    from rfnb import earth_model
    from rfnb.platforms import construct_facility
    return col, construct_facility, earth_model, pl


@app.cell
def _(mo):
    mo.md(
        r"""
        ## Orbital Mechanics

        rfnb includes an earth model with WGS-84 constants and common orbital
        mechanics utilities.  Use the slider below to explore how altitude
        affects orbital velocity and period.
        """
    )
    return


@app.cell
def _(mo):
    altitude_slider = mo.ui.slider(
        start=200, stop=36000, step=100, value=400,
        label="Orbit altitude (km)",
    )
    altitude_slider
    return (altitude_slider,)


@app.cell
def _(altitude_slider, earth_model, mo):
    alt_km = altitude_slider.value
    velocity_km_s = earth_model.alt_to_vel_km(alt_km)
    period_days = earth_model.alt_to_period_km(alt_km)
    period_minutes = period_days * 24 * 60

    mo.md(
        f"""
        ### Orbit at {alt_km:,} km altitude

        | Parameter | Value |
        |-----------|-------|
        | Orbital velocity | {velocity_km_s:.3f} km/s |
        | Orbital period | {period_minutes:.1f} minutes |
        | Mean motion | {earth_model.period_to_mean_motion(period_days):.6f} rev/day |
        | Earth radius (equatorial) | {earth_model.EQUATORIAL_RADIUS_M/1000:.3f} km |
        """
    )
    return


@app.cell
def _(earth_model, mo, pl):
    altitudes = list(range(200, 36200, 200))
    orbit_df = pl.DataFrame({
        "Altitude (km)": altitudes,
        "Velocity (km/s)": [earth_model.alt_to_vel_km(a) for a in altitudes],
        "Period (min)": [earth_model.alt_to_period_km(a) * 24 * 60 for a in altitudes],
    })

    chart = mo.ui.altair_chart(
        _make_orbit_chart(orbit_df),
    )
    chart
    return (chart,)


@app.cell
def _():
    def _make_orbit_chart(df):
        import altair as alt
        base = alt.Chart(df).encode(
            x=alt.X("Altitude (km):Q", title="Altitude (km)"),
        )
        velocity_line = base.mark_line(color="#1f77b4").encode(
            y=alt.Y("Velocity (km/s):Q", title="Velocity (km/s)"),
        )
        return velocity_line.properties(
            title="Orbital Velocity vs Altitude",
            width="container",
            height=300,
        )
    return (_make_orbit_chart,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## Ground Station (Facility) Creation

        rfnb lets you create ground station facilities from geodetic coordinates.
        Enter coordinates below to place a facility and inspect its ECEF
        (Earth-Centered, Earth-Fixed) position.
        """
    )
    return


@app.cell
def _(mo):
    station_name = mo.ui.text(value="Kirtland AFB", label="Station name")
    station_lat = mo.ui.number(value=35.0485, start=-90, stop=90, step=0.0001, label="Latitude (deg)")
    station_lon = mo.ui.number(value=-106.5493, start=-180, stop=180, step=0.0001, label="Longitude (deg)")
    station_alt = mo.ui.number(value=5355, start=0, stop=100000, step=1, label="Altitude (ft)")
    mo.hstack([station_name, station_lat, station_lon, station_alt])
    return station_alt, station_lat, station_lon, station_name


@app.cell
def _(col, construct_facility, earth_model, mo, station_alt, station_lat, station_lon, station_name):
    facility = construct_facility(
        station_name.value,
        station_lat.value,
        station_lon.value,
        station_alt.value,
    )

    pos_df = facility.position.collect()
    x_m = pos_df[col.ECEF_X].item()
    y_m = pos_df[col.ECEF_Y].item()
    z_m = pos_df[col.ECEF_Z].item()

    mo.md(
        f"""
        ### {facility.name}

        | Coordinate | Value |
        |-----------|-------|
        | Latitude | {station_lat.value:.4f} deg |
        | Longitude | {station_lon.value:.4f} deg |
        | Altitude | {station_alt.value:,.0f} ft ({station_alt.value * earth_model.METERS_IN_FOOT:.1f} m) |
        | ECEF X | {x_m:,.1f} m |
        | ECEF Y | {y_m:,.1f} m |
        | ECEF Z | {z_m:,.1f} m |

        The facility position is stored as a Polars LazyFrame using rfnb's
        standard column naming convention (e.g. `{col.ECEF_X}`, `{col.LAT_DEG}`).
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## Horizon Distance

        The distance to the radio horizon depends on the observer's altitude
        and the Earth's local radius.  rfnb uses the WGS-84 ellipsoid for
        accurate calculations.
        """
    )
    return


@app.cell
def _(earth_model, mo, pl):
    import math

    altitudes_ft = [10, 100, 500, 1000, 5000, 10000, 30000, 60000]
    R = earth_model.EQUATORIAL_RADIUS_M / earth_model.METERS_IN_FOOT

    horizon_df = pl.DataFrame({
        "Altitude (ft)": altitudes_ft,
        "Altitude (m)": [a * earth_model.METERS_IN_FOOT for a in altitudes_ft],
        "Horizon (km)": [
            math.sqrt(2 * R * a + a**2) * earth_model.METERS_IN_FOOT / 1000
            for a in altitudes_ft
        ],
        "Horizon (nmi)": [
            math.sqrt(2 * R * a + a**2) * earth_model.METERS_IN_FOOT / 1852
            for a in altitudes_ft
        ],
    })

    mo.ui.table(horizon_df)
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## Free-Space Path Loss

        The fundamental RF link equation includes free-space path loss (FSPL),
        which increases with both distance and frequency:

        $$
        \text{FSPL (dB)} = 92.45 + 20\log_{10}(d_{\text{km}}) + 20\log_{10}(f_{\text{GHz}})
        $$
        """
    )
    return


@app.cell
def _(mo):
    freq_slider = mo.ui.slider(
        start=0.1, stop=40.0, step=0.1, value=2.2,
        label="Frequency (GHz)",
    )
    freq_slider
    return (freq_slider,)


@app.cell
def _(freq_slider, mo, pl):
    import math as _math

    freq_ghz = freq_slider.value
    distances_km = [1, 10, 50, 100, 500, 1000, 5000, 10000, 36000]
    fspl_values = [
        92.45 + 20 * _math.log10(d) + 20 * _math.log10(freq_ghz)
        for d in distances_km
    ]

    fspl_df = pl.DataFrame({
        "Distance (km)": distances_km,
        f"FSPL at {freq_ghz} GHz (dB)": [round(v, 1) for v in fspl_values],
    })

    mo.md(
        f"""
        ### Free-Space Path Loss at {freq_ghz} GHz

        {mo.as_html(mo.ui.table(fspl_df))}

        At GEO distance (36,000 km) and {freq_ghz} GHz, the path loss is
        **{fspl_values[-1]:.1f} dB**.
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ---
        *This notebook runs entirely in the browser via Pyodide.  The rfnb
        package and all its dependencies were pre-downloaded at build time and
        are served from this static site.*
        """
    )
    return


if __name__ == "__main__":
    app.run()
