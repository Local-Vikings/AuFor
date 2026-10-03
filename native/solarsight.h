/*
 * solarsight.h: C API of the SolarSight C++17 physics core (bible.md 9.0).
 *
 * Boundary rules: only extern "C" functions, flat double arrays plus length n,
 * the caller allocates every output buffer, every function returns an int
 * status code and never throws. Angles are degrees at the API, time is UTC
 * epoch seconds. Azimuth convention: 180 = south (same as pvlib).
 *
 * Covers T12 (sun position, POA), T13 (cell temp, DC, AC, energy) and
 * T14 (clear-sky, Kasten-Czeplak, Erbs). Battery, advisor and weather stay in Python.
 */
#ifndef SOLARSIGHT_H
#define SOLARSIGHT_H

#ifdef _WIN32
#define SS_API __declspec(dllexport)
#else
#define SS_API __attribute__((visibility("default")))
#endif

/* Status codes returned by every ss_ function. */
#define SS_OK 0
#define SS_ERR_NULL_POINTER 1  /* an input or output pointer is NULL */
#define SS_ERR_BAD_LENGTH 2    /* n < 0 */
#define SS_ERR_BAD_PARAMETER 3 /* a scalar parameter is out of range or not finite */

#ifdef __cplusplus
extern "C" {
#endif

/* ---------- T12: sun position and plane-of-array irradiance ---------- */

/*
 * Sun position (bible 9.1), Spencer (1971) declination and equation of time,
 * analytical zenith/azimuth (same formulas as pvlib spencer71 and analytical functions).
 * t_utc: UTC epoch seconds. For Open-Meteo hourly averages pass t - 1800 s
 * (middle of the preceding hour, bible 9.1).
 * lat_deg in [-90, 90], lon_deg in [-180, 180] (east positive).
 * out_zenith_deg: 0 = overhead, > 90 = below horizon.
 * out_azimuth_deg: 0 = north, 90 = east, 180 = south, 270 = west.
 */
SS_API int ss_sun_position(const double* t_utc, int n, double lat_deg, double lon_deg,
                           double* out_zenith_deg, double* out_azimuth_deg);

/*
 * Angle of incidence between the sun and the panel normal (degrees, 0..180).
 * tilt_deg in [0, 90], surface_azimuth_deg in [0, 360] (180 = south).
 */
SS_API int ss_angle_of_incidence(const double* zenith_deg, const double* azimuth_deg, int n,
                                 double tilt_deg, double surface_azimuth_deg,
                                 double* out_aoi_deg);

/*
 * Plane-of-array irradiance, isotropic sky (bible 9.2), W/m2:
 * G_poa = DNI*max(cos(aoi),0) + DHI*(1+cos b)/2 + GHI*albedo*(1-cos b)/2.
 * The direct term is also 0 when the sun is below the horizon (zenith >= 90).
 * albedo in [0, 1] (default 0.2).
 */
SS_API int ss_poa_irradiance(const double* zenith_deg, const double* azimuth_deg,
                             const double* ghi, const double* dni, const double* dhi, int n,
                             double tilt_deg, double surface_azimuth_deg, double albedo,
                             double* out_poa_w_m2);

/* ---------- T13: cell temperature, DC and AC power, energy ---------- */

/* Cell temperature, NOCT model (bible 9.3): T_cell = T_air + (NOCT-20)/800 * G_poa. */
SS_API int ss_cell_temperature(const double* temp_air_c, const double* poa_w_m2, int n,
                               double noct_c, double* out_t_cell_c);

/*
 * DC power (bible 9.4): P_dc = P_stc * G_poa/1000 * (1 + gamma*(T_cell-25)), clamped >= 0.
 * p_stc_w = panel_count * watt_peak (> 0); gamma per degree C (e.g. -0.004).
 */
SS_API int ss_dc_power(const double* poa_w_m2, const double* t_cell_c, int n, double p_stc_w,
                       double gamma_per_c, double* out_p_dc_w);

/*
 * AC power (bible 9.5): P_ac = min(P_dc * PR, P_inverter_max). Apply per inverter
 * (per panel group) and sum in the caller. pr in (0, 1.05], inverter_max_w > 0.
 */
SS_API int ss_ac_power(const double* p_dc_w, int n, double pr, double inverter_max_w,
                       double* out_p_ac_w);

/* Energy (bible 9.6): out_energy_kwh = sum(P_ac_W * dt_h) / 1000. dt_h > 0. */
SS_API int ss_energy_kwh(const double* p_ac_w, int n, double dt_h, double* out_energy_kwh);

/* ---------- T14: clear-sky and cloud fallback ---------- */

/* Haurwitz clear-sky GHI (bible 9.7): 1098*cos z*exp(-0.059/cos z), 0 if cos z <= 0. */
SS_API int ss_clearsky_haurwitz(const double* zenith_deg, int n, double* out_ghi_clear);

/*
 * Kasten-Czeplak (bible 9.7): GHI = GHI_clear * (1 - 0.75*(N/8)^3.4),
 * N = cloud_cover_pct/100*8 (cloud cover is clamped to 0..100 %).
 */
SS_API int ss_kasten_czeplak(const double* ghi_clear, const double* cloud_cover_pct, int n,
                             double* out_ghi);

/*
 * Erbs decomposition GHI -> DNI + DHI (bible 9.7), same as pvlib.irradiance.erbs
 * with defaults (min_cos_zenith 0.065, max_zenith 87, Spencer extra-terrestrial
 * radiation, solar constant 1366.1). t_utc gives the day of year (UTC date).
 */
SS_API int ss_erbs(const double* ghi, const double* zenith_deg, const double* t_utc, int n,
                   double* out_dni, double* out_dhi);

/*
 * Full fallback for missing irradiance (bible 9.7):
 * Haurwitz -> Kasten-Czeplak -> Erbs. Gives GHI, DNI, DHI ready for ss_poa_irradiance.
 */
SS_API int ss_irradiance_from_clouds(const double* zenith_deg, const double* cloud_cover_pct,
                                     const double* t_utc, int n, double* out_ghi,
                                     double* out_dni, double* out_dhi);

#ifdef __cplusplus
}
#endif

#endif /* SOLARSIGHT_H */
