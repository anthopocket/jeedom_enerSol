"""Modèle de consommation de la maison en fonction de l'heure et de la température.

Pour chaque heure h : conso_Wh = a_h + b_h·DJU_chauffe + c_h·DJU_clim
  DJU_chauffe = max(0, 16 − T_ext)   DJU_clim = max(0, T_ext − 24)
Les coefficients sont appris par moindres carrés (régularisés) sur l'historique
Jeedom (moyennes horaires de la conso et de la température extérieure), puis
mis à jour avec les mesures du démon. Sans température : moyenne par heure.

En plus, un facteur appris par heure corrige selon le TYPE DE JOUR (semaine / week-end)
et la PRÉSENCE (maison occupée / vide) : conso = modèle × facteur[type][heure].
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger('enerSol.consumption')

T_HEAT = 16.0
T_COOL = 24.0


def _features(t: Optional[float]):
    if t is None:
        return 1.0, 0.0, 0.0
    return 1.0, max(0.0, T_HEAT - t), max(0.0, t - T_COOL)


def _solve3(A, b):
    M = [A[i][:] + [b[i]] for i in range(3)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        if abs(M[col][col]) < 1e-9:
            return None
        for r in range(col + 1, 3):
            f = M[r][col] / M[col][col]
            for j in range(col, 4):
                M[r][j] -= f * M[col][j]
    x = [0.0] * 3
    for i in range(2, -1, -1):
        x[i] = (M[i][3] - sum(M[i][j] * x[j] for j in range(i + 1, 3))) / M[i][i]
    return x


class ConsumptionModel:

    MAX_DAYS = 60
    RIDGE = 2.0

    def __init__(self, datadir: str):
        self._file = os.path.join(datadir, 'load_model.json')
        self.samples: dict = {}    # {'YYYY-MM-DD HH': [wh, t]}
        self.coefs: dict = {}      # {h: [a, b, c]}
        self.n_samples = 0
        self._cur_key = ''
        self._acc_wh = 0.0
        self._acc_t = []
        self._acc_p: list = []
        self.factors: dict = {}    # {'semaine'|'weekend'|'absent': {h: facteur}}
        self._last_ts: Optional[datetime] = None
        self._load()

    def _load(self):
        try:
            with open(self._file) as fh:
                d = json.load(fh)
            self.samples = d.get('samples', {})
            self.fit()
        except Exception:  # noqa: BLE001
            pass

    def save(self):
        cutoff = (datetime.now() - timedelta(days=self.MAX_DAYS)).strftime('%Y-%m-%d %H')
        self.samples = {k: v for k, v in self.samples.items() if k >= cutoff}
        try:
            with open(self._file, 'w') as fh:
                json.dump({'samples': self.samples, 'coefs': self.coefs}, fh)
        except OSError as e:
            log.warning('Sauvegarde modèle conso : %s', e)

    # ── données ───────────────────────────────────────────────────
    def import_history(self, load_series: list, temp_series: list):
        """Séries Jeedom horaires [['YYYY-MM-DD HH', moyenne], ...] (W moyens = Wh)."""
        temps = {k: v for k, v in (temp_series or [])}
        n = 0
        for k, w in load_series or []:
            if w is None or w < 0:
                continue
            self.samples[k] = [float(w), temps.get(k)]
            n += 1
        log.info('Historique conso importé : %d heures', n)
        self.fit()
        self.save()

    def record(self, now: datetime, load_w: float, text: Optional[float], present: Optional[bool] = None):
        key = now.strftime('%Y-%m-%d %H')
        if self._last_ts is not None and key == self._cur_key:
            dt_h = min(0.1, (now - self._last_ts).total_seconds() / 3600.0)
            self._acc_wh += max(0.0, load_w) * dt_h
            if text is not None:
                self._acc_t.append(text)
            if present is not None:
                self._acc_p.append(1 if present else 0)
        if key != self._cur_key:
            if self._cur_key and self._acc_wh > 0:
                t = sum(self._acc_t) / len(self._acc_t) if self._acc_t else None
                p = (1 if sum(self._acc_p) * 2 >= len(self._acc_p) else 0) if self._acc_p else None
                self.samples[self._cur_key] = [round(self._acc_wh, 1), t, p]
                if now.hour == 3:
                    self.fit()
                    self.save()
            self._cur_key, self._acc_wh, self._acc_t, self._acc_p = key, 0.0, [], []
        self._last_ts = now

    # ── apprentissage ─────────────────────────────────────────────
    def fit(self):
        by_hour: dict = {h: [] for h in range(24)}
        for k, v in self.samples.items():
            wh, t = v[0], v[1]
            try:
                by_hour[int(k[11:13])].append((wh, t))
            except (ValueError, IndexError):
                continue
        coefs = {}
        for h, rows in by_hour.items():
            if not rows:
                continue
            mean = sum(r[0] for r in rows) / len(rows)
            rows_t = [r for r in rows if r[1] is not None]
            if len(rows_t) < 10:
                coefs[h] = [mean, 0.0, 0.0]
                continue
            A = [[0.0] * 3 for _ in range(3)]
            bvec = [0.0] * 3
            for wh, t in rows_t:
                x = _features(t)
                for i in range(3):
                    bvec[i] += x[i] * wh
                    for j in range(3):
                        A[i][j] += x[i] * x[j]
            A[1][1] += self.RIDGE
            A[2][2] += self.RIDGE
            sol = _solve3(A, bvec)
            if sol is None:
                coefs[h] = [mean, 0.0, 0.0]
            else:
                a, b, c = sol
                coefs[h] = [max(0.0, a), max(0.0, b), max(0.0, c)]
        self.coefs = coefs
        self.n_samples = len(self.samples)
        self._fit_factors()

    @staticmethod
    def day_type(dt: datetime, present: Optional[bool] = None) -> str:
        if present is False:
            return 'absent'
        return 'weekend' if dt.weekday() >= 5 else 'semaine'

    def _fit_factors(self):
        """Écart moyen réel / modèle par type de jour et par heure (borné 0,5 – 1,8)."""
        acc: dict = {}
        for k, v in self.samples.items():
            try:
                dt = datetime.strptime(k, '%Y-%m-%d %H')
            except ValueError:
                continue
            p = v[2] if len(v) > 2 else None
            base = self._base(dt, v[1])
            if base <= 50:
                continue
            key = (self.day_type(dt, None if p is None else bool(p)), dt.hour)
            acc.setdefault(key, []).append(v[0] / base)
        f: dict = {}
        for (typ, h), r in acc.items():
            if len(r) >= 4:
                r = sorted(r)[len(r) // 10: len(r) - len(r) // 10 or None]      # sans les extrêmes
                f.setdefault(typ, {})[h] = round(max(0.5, min(1.8, sum(r) / len(r))), 2)
        self.factors = f

    def _base(self, dt: datetime, t: Optional[float]) -> float:
        c = self.coefs.get(dt.hour)
        if not c:
            vals = [v[0] for v in self.coefs.values()]
            return sum(vals) / len(vals) if vals else 500.0
        x = _features(t)
        return max(50.0, c[0] * x[0] + c[1] * x[1] + c[2] * x[2])

    def profile(self) -> dict:
        """Résumé : écart des week-ends et des absences par rapport à la semaine."""
        out = {}
        for typ, hs in self.factors.items():
            if hs:
                out[typ] = {'facteur_moyen': round(sum(hs.values()) / len(hs), 2),
                            'soir_18_23h': round(sum(hs.get(h, 1) for h in range(18, 24)) / 6, 2)}
        return out

    # ── prévision ─────────────────────────────────────────────────
    def predict(self, dt: datetime, t: Optional[float], present: Optional[bool] = None) -> float:
        f = (self.factors.get(self.day_type(dt, present)) or {}).get(dt.hour, 1.0)
        return max(50.0, self._base(dt, t) * f)

    def heating_wh(self, dt: datetime, t: Optional[float]) -> float:
        """Part « chauffage » (Wh) de la consommation prévue à cette heure."""
        c = self.coefs.get(dt.hour)
        if not c or t is None:
            return 0.0
        return c[1] * max(0.0, T_HEAT - t)

    def heating_learned(self) -> bool:
        """Le modèle a-t-il appris un effet du froid (assez d'heures avec température et froid) ?"""
        n = sum(1 for v in self.samples.values() if v[1] is not None and v[1] < T_HEAT - 3)
        return n >= 48 and self.heating_sensitivity() > 0

    def heating_sensitivity(self) -> float:
        """Wh supplémentaires par jour pour 1 °C de moins."""
        return round(sum(c[1] for c in self.coefs.values()), 0)
