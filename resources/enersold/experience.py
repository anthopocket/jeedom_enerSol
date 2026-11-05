"""Expérience : ce que rapporte VRAIMENT 1 kWh de soleil donné à chaque appareil, mesuré sur les compteurs.

Unité commune : « kWh évités plus tard » (achetés au réseau ou pris à la batterie) pour 1 kWh de soleil.

  - Chauffe-eau (thermodynamique ou non) : on suit son compteur par « cycle » = la journée solaire
    puis la nuit qui suit. Si plus il chauffe au soleil, moins il consomme ensuite, la pente le dit :
        conso_hors_soleil = c − v · conso_au_soleil        →   v = rendement appris
    (1 = chaque kWh au soleil évite 1 kWh plus tard ; 0,3 = le ballon était déjà presque chaud).
    Le compteur dit aussi quand le ballon est « rassasié » (en Auto, il ne consomme plus) et combien
    il consomme d'habitude par cycle → ce qu'il lui reste à chauffer aujourd'hui.

  - VMI (résistance) : le modèle thermique apprend séparément les °C apportés par 1 kWh de PAC et par
    1 kWh de résistance VMI. 1 kWh de résistance VMI évite (a_vmi / a_pac) kWh de PAC.
    L'air des combles est appris de la même façon (chaleur gratuite).

  - Surchauffe PAC : 1 kWh stocké évite ~1 kWh de PAC ce soir, moins ce qui s'échappe avant (modèle thermique).
"""

import json
import logging
import os
import statistics
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger('enerSol.experience')


class Experience:
    MAX_CYCLES = 45
    MIN_CYCLES = 7               # au moins une semaine de mesures avant de croire la pente
    PRIOR_WATER = 0.8            # valeurs par défaut tant que rien n'est appris
    PRIOR_COP_PAC = 3.0          # la PAC fait ~3 kWh de chaleur pour 1 kWh électrique
    RES_W = 1000                 # au-dessus : résistance du chauffe-eau
    SATED_W = 30                 # en Auto et sous ce seuil : ballon chaud
    SATED_S = 40 * 60

    def __init__(self, datadir: str):
        self._file = os.path.join(datadir, 'experience.json')
        self.cycles: list = []          # [{date, sol_kwh, off_kwh, res_sol_kwh, res_off_kwh}]
        self.cur: dict = {}
        self.vmi_curve: dict = {}       # '10', '20' … '100' (% en 0-10 V) → W mesurés au compteur
        self._low_since = 0.0
        self._saved = 0.0
        self._load()

    # ── persistance ───────────────────────────────────────────────
    def _load(self):
        try:
            with open(self._file) as fh:
                d = json.load(fh)
            self.cycles = d.get('cycles', [])
            self.cur = d.get('cur', {}) or {}
            self.vmi_curve = d.get('vmi_curve', {}) or {}
        except Exception:  # noqa: BLE001
            pass

    def save(self):
        try:
            with open(self._file, 'w') as fh:
                json.dump({'cycles': self.cycles[-self.MAX_CYCLES:], 'cur': self.cur, 'vmi_curve': self.vmi_curve}, fh, ensure_ascii=False)
        except OSError as e:
            log.warning('Sauvegarde expérience : %s', e)

    # ── chauffe-eau : mesures ─────────────────────────────────────
    def water_tick(self, now: datetime, power_w: float, in_solar: bool, auto: Optional[bool], cycle_s: float):
        """Appelé à chaque cycle de 10 s avec le compteur du chauffe-eau."""
        p = max(0.0, power_w or 0.0)
        c = self.cur
        # un nouveau cycle commence avec le soleil
        if in_solar and c.get('phase') == 'off':
            self._close()
            c = self.cur
        if not c:
            c.update(date=now.strftime('%Y-%m-%d'), phase='sol' if in_solar else 'off',
                     sol=0.0, off=0.0, res_sol=0.0, res_off=0.0, sated=None, after_sated=0.0)
        if in_solar:
            c['phase'] = 'sol'
        elif c.get('phase') == 'sol':
            c['phase'] = 'off'
        wh = p * cycle_s / 3600.0
        k = 'sol' if c['phase'] == 'sol' else 'off'
        c[k] += wh
        if p >= self.RES_W:
            c['res_' + k] += wh
        if c.get('sated'):
            c['after_sated'] += wh
        # ballon rassasié : en Auto, au soleil, plus rien consommé pendant 40 min
        if in_solar and auto and p < self.SATED_W:
            self._low_since = self._low_since or time.monotonic()
            if time.monotonic() - self._low_since >= self.SATED_S and not c.get('sated'):
                c['sated'] = now.strftime('%H:%M')
                c['after_sated'] = 0.0
                log.info('Chauffe-eau : ballon chaud à %s (%.1f kWh pris au soleil)', c['sated'], c['sol'] / 1000)
        else:
            self._low_since = 0.0
        if time.monotonic() - self._saved >= 900:
            self._saved = time.monotonic()
            self.save()

    def _close(self):
        c = self.cur
        if c and c.get('sol', 0) + c.get('off', 0) > 50:
            k = lambda x: round(x / 1000, 2)
            self.cycles.append({'date': c['date'], 'sol_kwh': k(c['sol']), 'off_kwh': k(c['off']),
                                'res_sol_kwh': k(c['res_sol']), 'res_off_kwh': k(c['res_off']),
                                'chaud_a': c.get('sated')})
            self.cycles = self.cycles[-self.MAX_CYCLES:]
            log.info('Chauffe-eau : cycle du %s → %.1f kWh au soleil, %.1f kWh ensuite', c['date'],
                     c['sol'] / 1000, c['off'] / 1000)
        self.cur = {}
        self.save()

    # ── chauffe-eau : ce qui est appris ───────────────────────────
    def water_need_kwh(self) -> Optional[float]:
        """Consommation habituelle d'un cycle (jour + nuit)."""
        tot = [c['sol_kwh'] + c['off_kwh'] for c in self.cycles[-14:]]
        return round(statistics.median(tot), 2) if len(tot) >= 3 else None

    def water_rest_kwh(self) -> Optional[float]:
        """Ce qu'il reste à chauffer aujourd'hui (0 si le ballon est déjà chaud)."""
        c = self.cur
        if c.get('sated'):
            return 0.0
        need = self.water_need_kwh()
        if need is None:
            return None
        return round(max(0.0, need - (c.get('sol', 0.0) / 1000)), 2)

    def water_value(self) -> dict:
        cyc = [c for c in self.cycles[-30:] if c['sol_kwh'] + c['off_kwh'] > 0.3]
        xs = [c['sol_kwh'] for c in cyc]
        if len(cyc) >= self.MIN_CYCLES and statistics.pvariance(xs) >= 0.1:
            ys = [c['off_kwh'] for c in cyc]
            mx, my = statistics.fmean(xs), statistics.fmean(ys)
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
            v = max(0.0, min(1.5, -slope))
            return {'valeur': round(v, 2), 'appris': True, 'jours': len(cyc),
                    'detail': f'1 kWh de soleil évite {v:.1f} kWh plus tard (mesuré sur {len(cyc)} jours)'.replace('.', ',', 1)}
        return {'valeur': self.PRIOR_WATER, 'appris': False, 'jours': len(cyc),
                'detail': f'valeur par défaut, en apprentissage ({len(cyc)}/{self.MIN_CYCLES} jours mesurés)'}

    # ── VMI : puissance réelle de la résistance selon le 0-10 V ───
    def vmi_curve_tick(self, pct: float, watts: float):
        """Compteur de la résistance, consigne stable depuis 1 min : moyenne glissante par palier de 10 %."""
        k = str(int(round(pct / 10.0) * 10))
        if k == '0' or watts is None or watts < 0:
            return
        old = self.vmi_curve.get(k)
        self.vmi_curve[k] = round(watts if old is None else old + (watts - old) * 0.05, 0)

    def vmi_watts(self, pct: float, max_w: float) -> float:
        """Puissance attendue pour une consigne (courbe apprise, sinon linéaire)."""
        pts = sorted((int(k), w) for k, w in self.vmi_curve.items())
        if len(pts) < 2:
            return pct / 100.0 * max_w
        pts = [(0, 0.0)] + pts
        if pct >= pts[-1][0]:
            return pts[-1][1] * (pct / pts[-1][0]) if pts[-1][0] < 100 else pts[-1][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= pct <= x1:
                return y0 + (y1 - y0) * (pct - x0) / max(1, x1 - x0)
        return pct / 100.0 * max_w

    def vmi_pct_for(self, watts: float, max_w: float) -> float:
        """Consigne 0-10 V (en %) pour consommer `watts` (inverse de la courbe apprise)."""
        if watts <= 0:
            return 0.0
        lo, hi = 0.0, 100.0
        if self.vmi_watts(100, max_w) <= watts:
            return 100.0
        for _ in range(20):
            mid = (lo + hi) / 2
            if self.vmi_watts(mid, max_w) < watts:
                lo = mid
            else:
                hi = mid
        return round(lo, 1)

    # ── VMI et PAC : depuis le modèle thermique ───────────────────
    def vmi_value(self, thermal, text=None, pct=None) -> dict:
        r = thermal.vmi_ratio(text, pct) if thermal is not None else None
        if r is not None:
            return {'valeur': round(r, 2), 'appris': True,
                    'detail': f'1 kWh de résistance VMI évite {r:.2f} kWh de PAC (mesuré)'.replace('.', ',', 1)}
        v = 1 / self.PRIOR_COP_PAC
        return {'valeur': round(v, 2), 'appris': False,
                'detail': 'valeur par défaut (la PAC est ~3 fois plus efficace), en apprentissage'}

    # ── pour la tuile et Jeedom ──────────────────────────────────
    def summary(self, thermal=None) -> dict:
        out = {'chauffe_eau': dict(self.water_value(), besoin_habituel_kwh=self.water_need_kwh(),
                                   reste_a_chauffer_aujourdhui_kwh=self.water_rest_kwh(),
                                   ballon_chaud_a=self.cur.get('sated'),
                                   derniers_cycles=self.cycles[-7:]),
               'vmi_resistance': self.vmi_value(thermal)}
        if self.vmi_curve:
            out['resistance_vmi_w_selon_0_10v'] = {k + ' %': w for k, w in sorted(self.vmi_curve.items(), key=lambda x: int(x[0]))}
        if thermal is not None:
            out['chauffage_par_temperature_exterieure'] = thermal.table()
            if thermal.vmi_power:
                out['vmi_par_puissance_0_10v'] = thermal.vmi_power_table()
        if thermal is not None:
            att = thermal.attic_free_kwh_today()
            if att is not None:
                out['air_des_combles'] = {'kwh_pac_evites_aujourdhui': att}
        return out

    def text(self, thermal=None) -> str:
        s = self.summary(thermal)
        w, v = s['chauffe_eau'], s['vmi_resistance']
        lines = [f"Chauffe-eau : {w['detail']}"]
        if w.get('besoin_habituel_kwh') is not None:
            rest = w.get('reste_a_chauffer_aujourdhui_kwh')
            lines.append(f"Chauffe-eau : ~{w['besoin_habituel_kwh']:.1f} kWh par jour"
                         + (f" — ballon chaud depuis {w['ballon_chaud_a']}" if w.get('ballon_chaud_a') else
                            f' — encore ~{rest:.1f} kWh aujourd\'hui' if rest else ' — besoin du jour déjà pris au soleil'))
        lines.append(f"VMI : {v['detail']}")
        if thermal is not None:
            lines += thermal.text()
        if len(self.vmi_curve) >= 2:
            pts = sorted((int(k), w) for k, w in self.vmi_curve.items())
            lines.append('Résistance VMI mesurée : ' + ' · '.join(f'{k} % = {w:.0f} W' for k, w in pts))
        if 'air_des_combles' in s:
            lines.append(f"Combles : {s['air_des_combles']['kwh_pac_evites_aujourdhui']:.1f} kWh de PAC évités aujourd'hui")
        return '\n'.join('• ' + x.replace('.', ',') for x in lines)
