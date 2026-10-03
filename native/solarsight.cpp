

#include "solarsight.h"

#include <algorithm>
#include <cmath>
#include <vector>

using namespace std;

static const double PI = 3.14159265358979323846;
static const double D2R = PI / 180.0;


static int doy_utc(double t) {
    long long z = (long long)floor(t / 86400.0) + 719468;
    long long era = (z >= 0 ? z : z - 146096) / 146097;
    long long doe = z - era * 146097;
    long long yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    long long d = doe - (365 * yoe + yoe / 4 - yoe / 100);  
    bool leap = (yoe % 4 == 0 && yoe % 100 != 0) || yoe % 400 == 0;

    return d >= 306 ? (int)(d - 305) : (int)(d + 60 + (leap ? 1 : 0));
}

static double day_ang(int doy) { return 2 * PI / 365.0 * (doy - 1); }


static double decl(int doy) {
    double b = day_ang(doy);
    return 0.006918 - 0.399912 * cos(b) + 0.070257 * sin(b) - 0.006758 * cos(2 * b) +
        0.000907 * sin(2 * b) - 0.002697 * cos(3 * b) + 0.00148 * sin(3 * b);
}

static double eot(int doy) {
    double b = day_ang(doy);
    return 1440.0 / 2 / PI *
        (0.0000075 + 0.001868 * cos(b) - 0.032077 * sin(b) - 0.014615 * cos(2 * b) -
            0.040849 * sin(2 * b));
}

static double cos_aoi(double zen, double azi, double tilt, double surf_az) {
    double z = zen * D2R, b = tilt * D2R;
    double c = cos(z) * cos(b) + sin(z) * sin(b) * cos((azi - surf_az) * D2R);
    return clamp(c, -1.0, 1.0);
}

static double haurwitz(double zen) {
    double cz = cos(zen * D2R);
    return cz > 0 ? 1098.0 * cz * exp(-0.059 / cz) : 0.0;
}

static double kc(double ghi_clear, double cloud) {
    double n = clamp(cloud, 0.0, 100.0) / 100.0; 
    return ghi_clear * (1 - 0.75 * pow(n, 3.4));
}
static void erbs1(double ghi, double zen, int doy, double& dni, double& dhi) {
    double b = day_ang(doy);
    double e0 = 1366.1 * (1.00011 + 0.034221 * cos(b) + 0.00128 * sin(b) + 0.000719 * cos(2 * b) +
        7.7e-05 * sin(2 * b));
    double cz = cos(zen * D2R);
    double kt = clamp(ghi / (e0 * max(cz, 0.065)), 0.0, 1.0);
    double df = 1 - 0.09 * kt;
    if (kt > 0.8)
        df = 0.165;
    else if (kt > 0.22)
        df = 0.9511 - 0.1604 * kt + 4.388 * kt * kt - 16.638 * pow(kt, 3) + 12.336 * pow(kt, 4);
    dhi = df * ghi;
    dni = (ghi - dhi) / cz;
    if (zen > 87 || ghi < 0 || dni < 0) {
        dni = 0;
        dhi = ghi;
    }
}

extern "C" {

    int ss_sun_position(const double* t, int n, double lat, double lon, double* zen, double* azi) {
        if (!t || !zen || !azi) return 1;
        if (n < 0) return 2;
        if (fabs(lat) > 90 || fabs(lon) > 180) return 3;
        double phi = lat * D2R;
        for (int i = 0; i < n; i++) {
            int doy = doy_utc(t[i]);
            double dec = decl(doy);
            double hrs = (t[i] - floor(t[i] / 86400.0) * 86400.0) / 3600.0;
            double ha = (15 * (hrs - 12) + lon + eot(doy) / 4) * D2R;
            double z = acos(clamp(cos(dec) * cos(phi) * cos(ha) + sin(dec) * sin(phi), -1.0, 1.0));
          
            double den = sin(z) * cos(phi);
            double ca = fabs(den) < 1e-8 ? 1.0 : (cos(z) * sin(phi) - sin(dec)) / den;
            if (fabs(ca - 1) < 1e-8) ca = 1;
            if (fabs(ca + 1) < 1e-8) ca = -1;
            double sgn = (ha > 0) - (ha < 0);
            zen[i] = z / D2R;
            azi[i] = (sgn * acos(clamp(ca, -1.0, 1.0)) + PI) / D2R;
        }
        return 0;
    }

    int ss_angle_of_incidence(const double* zen, const double* azi, int n, double tilt, double surf_az,
        double* aoi) {
        if (!zen || !azi || !aoi) return 1;
        if (n < 0) return 2;
        for (int i = 0; i < n; i++) aoi[i] = acos(cos_aoi(zen[i], azi[i], tilt, surf_az)) / D2R;
        return 0;
    }

    int ss_poa_irradiance(const double* zen, const double* azi, const double* ghi, const double* dni,
        const double* dhi, int n, double tilt, double surf_az, double albedo,
        double* poa) {
        if (!zen || !azi || !ghi || !dni || !dhi || !poa) return 1;
        if (n < 0) return 2;
        if (tilt < 0 || tilt > 90 || surf_az < 0 || surf_az > 360) return 3;
        double cb = cos(tilt * D2R);
        for (int i = 0; i < n; i++) {
       
            double beam = zen[i] < 90 ? dni[i] * max(cos_aoi(zen[i], azi[i], tilt, surf_az), 0.0) : 0;
            poa[i] = max(beam + dhi[i] * (1 + cb) / 2 + ghi[i] * albedo * (1 - cb) / 2, 0.0);
        }
        return 0;
    }

    int ss_cell_temperature(const double* t_air, const double* poa, int n, double noct,
        double* t_cell) {
        if (!t_air || !poa || !t_cell) return 1;
        if (n < 0) return 2;
        for (int i = 0; i < n; i++) t_cell[i] = t_air[i] + (noct - 20) / 800 * poa[i];
        return 0;
    }

    int ss_dc_power(const double* poa, const double* t_cell, int n, double p_stc, double gamma,
        double* p_dc) {
        if (!poa || !t_cell || !p_dc) return 1;
        if (n < 0) return 2;
        for (int i = 0; i < n; i++)
            p_dc[i] = max(p_stc * poa[i] / 1000 * (1 + gamma * (t_cell[i] - 25)), 0.0);
        return 0;
    }

    int ss_ac_power(const double* p_dc, int n, double pr, double inv_max, double* p_ac) {
        if (!p_dc || !p_ac) return 1;
        if (n < 0) return 2;
        for (int i = 0; i < n; i++) p_ac[i] = clamp(p_dc[i] * pr, 0.0, inv_max);
        return 0;
    }

    int ss_energy_kwh(const double* p_ac, int n, double dt_h, double* kwh) {
        if (!p_ac || !kwh) return 1;
        if (n < 0) return 2;
        double wh = 0;
        for (int i = 0; i < n; i++) wh += p_ac[i] * dt_h;
        *kwh = wh / 1000;
        return 0;
    }

    int ss_clearsky_haurwitz(const double* zen, int n, double* ghi_clear) {
        if (!zen || !ghi_clear) return 1;
        if (n < 0) return 2;
        for (int i = 0; i < n; i++) ghi_clear[i] = haurwitz(zen[i]);
        return 0;
    }

    int ss_kasten_czeplak(const double* ghi_clear, const double* cloud, int n, double* ghi) {
        if (!ghi_clear || !cloud || !ghi) return 1;
        if (n < 0) return 2;
        for (int i = 0; i < n; i++) ghi[i] = kc(ghi_clear[i], cloud[i]);
        return 0;
    }

    int ss_erbs(const double* ghi, const double* zen, const double* t, int n, double* dni,
        double* dhi) {
        if (!ghi || !zen || !t || !dni || !dhi) return 1;
        if (n < 0) return 2;
        for (int i = 0; i < n; i++) erbs1(ghi[i], zen[i], doy_utc(t[i]), dni[i], dhi[i]);
        return 0;
    }

    int ss_irradiance_from_clouds(const double* zen, const double* cloud, const double* t, int n,
        double* ghi, double* dni, double* dhi) {
        if (!zen || !cloud || !t || !ghi || !dni || !dhi) return 1;
        if (n < 0) return 2;
        for (int i = 0; i < n; i++) {
            ghi[i] = kc(haurwitz(zen[i]), cloud[i]);
            erbs1(ghi[i], zen[i], doy_utc(t[i]), dni[i], dhi[i]);
        }
        return 0;
    }
}


struct Sky {
    vector<double> zen, azi, ghi, dni, dhi;
};

static bool leap(int y) { return (y % 4 == 0 && y % 100 != 0) || y % 400 == 0; }

static int make_sky(double lat, double lon, int year, double cloud, Sky& sky) {
    if (year < 1970 || year > 2100 || cloud < 0 || cloud > 100) return 3;
    double t0 = 0;
    for (int y = 1970; y < year; y++) t0 += (leap(y) ? 366 : 365) * 86400.0;
    int n = (leap(year) ? 366 : 365) * 24;
    vector<double> t(n), cl(n, cloud);
    for (int i = 0; i < n; i++) t[i] = t0 + i * 3600.0 + 1800.0;
    sky.zen.resize(n);
    sky.azi.resize(n);
    sky.ghi.resize(n);
    sky.dni.resize(n);
    sky.dhi.resize(n);
    int res = ss_sun_position(t.data(), n, lat, lon, sky.zen.data(), sky.azi.data());
    if (res) return res;
    return ss_irradiance_from_clouds(sky.zen.data(), cl.data(), t.data(), n, sky.ghi.data(),
        sky.dni.data(), sky.dhi.data());
}

static int year_kwh(Sky& sky, double tilt, double surf_az, double albedo, vector<double>& buf,
    double* kwh_m2) {
    int n = (int)sky.zen.size();
    buf.resize(n);
    int res = ss_poa_irradiance(sky.zen.data(), sky.azi.data(), sky.ghi.data(), sky.dni.data(),
        sky.dhi.data(), n, tilt, surf_az, albedo, buf.data());
    if (res) return res;
    return ss_energy_kwh(buf.data(), n, 1.0, kwh_m2);
}

extern "C" {

    SS_API int ss_annual_poa_kwh_m2(double lat, double lon, int year, double tilt, double surf_az,
        double albedo, double cloud, double* kwh_m2) {
        if (!kwh_m2) return 1;
        Sky sky;
        int res = make_sky(lat, lon, year, cloud, sky);
        if (res) return res;
        vector<double> buf;
        return year_kwh(sky, tilt, surf_az, albedo, buf, kwh_m2);
    }

    SS_API int ss_optimal_orientation(double lat, double lon, int year, double albedo,
        double cloud, double tilt_step, double az_step,
        double* best_tilt, double* best_az, double* best_kwh_m2) {
        if (!best_tilt || !best_az || !best_kwh_m2) return 1;
        if (tilt_step <= 0 || az_step <= 0) return 3;
        Sky sky;
        int res = make_sky(lat, lon, year, cloud, sky);
        if (res) return res;
        vector<double> buf;
        *best_kwh_m2 = -1;
        for (double tilt = 0; tilt <= 90; tilt += tilt_step) {
            for (double az = 0; az < 360; az += az_step) {
                double kwh = 0;
                res = year_kwh(sky, tilt, az, albedo, buf, &kwh);
                if (res) return res;
                if (kwh > *best_kwh_m2) {
                    *best_kwh_m2 = kwh;
                    *best_tilt = tilt;
                    *best_az = az;
                }
            }
        }
        return 0;
    }
}

extern "C" SS_API int ss_metrics(const double* pred, const double* obs, const double* ref, int n,
    double* rmse, double* mae, double* skill) {
    double sum_sq = 0;
    double sum_abs = 0;
    double sum_ref = 0;
    for (int i = 0; i < n; i++) {
        double err = pred[i] - obs[i];
        double err_ref = ref[i] - obs[i];
        sum_sq += err * err;
        sum_abs += fabs(err);
        sum_ref += err_ref * err_ref;
    }
    *rmse = sqrt(sum_sq / n);
    *mae = sum_abs / n;
    if (sum_ref == 0) {
        *skill = 0;
    }
    else {
        *skill = 1 - *rmse / sqrt(sum_ref / n);
    }
    return 0;
}