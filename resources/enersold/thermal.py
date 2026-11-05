"""Modèle thermique de la maison, appris sur les mesures, PAR TEMPÉRATURE EXTÉRIEURE.

Toutes les 10 min, on note la température intérieure, la température extérieure, l'énergie de la PAC,
l'énergie de la résistance VMI (et sa puissance en %), et l'air des combles soufflé. On apprend :

    ΔT_int = a · E_pac  +  c · E_vmi  +  g · X_combles  −  b · (T_int − T_ext) · Δt

    a : °C gagnés par kWh électrique de PAC       → 1/a = kWh pour monter de 1 °C (dépend du froid dehors :
                                                     la PAC rend moins quand il gèle)
    c : °C gagnés par kWh de résistance VMI        → dépend du froid dehors (air neuf à réchauffer) et de la
                                                     puissance demandée en 0-10 V
    g : °C gagnés par l'air des combles            (X = (T_combles − T_int) × Δt × ventilateur)
    b : vitesse de refroidissement (par heure)     → la maison perd b·(T_int − T_ext) °C par heure

Un modèle global est appris d'abord, puis un modèle PAR TRANCHE de température extérieure
(< 0, 0-5, 5-10, 10-15, > 15 °C) dès qu'une tranche a assez de mesures, et l'effet de la résistance VMI
par palier de puissance (≤ 40 %, 40-70 %, > 70 %). Tant qu'une tranche n'est pas apprise, le global sert.
"""

import json
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger('enerSol.thermal')

BANDS = [(-99.0, 0.0, 'moins de 0 °C'), (0.0, 5.0, '0 à 5 °C'), (5.0, 10.0, '5 à 10 °C'),
         (10.0, 15.0, '10 à 15 °C'), (15.0, 99.0, 'plus de 15 °C')]
POWER_BANDS = [(0.0, 40.0, 'jusqu\'à 40 %'), (40.0, 70.0, '40 à 70 %'), (70.0, 101.0, 'plus de 70 %')]


def _band(text: Optional[float], bands=BANDS) -> Optional[str]:
    if text is None:
        return None
    for lo, hi, name in bands:
        if lo <= text < hi:
            return name
    return None


class ThermalModel:
    STEP_S = 600            # un échantillon toutes les 10 min
    MAX_DAYS = 60           # deux mois : de quoi couvrir plusieurs tranches de froid
    MIN_SAMPLES = 300       # ~2 jours de chauffe avant d'être jugé fiable
    MIN_BAND = 60           # ~10 h de mesures dans une tranche de température extérieure
    MIN_VMI_SAMPLES = 20    # ~3 h de résistance VMI mesurées avant d'apprendre son effet
    MIN_VMI_BAND = 8
    MIN_ATTIC_SAMPLES = 30
    MIN_R2 = 0.35               # en dessous, le modèle explique trop mal la maison : on ne s'en sert pas

    def __init__(self, datadir: str):
        self._file = os.path.join(datadir, 'thermal.json')
        self._hist_file = os.path.join(datadir, 'thermal_history.json')
        self.hist_samples: list = []    # mesures horaires tirées de l'historique Jeedom (années passées)
        self.hist_info: dict = {}
        # [ts_iso, T0, T1, Text, E_pac_kwh, dt_h, E_vmi_kwh, X_combles, pct_vmi]
        self.samples: list = []
        self.a = self.b = None
        self.c = self.g = None          # résistance VMI, air des combles
        self.k = 0.0                    # apports gratuits (gens, frigo, appareils), °C/h
        self.manual: dict = {'kwh': {}, 'drop': {}}   # valeurs saisies à la main : °C ext → valeur
        self.zone = None                # (bas, haut) W : chauffage zoné (étage seul en dessous de « bas »)
        self.f = None                   # °C au salon par kWh « étage seul »
        self.r2 = 0.0
        self.bands: dict = {}           # tranche → {'a','b','c','n'}
        self.vmi_power: dict = {}       # palier de puissance → c
        self._cur = None                # échantillon en cours
        self._load()

    # ── persistance ───────────────────────────────────────────────
    def _load(self):
        try:
            with open(self._file) as fh:
                d = json.load(fh)
            self.samples = d.get('samples', [])
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(self._hist_file) as fh:
                h = json.load(fh)
            self.hist_samples, self.hist_info = h.get('samples', []), h.get('info', {})
        except Exception:  # noqa: BLE001
            pass
        self.fit()

    def save(self):
        cutoff = (datetime.now() - timedelta(days=self.MAX_DAYS)).isoformat(timespec='minutes')
        self.samples = [s for s in self.samples if s[0] >= cutoff]
        try:
            with open(self._file, 'w') as fh:
                json.dump({'samples': self.samples, 'a': self.a, 'b': self.b, 'c': self.c, 'g': self.g,
                           'r2': self.r2, 'bands': self.bands, 'vmi_power': self.vmi_power}, fh)
        except OSError as e:
            log.warning('Sauvegarde modèle thermique : %s', e)

    # ── historique Jeedom (plusieurs années) ──────────────────────
    NIGHT_HOURS = set(range(19, 24)) | set(range(0, 7))   # pas de soleil par les fenêtres : mesure propre
    HEAT_MONTHS = (10, 11, 12, 1, 2, 3, 4)
    MAX_HIST = 30000
    WINDOW_H = 3                # écart de température mesuré sur 3 h : la sonde (arrondie) bouge assez

    @classmethod
    def samples_from_history(cls, text: list, power: list, tint: list, house: Optional[list] = None) -> list:
        """Mesures horaires à partir de trois séries [['YYYY-MM-DD HH', valeur], …] (moyennes horaires) :
        température extérieure, puissance du chauffage (W), température intérieure.
        Entre deux moyennes horaires consécutives (centrées sur h+½ et h+1½), la maison a reçu
        ~(P[h]+P[h+1])/2 Wh. On garde les heures de chauffe (oct. → avril, < 16 °C dehors) et la nuit
        (pas d'apport du soleil par les fenêtres, qui fausserait l'apprentissage)."""
        te = {k: v for k, v in text if v is not None}
        pw = {k: v for k, v in power if v is not None}
        ph = {k: v for k, v in (house or []) if v is not None}      # part « toute la maison » (chauffage zoné)
        if ph and len(ph) < 0.5 * len(pw):
            ph = {}                       # découpage absent ou trop incomplet : on garde la puissance totale
        ti = {k: v for k, v in tint if v is not None}
        out = []
        W = cls.WINDOW_H
        for k in sorted(ti):
            try:
                dt = datetime.strptime(k, '%Y-%m-%d %H')
            except ValueError:
                continue
            if dt.month not in cls.HEAT_MONTHS:
                continue
            ks = [(dt + timedelta(hours=i)).strftime('%Y-%m-%d %H') for i in range(W + 1)]
            hours_ = [(dt + timedelta(hours=i)).hour for i in range(W + 1)]
            if any(h not in cls.NIGHT_HOURS for h in hours_):
                continue
            if any(x not in ti or x not in te or x not in pw for x in ks):
                continue
            t0, t1 = ti[ks[0]], ti[ks[-1]]
            tex = sum(te[x] for x in ks) / len(ks)
            # énergie entre les deux moyennes horaires (centrées sur h+½ et h+W+½) : demi-heures aux bouts
            e = max(0.0, pw[ks[0]] / 2 + sum(pw[x] for x in ks[1:-1]) + pw[ks[-1]] / 2) / 1000.0
            ef = 0.0
            if ph:
                if any(x not in ph for x in ks):
                    continue
                eh = max(0.0, ph[ks[0]] / 2 + sum(ph[x] for x in ks[1:-1]) + ph[ks[-1]] / 2) / 1000.0
                e, ef = eh, max(0.0, e - eh)                    # maison entière / étage seul
            if tex >= 16 or abs(t1 - t0) > 3 or not (5 < t0 < 35):
                continue
            out.append([dt.isoformat(timespec='minutes'), round(t0, 2), round(t1, 2), round(tex, 2), round(e, 4),
                        float(W), 0.0, 0.0, 0, round(ef, 4)])
        return out[-cls.MAX_HIST:]

    def seed_history(self, samples: list, info: dict):
        self.hist_samples = samples[-self.MAX_HIST:]
        self.hist_info = info
        try:
            with open(self._hist_file, 'w') as fh:
                json.dump({'samples': self.hist_samples, 'info': info}, fh)
        except OSError as e:
            log.warning('Sauvegarde historique thermique : %s', e)
        self.fit()

    # ── mesures ───────────────────────────────────────────────────
    def update(self, now: datetime, tint: Optional[float], text: Optional[float], pac_w: float, mode: str,
               vmi_w: float = 0.0, attic_rate: float = 0.0, vmi_pct: float = 0.0):
        """Appelé à chaque cycle, en mode chauffage seulement.
        vmi_w / vmi_pct : puissance et consigne 0-10 V (en %) de la résistance VMI ;
        attic_rate : (T_combles − T_int) × part du ventilateur quand la VMI souffle l'air des combles."""
        if tint is None or text is None or mode != 'heat':
            self._cur = None
            return
        if self._cur is None:
            self._cur = {'t0': now, 'T0': tint, 'E': 0.0, 'Ef': 0.0, 'Ev': 0.0, 'Evp': 0.0, 'X': 0.0, 'text': [text], 'last': now}
            return
        dt = (now - self._cur['last']).total_seconds()
        if dt <= 0 or dt > 120:          # trou de mesures : on recommence
            self._cur = None
            return
        c = self._cur
        ev = max(0.0, vmi_w or 0.0) * dt / 3600000.0
        pw = max(0.0, pac_w or 0.0)
        sh = self.house_share(pw)
        c['E'] += pw * sh * dt / 3600000.0                 # toute la maison
        c['Ef'] = c.get('Ef', 0.0) + pw * (1 - sh) * dt / 3600000.0   # étage seul
        c['Ev'] += ev
        c['Evp'] += ev * max(0.0, min(100.0, vmi_pct or 0.0))
        c['X'] += max(0.0, attic_rate or 0.0) * dt / 3600.0
        c['text'].append(text)
        c['last'] = now
        if (now - c['t0']).total_seconds() >= self.STEP_S:
            dt_h = (now - c['t0']).total_seconds() / 3600.0
            pct = c['Evp'] / c['Ev'] if c['Ev'] > 1e-6 else 0.0
            self.samples.append([c['t0'].isoformat(timespec='minutes'), round(c['T0'], 2), round(tint, 2),
                                 round(sum(c['text']) / len(c['text']), 2), round(c['E'], 4), round(dt_h, 3),
                                 round(c['Ev'], 4), round(c['X'], 3), round(pct), round(c.get('Ef', 0.0), 4)])
            self._cur = {'t0': now, 'T0': tint, 'E': 0.0, 'Ef': 0.0, 'Ev': 0.0, 'Evp': 0.0, 'X': 0.0, 'text': [text], 'last': now}
            if len(self.samples) % 36 == 0:          # toutes les ~6 h
                self.fit()
                self.save()

    # ── apprentissage ─────────────────────────────────────────────
    @staticmethod
    def _lstsq(rows: list, k: int) -> Optional[list]:
        """Moindres carrés sans constante : y = Σ coef_i · x_i (rows = [(y, x1..xk)])."""
        A = [[sum(r[i + 1] * r[j + 1] for r in rows) for j in range(k)] + [sum(r[0] * r[i + 1] for r in rows)]
             for i in range(k)]
        for i in range(k):                                  # élimination de Gauss
            piv = max(range(i, k), key=lambda r: abs(A[r][i]))
            if abs(A[piv][i]) < 1e-12:
                return None
            A[i], A[piv] = A[piv], A[i]
            for r in range(k):
                if r != i:
                    f = A[r][i] / A[i][i]
                    A[r] = [x - f * y for x, y in zip(A[r], A[i])]
        return [A[i][k] / A[i][i] for i in range(k)]

    @staticmethod
    def _rows(samples: list) -> list:
        """(ΔT, E_pac maison, −(T−Text)·Δt, E_vmi, X_combles, Text, pct, Δt, E_pac étage seul)"""
        out = []
        for s in samples:
            _, t0, t1, te, e, dth = s[:6]
            ev = s[6] if len(s) > 6 else 0.0
            x = s[7] if len(s) > 7 else 0.0
            pct = s[8] if len(s) > 8 else (100.0 if ev > 0 else 0.0)
            ef = s[9] if len(s) > 9 else 0.0
            out.append((t1 - t0, e, -((t0 + t1) / 2 - te) * dth, ev, x, te, pct, dth, ef))
        return out

    def _fit_rows(self, rows: list, use_e: bool, use_v: bool, use_g: bool, g_fixed: Optional[float] = None,
                  k_fixed: Optional[float] = None, use_f: bool = False):
        # apports gratuits (col. 7) : appris sur l'ensemble (températures extérieures variées), puis FIXÉS par
        # tranche — dans une tranche étroite, « perd beaucoup » et « reçoit beaucoup » se confondent
        cols = ([1] if use_e else []) + [2] + ([3] if use_v else []) + ([4] if use_g else []) + ([7] if k_fixed is None else []) \
            + ([8] if use_f else [])
        data = [tuple([r[0] - (g_fixed * r[4] if g_fixed else 0.0) - ((k_fixed or 0.0) * r[7])]
                      + [r[i] for i in cols]) for r in rows]
        coef = self._lstsq(data, len(cols))
        if not coef:
            return None
        res = dict(zip(cols, coef))
        ym = sum(d[0] for d in data) / len(data)
        ss_tot = sum((d[0] - ym) ** 2 for d in data) or 1e-9
        ss_res = sum((d[0] - sum(cf * x for cf, x in zip(coef, d[1:]))) ** 2 for d in data)
        return {'a': res.get(1), 'b': res.get(2), 'c': res.get(3), 'g': res.get(4), 'k': res.get(7), 'f': res.get(8),
                'r2': max(0.0, 1 - ss_res / ss_tot)}

    def fit(self):
        rows = self._rows(self.hist_samples + self.samples)
        if len(rows) < 50:
            return
        use_e = sum(1 for r in rows if r[1] > 0.02) >= 30          # PAC mesurée
        use_v = sum(1 for r in rows if r[3] > 0.02) >= self.MIN_VMI_SAMPLES
        use_g = sum(1 for r in rows if r[4] > 0.05) >= self.MIN_ATTIC_SAMPLES
        use_f = sum(1 for r in rows if r[8] > 0.02) >= 30          # chauffage de l'étage seul
        f = self._fit_rows(rows, use_e, use_v, use_g, use_f=use_f)
        if not f or (f['b'] or 0) <= 0 or (use_e and (f['a'] or 0) <= 0):
            log.info('Modèle thermique : résultats incohérents, on attend plus de données')
            return
        self.a = f['a'] if use_e else None
        self.b, self.r2 = f['b'], f['r2']
        self.k = max(0.0, f.get('k') or 0.0)      # apports gratuits (°C/h)
        self.f = max(0.0, f.get('f') or 0.0) if use_f else None   # l'étage seul réchauffe un peu le salon
        self.c = f['c'] if (f['c'] is not None and f['c'] > 0) else None
        self.g = f['g'] if (f['g'] is not None and f['g'] >= 0) else None
        # par tranche de température extérieure (air des combles fixé au global)
        bands = {}
        for lo, hi, name in BANDS:
            br = [r for r in rows if lo <= r[5] < hi]
            if len(br) < self.MIN_BAND:
                continue
            be = use_e and sum(1 for r in br if r[1] > 0.02) >= 20
            bv = use_v and sum(1 for r in br if r[3] > 0.02) >= self.MIN_VMI_BAND
            bf = use_f and sum(1 for r in br if r[8] > 0.02) >= 20
            fb = self._fit_rows(br, be, bv, False, g_fixed=self.g, k_fixed=self.k, use_f=bf)
            if not fb or (fb['b'] or 0) <= 0 or (be and (fb['a'] or 0) <= 0):
                continue
            bands[name] = {'a': fb['a'] if be else None, 'b': fb['b'],
                           'c': fb['c'] if (bv and (fb['c'] or 0) > 0) else None, 'n': len(br)}
        self.bands = bands
        # résistance VMI par palier de puissance (0-10 V) : le reste est expliqué par le modèle
        # (facteur relatif : 1 = rendement moyen de la résistance, 0,8 = 20 % de moins à ce palier)
        vp = {}
        if self.c:
            def c_of(pr):
                num = den = 0.0
                for r in pr:
                    a, b = self._a_learned(r[5]) or 0.0, self._b_learned(r[5])
                    resid = r[0] - a * r[1] - b * r[2] - (self.g or 0.0) * r[4] - self._k_learned(r[5]) * r[7] \
                        - (self.f or 0.0) * r[8]
                    num += resid * r[3]
                    den += r[3] * r[3]
                return num / den if den > 0 and num > 0 else None
            c_ref = c_of([r for r in rows if r[3] > 0.02])
            for lo, hi, name in POWER_BANDS:
                pr = [r for r in rows if r[3] > 0.02 and lo <= r[6] < hi]
                cp = c_of(pr) if len(pr) >= self.MIN_VMI_BAND else None
                if cp and c_ref:
                    vp[name] = cp / c_ref
        self.vmi_power = vp
        self.export_learned(os.path.dirname(self._file))
        log.info('Modèle thermique : %s, constante de temps %.0f h (r²=%.2f, %d mesures, %d tranches apprises)%s',
                 f'{1 / self.a:.1f} kWh PAC pour +1 °C' if self.a else 'PAC pas encore mesurée',
                 1 / self.b, self.r2, len(rows), len(bands),
                 f', VMI {1 / self.c:.1f} kWh pour +1 °C' if self.c else '')

    # ── valeurs saisies à la main (configuration du plugin) ────────
    MANUAL_TEMPS = (-10, -5, 0, 5, 10, 15)
    MANUAL_TINT = 20.0          # la descente est donnée pour une maison à 20 °C
    MIN_NEAR = 30               # mesures à ±2,5 °C nécessaires pour proposer la valeur apprise d'une ligne

    def set_zone(self, g: dict):
        """Chauffage zoné : sous « bas » W seul l'étage chauffe, au-dessus de « haut » W toute la maison."""
        try:
            lo, hi = float(g.get('heat_floor_w', 500) or 0), float(g.get('heat_whole_w', 1000) or 0)
        except (TypeError, ValueError):
            lo, hi = 0.0, 0.0
        self.zone = (lo, hi) if 0 < lo < hi else None

    def house_share(self, w: float) -> float:
        """Part de la puissance qui chauffe TOUTE la maison (le reste ne chauffe que l'étage)."""
        if not self.zone:
            return 1.0
        lo, hi = self.zone
        return max(0.0, min(1.0, (w - lo) / (hi - lo)))

    def set_manual(self, g: dict):
        """Tableau « Chauffage : valeurs connues » : kWh pour +1 °C et descente (°C/h) par température extérieure."""
        out = {'kwh': {}, 'drop': {}}
        for t in self.MANUAL_TEMPS:
            tag = f'm{abs(t)}' if t < 0 else str(t)
            for col in ('kwh', 'drop'):
                try:
                    v = float(str(g.get(f'heat_{col}_{tag}', '')).replace(',', '.'))
                except ValueError:
                    continue
                if v > 0:
                    out[col][float(t)] = v
        if out != self.manual:
            self.manual = out
            if out['kwh'] or out['drop']:
                log.info('Chauffage : valeurs manuelles — %s', ' ; '.join(
                    f"{t:+.0f} °C : " + ' · '.join(x for x in (
                        f"{out['kwh'][t]:.1f} kWh/°C" if t in out['kwh'] else '',
                        f"{out['drop'][t]:.2f} °C/h" if t in out['drop'] else '') if x)
                    for t in sorted(set(out['kwh']) | set(out['drop']))))

    def _manual(self, col: str, text: Optional[float]) -> Optional[float]:
        """Valeur manuelle interpolée entre les températures renseignées (la plus proche hors bornes)."""
        pts = sorted(self.manual.get(col, {}).items())
        if not pts or text is None:
            return None
        if text <= pts[0][0]:
            return pts[0][1]
        if text >= pts[-1][0]:
            return pts[-1][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= text <= x1:
                return y0 + (y1 - y0) * (text - x0) / (x1 - x0)
        return None

    # ── valeurs selon la température extérieure (manuel d'abord, sinon appris) ──
    def _a_learned(self, text: Optional[float]) -> Optional[float]:
        bd = self.bands.get(_band(text)) or {}
        return bd.get('a') or self.a

    def _b_learned(self, text: Optional[float]) -> Optional[float]:
        bd = self.bands.get(_band(text)) or {}
        return bd.get('b') or self.b

    def _k_learned(self, text: Optional[float] = None) -> float:
        return self.k or 0.0

    def a_at(self, text: Optional[float]) -> Optional[float]:
        kwh = self._manual('kwh', text)
        return 1.0 / kwh if kwh else self._a_learned(text)

    def b_at(self, text: Optional[float]) -> Optional[float]:
        drop = self._manual('drop', text)
        if drop and text is not None:
            return drop / max(1.0, self.MANUAL_TINT - text)     # descente donnée nette : pas d'apports à part
        return self._b_learned(text)

    def c_at(self, text: Optional[float], pct: Optional[float] = None) -> Optional[float]:
        """°C par kWh de résistance VMI, selon le froid dehors et la puissance demandée."""
        if not self.c:
            return None
        c = self.c
        bd = self.bands.get(_band(text)) or {}
        if bd.get('c'):
            c = bd['c']
        pf = self.vmi_power.get(_band(pct, POWER_BANDS)) if pct is not None else None
        if pf:
            c *= pf
        return c

    # ── utilisation ───────────────────────────────────────────────
    def _learned_ok(self) -> bool:
        n = len(self.samples) + len(self.hist_samples) * 6     # 1 mesure horaire ≈ 6 mesures de 10 min
        return self.b is not None and n >= self.MIN_SAMPLES and self.r2 >= self.MIN_R2

    def reliable(self) -> bool:
        """Chauffage connu : valeurs manuelles, ou modèle appris assez précis."""
        a_ok = bool(self.manual['kwh']) or (self.a is not None and self._learned_ok())
        return a_ok and self.house_reliable()

    def house_reliable(self) -> bool:
        """Refroidissement de la maison connu (manuel, ou appris assez précis)."""
        return bool(self.manual['drop']) or self._learned_ok()

    def vmi_ratio(self, text: Optional[float] = None, pct: Optional[float] = None) -> Optional[float]:
        """kWh de PAC évités par kWh de résistance VMI (mesuré), None tant que ce n'est pas appris."""
        c, a = self.c_at(text, pct), self.a_at(text)
        if not self.reliable() or not c or not a:
            return None
        return max(0.0, min(3.0, c / a))

    def attic_free_kwh_today(self) -> Optional[float]:
        """Chaleur apportée aujourd'hui par l'air des combles, en kWh de PAC évités."""
        if not self.reliable() or self.g is None:
            return None
        today = datetime.now().strftime('%Y-%m-%d')
        tot = sum(self.g * s[7] / (self.a_at(s[3]) or self.a) for s in self.samples
                  if len(s) > 7 and s[0].startswith(today))
        return round(tot, 1)

    def keep_factor(self, text: Optional[float], hours: float) -> float:
        """Part de la chaleur stockée qui reste après `hours` heures."""
        b = self.b_at(text)
        return math.exp(-b * max(0.0, hours)) if b else 1.0

    def k_at(self, text: Optional[float]) -> float:
        return 0.0 if self._manual('drop', text) else self._k_learned(text)

    def coast_hours(self, t_start: float, t_floor: float, text: float) -> Optional[float]:
        """Heures sans chauffer pour descendre de t_start à t_floor. Sans chauffage, la maison tend vers
        T_ext + apports/b (les apports gratuits la tiennent un peu au-dessus de l'extérieur)."""
        b = self.b_at(text)
        if not b:
            return None
        t_inf = text + self.k_at(text) / b
        if t_start <= t_floor or t_floor <= t_inf:
            return 0.0 if t_start <= t_floor else 99.0
        return math.log((t_start - t_inf) / (t_floor - t_inf)) / b

    def hold_kwh_per_h(self, t_set: float, text: float) -> Optional[float]:
        """kWh électriques par heure pour maintenir t_set (pertes moins apports gratuits, compensées par la PAC)."""
        a, b = self.a_at(text), self.b_at(text)
        if not (a and b):
            return None
        return max(0.0, (b * (t_set - text) - self.k_at(text)) / a)

    def storage_value(self, boost_c: float, t_set: float, text: float,
                      hours_to_evening: float) -> Optional[dict]:
        """Ce que rapporte +boost_c °C stockés maintenant, une fois le soleil parti."""
        if not self.reliable():
            return None
        kwh_in = boost_c / self.a_at(text)
        keep = self.keep_factor(text, hours_to_evening)       # la chaleur en plus s'échappe avant le soir
        gain_h = self.coast_hours(t_set + boost_c * keep, t_set, text) or 0.0
        return {'kwh_stockes': round(kwh_in, 1), 'reste_le_soir_pct': round(keep * 100),
                'heures_sans_chauffer_le_soir': round(gain_h, 1),
                'kwh_pac_evites_le_soir': round(min(kwh_in * keep, gain_h * (self.hold_kwh_per_h(t_set, text) or 0)), 1)}

    def table(self, tint: float = 20.0) -> list:
        """Ce qui est appris, tranche par tranche de température extérieure."""
        out = []
        for lo, hi, name in BANDS:
            bd = self.bands.get(name)
            mid = max(lo, -5.0) if hi <= 0 else (lo + 2.5 if lo >= 15 else (lo + hi) / 2)
            a = (bd or {}).get('a') or self.a
            b = (bd or {}).get('b') or self.b
            c = self.c_at(mid)
            row = {'dehors': name, 'appris': bool(bd), 'mesures': (bd or {}).get('n', 0)}
            if a:
                row['tone_kwh_pour_1c'] = round(1 / a, 1)
            if c:
                row['vmi_kwh_pour_1c'] = round(1 / c, 1)
            if b:
                row['perte_c_par_h'] = round(max(0.0, b * (tint - mid) - (self.k or 0.0)), 2)   # sans chauffer
            out.append(row)
        return out

    def vmi_power_table(self, text: Optional[float] = None) -> list:
        text = 5.0 if text is None else text
        return [{'puissance': name, 'vmi_kwh_pour_1c': round(1 / self.c_at(text, (lo + hi) / 2), 1)}
                for lo, hi, name in POWER_BANDS if name in self.vmi_power and self.c_at(text, (lo + hi) / 2)]


    def export_learned(self, datadir: str):
        """Valeurs APPRISES aux températures du tableau manuel (fichier lu par Jeedom pour le pré-remplir)."""
        # seulement les températures réellement mesurées (tranche apprise) : pas de moyenne extrapolée
        rows = []
        if self.b is not None:
            texts = [x[3] for x in self.hist_samples + self.samples]
            for t in self.MANUAL_TEMPS:
                near = sum(1 for x in texts if abs(x - t) <= 2.5)     # mesures à ±2,5 °C de cette température
                if not self.bands.get(_band(t)) or near < self.MIN_NEAR:
                    continue
                r = {'t': t, 'heures': near}
                a, b = self._a_learned(t), self._b_learned(t)
                if a:
                    r['kwh'] = round(1.0 / a, 1)
                if b:
                    r['drop'] = round(max(0.0, b * (self.MANUAL_TINT - t) - self._k_learned(t)), 2)
                rows.append(r)
        out = {'r2': round(self.r2, 2), 'fiable': self._learned_ok(), 'r2_mini': self.MIN_R2,
               'heures': len(self.hist_samples), 'rows': rows}
        try:
            with open(os.path.join(datadir, 'thermal_table.json'), 'w') as fh:
                json.dump(out, fh)
        except OSError as e:
            log.warning('Export du tableau appris : %s', e)

    def text(self, tint: float = 20.0) -> list:
        """Lignes en clair pour Jeedom."""
        lines = []
        if self.manual['kwh'] or self.manual['drop']:
            lines.append('Valeurs manuelles utilisées : ' + ' ; '.join(
                f"{t:+.0f} °C " + ' · '.join(x for x in (
                    f"{self.manual['kwh'][t]:.1f} kWh/°C" if t in self.manual['kwh'] else '',
                    f"descente {self.manual['drop'][t]:.2f} °C/h" if t in self.manual['drop'] else '') if x)
                for t in sorted(set(self.manual['kwh']) | set(self.manual['drop']))))
        for r in self.table(tint):
            if not r['appris']:
                continue
            parts = []
            if 'tone_kwh_pour_1c' in r:
                parts.append(f"T-One {r['tone_kwh_pour_1c']:.1f} kWh pour +1 °C")
            if 'vmi_kwh_pour_1c' in r:
                parts.append(f"VMI {r['vmi_kwh_pour_1c']:.1f} kWh pour +1 °C")
            if 'perte_c_par_h' in r:
                parts.append(f"la maison perd {r['perte_c_par_h']:.2f} °C/h")
            lines.append(f"Dehors {r['dehors']} : " + ' · '.join(parts))
        for r in self.vmi_power_table():
            lines.append(f"VMI réglée {r['puissance']} : {r['vmi_kwh_pour_1c']:.1f} kWh pour +1 °C")
        if self.f is not None and self.a:
            lines.append(f"Étage seul (chauffage zoné) : son effet sur le salon vaut {100 * self.f / self.a:.0f} % "
                         f"de celui de toute la maison")
        if self.hist_samples:
            lines.insert(0, f"Historique : {len(self.hist_samples)} heures de chauffe apprises "
                            f"({self.hist_samples[0][0][:10]} → {self.hist_samples[-1][0][:10]})")
        if not lines:
            lines.append(f"Chauffage : en apprentissage ({len(self.samples)} mesures de 10 min, "
                         f"~10 h par tranche de température extérieure)")
        return [x.replace('.', ',') for x in lines]
