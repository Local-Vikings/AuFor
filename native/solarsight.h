#pragma once
// solarsight C API - flat double arrays, python allocates outputs
// return codes: 0 ok, 1 null ptr, 2 bad n, 3 bad param
// angles in deg, azimuth 180 = south, time = utc epoch seconds
#pragma once

#ifdef _WIN32
#define SS_API __declspec(dllexport)
#else
#define SS_API __attribute__((visibility("default")))
#endif

extern "C" {

    // T12
    SS_API int ss_sun_position(const double* t, int n, double lat, double lon, double* zen, double* azi);
    SS_API int ss_angle_of_incidence(const double* zen, const double* azi, int n, double tilt,
        double surf_az, double* aoi);
    SS_API int ss_poa_irradiance(const double* zen, const double* azi, const double* ghi,
        const double* dni, const double* dhi, int n, double tilt,
        double surf_az, double albedo, double* poa);

    // T13
    SS_API int ss_cell_temperature(const double* t_air, const double* poa, int n, double noct,
        double* t_cell);
    SS_API int ss_dc_power(const double* poa, const double* t_cell, int n, double p_stc, double gamma,
        double* p_dc);
    SS_API int ss_ac_power(const double* p_dc, int n, double pr, double inv_max, double* p_ac);
    SS_API int ss_energy_kwh(const double* p_ac, int n, double dt_h, double* kwh);

    // T14
    SS_API int ss_clearsky_haurwitz(const double* zen, int n, double* ghi_clear);
    SS_API int ss_kasten_czeplak(const double* ghi_clear, const double* cloud, int n, double* ghi);
    SS_API int ss_erbs(const double* ghi, const double* zen, const double* t, int n, double* dni,
        double* dhi);
    SS_API int ss_irradiance_from_clouds(const double* zen, const double* cloud, const double* t, int n,
        double* ghi, double* dni, double* dhi);
}
