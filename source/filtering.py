from i18n import tr, set_language
'Independent 3D speed-norm adaptation of Casiez et al. CHI 2012 One Euro.\n\nhttps://gery.casiez.net/1euro/ ; positions metres, speed internally mm/s.\nNo deadband, prediction or intent classifier. Invalid input resets the state.\n'
import numpy as np
PARAMETERS = {'algorithm': 'one_euro_3d_speed_norm_v1', 'min_cutoff_hz': 2.5, 'beta_hz_per_mm_s': 0.04, 'derivative_cutoff_hz': 1.0, 'gap_reset_s': 0.25}

class OneEuro:

    def __init__(self):
        self.reset()

    def reset(self):
        self.time = None
        self.raw = None
        self.value = None
        self.velocity = np.zeros(3)

    @staticmethod
    def alpha(hz, dt):
        a = 2 * np.pi * hz * dt
        return a / (1 + a)

    def step(self, time, point, valid=True):
        point = np.asarray(point, dtype=float)
        if not valid or not np.isfinite(point).all():
            self.reset()
            return np.full(3, np.nan)
        dt = time - self.time if self.time is not None else 0
        if self.time is None or dt <= 0 or dt > PARAMETERS['gap_reset_s']:
            self.velocity = np.zeros(3)
            self.value = point.copy()
        else:
            derivative = (point - self.raw) * 1000 / dt
            a = self.alpha(PARAMETERS['derivative_cutoff_hz'], dt)
            self.velocity = a * derivative + (1 - a) * self.velocity
            cutoff = PARAMETERS['min_cutoff_hz'] + PARAMETERS['beta_hz_per_mm_s'] * np.linalg.norm(self.velocity)
            a = self.alpha(cutoff, dt)
            self.value = a * point + (1 - a) * self.value
        self.time = float(time)
        self.raw = point.copy()
        return self.value.copy()

def ensure_filtered(samples):
    if not samples or 'filtered_xyz' in samples[-1]:
        return
    f = OneEuro()
    for sample in samples:
        sample['filtered_xyz'] = f.step(sample['t'], sample['xyz'], sample['valid'])
