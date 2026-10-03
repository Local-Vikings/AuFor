
from app import pipeline, solar


def clear_sky(req, w):

    t = pipeline._epoch_seconds(w.index)
    zen, azi = solar.sun_position(t - 1800, req.lat, req.lon)
    return pipeline._clear_sky(req.panel_groups, w, zen, azi, t, 0.8)  


def over(vals, clear):
   
    cnt = 0
    for v, c in zip(vals, clear):
        if v > c * 1.05 + 0.000001:
            cnt += 1
    return cnt


def evaluate(pred, obs):
    pred = list(pred)
    obs = list(obs)
 
    return solar.metrics(pred[24:], obs[24:], obs[:-24])