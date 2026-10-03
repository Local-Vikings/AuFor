// solarsight.cpp: SolarSight C++17 physics core (bible.md 9.0 to 9.7).
// Pure functions over flat double arrays: no globals, no I/O, no allocation
// handed to the caller, no exceptions across the C boundary.
// Does not do battery, advisor, weather fetching or timezone handling.

#include "solarsight.h"

#include <algorithm>
#include <cmath>

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr double kDegToRad = kPi / 180.0;
constexpr double kRadToDeg = 180.0 / kPi;
constexpr double kSecondsPerDay = 86400.0;
constexpr double kSecondsPerHour = 3600.0;

// Isotropic/NOCT/STC reference values (bible 9.3, 9.4).
constexpr double kNoctAirC = 20.0;
constexpr double kNoctIrradiance = 800.0;
constexpr double kStcIrradiance = 1000.0;
constexpr double kStcCellC = 25.0;

// Haurwitz clear-sky constants (bible 9.7).
constexpr double kHaurwitzA = 1098.0;
constexpr double kHaurwitzB = 0.059;

// Kasten-Czeplak constants (bible 9.7).
constexpr double kKcCoefficient = 0.75;
constexpr double kKcExponent = 3.4;
constexpr double kOktas = 8.0;

// Erbs defaults, matching pvlib.irradiance.erbs / get_extra_radiation(spencer).
constexpr double kSolarConstant = 1366.1;
constexpr double kErbsMinCosZenith = 0.065;
constexpr double kErbsMaxZenithDeg = 87.0;

// Tolerance pvlib uses when snapping cos(azimuth) to +-1.
constexpr double kAzimuthSnap = 1e-8;

bool bad_args(int n) { return n < 0; }

bool is_finite(double x) { return std::isfinite(x); }

// Days since 1970-01-01 for a proleptic Gregorian date (H. Hinnant's algorithm).
long long days_from_civil(long long y, unsigned m, unsigned d) {
    y -= m <= 2;
    const long long era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(y - era * 400);
    const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + static_cast<long long>(doe) - 719468;
}

// Year of the civil date for a day count since 1970-01-01 (inverse of the above).
long long year_from_days(long long z) {
    z += 719468;
    const long long era = (z >= 0 ? z : z - 146096) / 146097;
    const unsigned doe = static_cast<unsigned>(z - era * 146097);
    const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const unsigned mp = (5 * doy + 2) / 153;
    const unsigned m = mp < 10 ? mp + 3 : mp - 9;
    return static_cast<long long>(yoe) + era * 400 + (m <= 2);
}

// Day of year (1..366) of the UTC date of an epoch timestamp.
int day_of_year_utc(double t_utc) {
    const long long days = static_cast<long long>(std::floor(t_utc / kSecondsPerDay));
    const long long jan1 = days_from_civil(year_from_days(days), 1, 1);
    return static_cast<int>(days - jan1 + 1);
}

// Spencer day angle (radians), offset 1 as in pvlib.
double day_angle(int doy) { return 2.0 * kPi / 365.0 * (doy - 1); }

// Spencer (1971) declination, radians.
double declination_spencer(int doy) {
    const double b = day_angle(doy);
    return 0.006918 - 0.399912 * std::cos(b) + 0.070257 * std::sin(b) -
           0.006758 * std::cos(2 * b) + 0.000907 * std::sin(2 * b) -
           0.002697 * std::cos(3 * b) + 0.00148 * std::sin(3 * b);
}

// Spencer (1971) equation of time, minutes.
double equation_of_time_spencer(int doy) {
    const double b = day_angle(doy);
    return (1440.0 / 2.0 / kPi) *
           (0.0000075 + 0.001868 * std::cos(b) - 0.032077 * std::sin(b) -
            0.014615 * std::cos(2 * b) - 0.040849 * std::sin(2 * b));
}

// Spencer extra-terrestrial normal irradiance, W/m2.
double extra_radiation(int doy) {
    const double b = day_angle(doy);
    return kSolarConstant * (1.00011 + 0.034221 * std::cos(b) + 0.00128 * std::sin(b) +
                             0.000719 * std::cos(2 * b) + 7.7e-05 * std::sin(2 * b));
}

double sign(double x) { return (x > 0) - (x < 0); }

// Analytical azimuth (radians, 0 = north), same branches as pvlib.
double azimuth_analytical(double lat, double ha, double dec, double zen) {
    const double numer = std::cos(zen) * std::sin(lat) - std::sin(dec);
    const double denom = std::sin(zen) * std::cos(lat);
    double cos_azi = std::fabs(denom) <= kAzimuthSnap ? 1.0 : numer / denom;
    if (std::fabs(cos_azi - 1.0) <= kAzimuthSnap) cos_azi = 1.0;
    if (std::fabs(cos_azi + 1.0) <= kAzimuthSnap) cos_azi = -1.0;
    cos_azi = std::clamp(cos_azi, -1.0, 1.0);
    return sign(ha) * std::acos(cos_azi) + kPi;
}

double cos_aoi(double zen_deg, double azi_deg, double tilt_deg, double surf_azi_deg) {
    const double zen = zen_deg * kDegToRad;
    const double tilt = tilt_deg * kDegToRad;
    const double v = std::cos(zen) * std::cos(tilt) +
                     std::sin(zen) * std::sin(tilt) * std::cos((azi_deg - surf_azi_deg) * kDegToRad);
    return std::clamp(v, -1.0, 1.0);
}

bool bad_panel(double tilt_deg, double surface_azimuth_deg) {
    return !is_finite(tilt_deg) || !is_finite(surface_azimuth_deg) || tilt_deg < 0 || tilt_deg > 90 ||
           surface_azimuth_deg < 0 || surface_azimuth_deg > 360;
}

double haurwitz(double zenith_deg) {
    const double cz = std::cos(zenith_deg * kDegToRad);
    return cz > 0 ? kHaurwitzA * cz * std::exp(-kHaurwitzB / cz) : 0.0;
}

double kasten_czeplak(double ghi_clear, double cloud_pct) {
    const double oktas = std::clamp(cloud_pct, 0.0, 100.0) / 100.0 * kOktas;
    return ghi_clear * (1.0 - kKcCoefficient * std::pow(oktas / kOktas, kKcExponent));
}

// Erbs (1982) diffuse fraction model; writes dni/dhi for one sample.
void erbs_one(double ghi, double zenith_deg, int doy, double* dni, double* dhi) {
    const double cz = std::cos(zenith_deg * kDegToRad);
    const double i0h = extra_radiation(doy) * std::max(cz, kErbsMinCosZenith);
    const double kt = std::clamp(ghi / i0h, 0.0, 1.0);
    double df = 1.0 - 0.09 * kt;
    if (kt > 0.22 && kt <= 0.8) {
        df = 0.9511 - 0.1604 * kt + 4.388 * kt * kt - 16.638 * std::pow(kt, 3) +
             12.336 * std::pow(kt, 4);
    } else if (kt > 0.8) {
        df = 0.165;
    }
    *dhi = df * ghi;
    *dni = (ghi - *dhi) / cz;
    if (zenith_deg > kErbsMaxZenithDeg || ghi < 0 || *dni < 0) {
        *dni = 0.0;
        *dhi = ghi;
    }
}

}  // namespace

extern "C" {

int ss_sun_position(const double* t_utc, int n, double lat_deg, double lon_deg,
                    double* out_zenith_deg, double* out_azimuth_deg) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!t_utc || !out_zenith_deg || !out_azimuth_deg) return SS_ERR_NULL_POINTER;
    if (!is_finite(lat_deg) || !is_finite(lon_deg) || std::fabs(lat_deg) > 90 ||
        std::fabs(lon_deg) > 180)
        return SS_ERR_BAD_PARAMETER;
    const double lat = lat_deg * kDegToRad;
    for (int i = 0; i < n; ++i) {
        const int doy = day_of_year_utc(t_utc[i]);
        const double dec = declination_spencer(doy);
        const double hours = (t_utc[i] - std::floor(t_utc[i] / kSecondsPerDay) * kSecondsPerDay) /
                             kSecondsPerHour;
        // Hour angle: 15 deg per hour from solar noon, corrected by longitude and EoT (9.1).
        const double ha = (15.0 * (hours - 12.0) + lon_deg + equation_of_time_spencer(doy) / 4.0) *
                          kDegToRad;
        const double cz = std::cos(dec) * std::cos(lat) * std::cos(ha) + std::sin(dec) * std::sin(lat);
        const double zen = std::acos(std::clamp(cz, -1.0, 1.0));
        out_zenith_deg[i] = zen * kRadToDeg;
        out_azimuth_deg[i] = azimuth_analytical(lat, ha, dec, zen) * kRadToDeg;
    }
    return SS_OK;
}

int ss_angle_of_incidence(const double* zenith_deg, const double* azimuth_deg, int n,
                          double tilt_deg, double surface_azimuth_deg, double* out_aoi_deg) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!zenith_deg || !azimuth_deg || !out_aoi_deg) return SS_ERR_NULL_POINTER;
    if (bad_panel(tilt_deg, surface_azimuth_deg)) return SS_ERR_BAD_PARAMETER;
    for (int i = 0; i < n; ++i) {
        out_aoi_deg[i] =
            std::acos(cos_aoi(zenith_deg[i], azimuth_deg[i], tilt_deg, surface_azimuth_deg)) *
            kRadToDeg;
    }
    return SS_OK;
}

int ss_poa_irradiance(const double* zenith_deg, const double* azimuth_deg, const double* ghi,
                      const double* dni, const double* dhi, int n, double tilt_deg,
                      double surface_azimuth_deg, double albedo, double* out_poa_w_m2) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!zenith_deg || !azimuth_deg || !ghi || !dni || !dhi || !out_poa_w_m2)
        return SS_ERR_NULL_POINTER;
    if (bad_panel(tilt_deg, surface_azimuth_deg) || !is_finite(albedo) || albedo < 0 || albedo > 1)
        return SS_ERR_BAD_PARAMETER;
    const double cos_tilt = std::cos(tilt_deg * kDegToRad);
    for (int i = 0; i < n; ++i) {
        const double c = cos_aoi(zenith_deg[i], azimuth_deg[i], tilt_deg, surface_azimuth_deg);
        // No beam from behind the panel or from below the horizon (9.2).
        const double direct = zenith_deg[i] < 90.0 ? dni[i] * std::max(c, 0.0) : 0.0;
        const double sky = dhi[i] * (1.0 + cos_tilt) / 2.0;
        const double ground = ghi[i] * albedo * (1.0 - cos_tilt) / 2.0;
        out_poa_w_m2[i] = std::max(direct + sky + ground, 0.0);
    }
    return SS_OK;
}

int ss_cell_temperature(const double* temp_air_c, const double* poa_w_m2, int n, double noct_c,
                        double* out_t_cell_c) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!temp_air_c || !poa_w_m2 || !out_t_cell_c) return SS_ERR_NULL_POINTER;
    if (!is_finite(noct_c) || noct_c < kNoctAirC) return SS_ERR_BAD_PARAMETER;
    for (int i = 0; i < n; ++i) {
        out_t_cell_c[i] = temp_air_c[i] + (noct_c - kNoctAirC) / kNoctIrradiance * poa_w_m2[i];
    }
    return SS_OK;
}

int ss_dc_power(const double* poa_w_m2, const double* t_cell_c, int n, double p_stc_w,
                double gamma_per_c, double* out_p_dc_w) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!poa_w_m2 || !t_cell_c || !out_p_dc_w) return SS_ERR_NULL_POINTER;
    if (!is_finite(p_stc_w) || !is_finite(gamma_per_c) || p_stc_w <= 0) return SS_ERR_BAD_PARAMETER;
    for (int i = 0; i < n; ++i) {
        const double p = p_stc_w * (poa_w_m2[i] / kStcIrradiance) *
                         (1.0 + gamma_per_c * (t_cell_c[i] - kStcCellC));
        out_p_dc_w[i] = std::max(p, 0.0);
    }
    return SS_OK;
}

int ss_ac_power(const double* p_dc_w, int n, double pr, double inverter_max_w,
                double* out_p_ac_w) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!p_dc_w || !out_p_ac_w) return SS_ERR_NULL_POINTER;
    if (!is_finite(pr) || !is_finite(inverter_max_w) || pr <= 0 || pr > 1.05 || inverter_max_w <= 0)
        return SS_ERR_BAD_PARAMETER;
    for (int i = 0; i < n; ++i) {
        out_p_ac_w[i] = std::clamp(p_dc_w[i] * pr, 0.0, inverter_max_w);
    }
    return SS_OK;
}

int ss_energy_kwh(const double* p_ac_w, int n, double dt_h, double* out_energy_kwh) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!p_ac_w || !out_energy_kwh) return SS_ERR_NULL_POINTER;
    if (!is_finite(dt_h) || dt_h <= 0) return SS_ERR_BAD_PARAMETER;
    double wh = 0.0;
    for (int i = 0; i < n; ++i) wh += p_ac_w[i] * dt_h;
    *out_energy_kwh = wh / 1000.0;
    return SS_OK;
}

int ss_clearsky_haurwitz(const double* zenith_deg, int n, double* out_ghi_clear) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!zenith_deg || !out_ghi_clear) return SS_ERR_NULL_POINTER;
    for (int i = 0; i < n; ++i) out_ghi_clear[i] = haurwitz(zenith_deg[i]);
    return SS_OK;
}

int ss_kasten_czeplak(const double* ghi_clear, const double* cloud_cover_pct, int n,
                      double* out_ghi) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!ghi_clear || !cloud_cover_pct || !out_ghi) return SS_ERR_NULL_POINTER;
    for (int i = 0; i < n; ++i) out_ghi[i] = kasten_czeplak(ghi_clear[i], cloud_cover_pct[i]);
    return SS_OK;
}

int ss_erbs(const double* ghi, const double* zenith_deg, const double* t_utc, int n,
            double* out_dni, double* out_dhi) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!ghi || !zenith_deg || !t_utc || !out_dni || !out_dhi) return SS_ERR_NULL_POINTER;
    for (int i = 0; i < n; ++i) {
        erbs_one(ghi[i], zenith_deg[i], day_of_year_utc(t_utc[i]), &out_dni[i], &out_dhi[i]);
    }
    return SS_OK;
}

int ss_irradiance_from_clouds(const double* zenith_deg, const double* cloud_cover_pct,
                              const double* t_utc, int n, double* out_ghi, double* out_dni,
                              double* out_dhi) {
    if (bad_args(n)) return SS_ERR_BAD_LENGTH;
    if (!zenith_deg || !cloud_cover_pct || !t_utc || !out_ghi || !out_dni || !out_dhi)
        return SS_ERR_NULL_POINTER;
    for (int i = 0; i < n; ++i) {
        out_ghi[i] = kasten_czeplak(haurwitz(zenith_deg[i]), cloud_cover_pct[i]);
        erbs_one(out_ghi[i], zenith_deg[i], day_of_year_utc(t_utc[i]), &out_dni[i], &out_dhi[i]);
    }
    return SS_OK;
}

}  // extern "C"
