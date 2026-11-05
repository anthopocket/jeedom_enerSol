"""Planificateur déterministe : prévisions → simulation → directives + conseils.

Recalculé toutes les 15 minutes.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from controller import Controller
from consumption import ConsumptionModel
from devices import pretty_program, Appliance, Snapshot
from forecast import PVForecaster
from simulator import Hour, SimResult, simulate, simulate_steps

log = logging.getLogger('enerSol.planner')

JOURS = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']


def fmt_kwh(wh: float) -> str:
    return f'{wh / 1000:.1f} kWh'


class Planner:

    HORIZON_H = 48

    def __init__(self, ctrl: Controller, pv: PVForecaster, load: ConsumptionModel):
        self.ctrl = ctrl
        self.pv = pv
        self.load = load
        self.plan: dict = {}
        self.nowcast = None
        self.plan_opt: dict = {}

    # ── horizon horaire ───────────────────────────────────────────
    def build_hours(self, snap: Snapshot, horizon: int = HORIZON_H) -> list:
        start = snap.now.replace(minute=0, second=0, microsecond=0)
        pv = self.pv.hourly(start, horizon, snap.forecast_by_hour, snap.forecast_tomorrow)
        # correction immédiate : si le soleil n'est pas au rendez-vous (nuages) ou en avance,
        # les heures qui viennent aujourd'hui sont ajustées (effet qui s'estompe sur 4 h)
        nc = self.pv.nowcast_ratio(snap.now, snap.forecast_by_hour) if snap.in_solar_window else None
        self.nowcast = nc
        hours = []
        for i, (dt, pv_wh) in enumerate(pv):
            if nc and dt.date() == snap.now.date():
                w = max(0.0, 1.0 - i / 4.0)
                pv_wh *= 1.0 + (nc[0] - 1.0) * w
            t = self.pv.temp_at(dt)
            if t is None and i == 0:
                t = snap.text
            # présence connue maintenant : appliquée aux 12 prochaines heures
            pres = snap.present if (self.ctrl.g.get('cmd_presence') and i < 12) else None
            load = self.load.predict(dt, t, pres)
            if i == 0:
                # heure en cours : seule la fraction restante compte
                frac = (60 - snap.now.minute) / 60.0
                pv_wh, load = pv_wh * frac, load * frac
                if snap.forecast_by_hour.get(dt.hour):
                    pv_wh = snap.forecast_by_hour[dt.hour] * frac * (nc[0] if nc else 1.0)
            hours.append(Hour(dt=dt, pv_wh=pv_wh, load_wh=load))
        return hours

    def run_sim(self, snap: Snapshot, hours: list) -> SimResult:
        L = self.ctrl.limits
        return simulate(snap.battery_soc, hours, L.capacity_wh, L.soc_critical,
                        L.charge_max_w, L.discharge_max_w, L.efficiency)

    # ── calcul principal ──────────────────────────────────────────
    def compute(self, snap: Snapshot) -> dict:
        hours = self.build_hours(snap)
        base = self.run_sim(snap, hours)
        # soleil net prévu (production − conso de base) heure par heure, pour la règle « batterie en avance »
        frac0 = max(0.05, (60 - snap.now.minute) / 60.0)
        self.ctrl.solar_net_by_hour = {h.dt.hour: max(0.0, h.pv_wh - h.load_wh) / (frac0 if i == 0 else 1.0)
                                       for i, h in enumerate(hours) if h.dt.date() == snap.now.date()}
        self.ctrl.pv_tomorrow_wh = None   # renseigné plus bas avec les jours
        today = snap.now.date()

        days = {}
        for k in range(4):
            d = today + timedelta(days=k)
            pv_d = self.pv.day_total(d, snap.forecast_by_hour, snap.forecast_tomorrow)
            load_d = sum(self.load.predict(datetime(d.year, d.month, d.day, h),
                                           self.pv.temp_at(datetime(d.year, d.month, d.day, h)))
                         for h in range(24))
            temps = [self.pv.temp_at(datetime(d.year, d.month, d.day, h)) for h in range(24)]
            heat_d = sum(self.load.heating_wh(datetime(d.year, d.month, d.day, h), temps[h]) for h in range(24))
            temps = [t for t in temps if t is not None]
            days[k] = {'date': d.isoformat(), 'jour': JOURS[d.weekday()],
                       'pv_wh': None if pv_d is None else round(pv_d), 'load_wh': round(load_d),
                       'balance_wh': None if pv_d is None else round(pv_d - load_d),
                       'dont_chauffage_wh': round(heat_d), 'chauffage_appris': self.load.heating_learned(),
                       'cloud_pct': self.pv.day_cloud(d),
                       't_min': min(temps) if temps else None, 't_max': max(temps) if temps else None}
        self.ctrl.pv_tomorrow_wh = days[1]['pv_wh'] if 1 in days else None
        self.ctrl.pv_today_wh = days[0]['pv_wh'] if 0 in days else None
        if snap.now.hour < 10 and days[0]['pv_wh'] is not None:
            self.pv.note_morning_prediction(today, days[0]['pv_wh'])

        night = self._night(snap, hours)
        windows = self._appliance_windows(snap, hours, base)
        # jours suivants où la machine passerait sur le surplus (bilan du jour ≥ énergie du cycle + 1 kWh
        # de marge, la batterie étant servie en premier) : un seul calcul, repris par les conseils
        for key in ('washer', 'dryer'):
            w = windows.get(key)
            if not w or w.get('decision') == 'no_program':
                continue
            need = (w.get('energy_wh') or 0) + 1000
            if not self.load.heating_learned():
                need += 3000          # chauffage pas encore appris : marge de sécurité
            w['jours_suivants_possibles'] = [days[k]['jour'] for k in (1, 2, 3)
                                             if days.get(k) and days[k]['balance_wh'] is not None
                                             and days[k]['balance_wh'] >= need]
        offsets = self._pac_home_offsets(snap, hours, base, night)

        directives = {
            'battery': {'night_reserve_soc': night['reserve_soc']},
            'washer': windows.get('washer', {}).get('directive', {}),
            'dryer': windows.get('dryer', {}).get('directive', {}),
            'water_heater': windows.get('water_heater', {}).get('directive', {}),
            'pac_home': {'consigne_offset_by_hour': offsets} if offsets else {},
        }
        # batterie qui ne se remplira pas aujourd'hui et lendemain déficitaire → pas de délestage superflu
        if snap.in_solar_window and base.soc_full_at is None and (days[1]['balance_wh'] or 0) < 0:
            directives['battery']['priority_charge'] = True
            directives['water_heater'] = {'mode': 'block'}
            directives['pac_pool'] = {'mode': 'block'}

        self.ctrl.lost_today_remaining_wh = sum(e for h, e in zip(hours, base.exports)
                                                if h.dt.date() == snap.now.date())
        opt = self._optimize(snap, hours, base, windows, offsets)
        self.plan_opt = opt
        self.last_hours, self.last_base = hours, base
        self.plan = {
            'generated_at': snap.now.isoformat(timespec='minutes'),
            'snapshot': {'soc': round(snap.battery_soc), 'pv_w': round(snap.pv_power),
                         'load_w': round(snap.load_power), 'grid_w': round(snap.grid_power),
                         'text': snap.text, 'season': snap.season},
            'days': days,
            'baseline': base.summary(hours, 24),
            'night': night,
            'windows': {k: {kk: vv for kk, vv in v.items() if kk != 'directive'} for k, v in windows.items()},
            'pac_home_offsets': offsets,
            'optimisation': opt,
            'directives_det': directives,
            'model': {'pv_hours_calibrated': self.pv.calibrated_hours(),
                      'load_samples': self.load.n_samples,
                      'heating_wh_per_degree_day': self.load.heating_sensitivity(),
                      'conso_selon_type_de_jour': self.load.profile()},
        }
        self.plan['advice'] = self._advice(days, base, night, windows)
        return self.plan

    # ── nuit / réserve ────────────────────────────────────────────
    def _night(self, snap: Snapshot, hours: list) -> dict:
        L = self.ctrl.limits
        # début : maintenant si la nuit est commencée, sinon première heure déficitaire du soir
        i0 = 0
        if snap.in_solar_window:
            i0 = next((i for i, h in enumerate(hours)
                       if h.dt.hour >= max(snap.solar_end_h, 15) and h.pv_wh < h.load_wh), len(hours))
        i1 = next((i for i in range(i0 + 1, len(hours))
                   if 5 <= hours[i].dt.hour <= 12 and hours[i].pv_wh >= hours[i].load_wh), min(len(hours), i0 + 14))
        seg = hours[i0:i1]
        deficit = sum(max(0.0, h.load_wh - h.pv_wh) for h in seg)
        eff = max(0.5, L.efficiency) ** 0.5
        reserve = L.soc_critical
        if not snap.in_solar_window:
            reserve = min(90.0, L.soc_critical + deficit / eff / max(1.0, L.capacity_wh) * 100.0)
        return {
            'start': seg[0].dt.strftime('%Hh') if seg else None,
            'end': hours[i1].dt.strftime('%Hh') if i1 < len(hours) else None,
            'deficit_wh': round(deficit),
            'reserve_soc': round(reserve, 0),
            'needed_soc_at_start': round(min(100.0, L.soc_critical + deficit / eff / max(1.0, L.capacity_wh) * 100.0)),
        }

    # ── fenêtres électroménager ───────────────────────────────────
    def _appliance_windows(self, snap: Snapshot, hours: list, base: SimResult) -> dict:
        out = {}
        for d in (self.ctrl.washer, self.ctrl.dryer):
            if not d.configured or not d.enabled or d.is_running():
                continue
            out[d.key] = self._solar_feasibility(snap, hours, base, d)
        wh = self.ctrl.water
        if wh.configured and wh.enabled and wh.solar_dump_enabled():
            good = sorted({h.dt.hour for h, e in zip(hours[:24], base.exports[:24])
                           if h.dt.date() == snap.now.date() and e > wh.nominal_power() * 0.4})
            out['water_heater'] = {'hours': good, 'directive': {'hours': good} if good else {'mode': 'block'}}
        return out

    # ── optimisation : où part l'énergie, et que déplacer ─────────
    @staticmethod
    def _day_stats(hours: list, r: SimResult, day) -> dict:
        """Surplus solaire perdu (batterie pleine : exporté ou bridé) et achats réseau d'un jour."""
        lost = buy = 0.0
        lost_h, buy_h = [], []
        for h, e, i in zip(hours, r.exports, r.imports):
            if h.dt.date() != day:
                continue
            lost += e
            buy += i
            if e >= 150:
                lost_h.append(f'{h.dt.hour}h ({e / 1000:.1f})')
            if i >= 150:
                buy_h.append(f'{h.dt.hour}h ({i / 1000:.1f})')
        return {'surplus_perdu_kwh': round(lost / 1000, 1), 'heures_surplus_perdu_kwh': lost_h,
                'achat_reseau_kwh': round(buy / 1000, 1), 'heures_achat_kwh': buy_h}

    def _optimize(self, snap: Snapshot, hours: list, base: SimResult, windows: dict, offsets: dict) -> dict:
        """Chiffre ce qui se perd (surplus) et ce qui s'achète (réseau) aujourd'hui et demain, liste
        les consommations déplaçables et propose un placement dans les heures de surplus perdu.
        La batterie est servie d'abord : un surplus « perdu » n'existe que batterie pleine."""
        today = snap.now.date()
        tomorrow = today + timedelta(days=1)
        lost = list(base.exports)
        extra = [0.0] * len(hours)
        flex, placed = [], []

        def place(name, energy, dur_h, day, earliest=None, fixed_start=None):
            """Place une charge sur le jour donné, là où elle récupère le plus de surplus perdu."""
            dur_h = max(1, int(dur_h + 0.999))
            idx = [i for i, h in enumerate(hours) if h.dt.date() == day
                   and (earliest is None or h.dt >= earliest) and 7 <= h.dt.hour <= 18]
            if fixed_start is not None:
                idx = [i for i in idx if hours[i].dt.hour == fixed_start.hour] or idx[:1]
            best, best_gain = None, -1.0
            for i in idx:
                win = range(i, min(len(hours), i + dur_h))
                gain = sum(min(lost[j], energy / dur_h) for j in win)
                if gain > best_gain + 1:
                    best, best_gain = i, gain
            if best is None:
                return None
            for j in range(best, min(len(hours), best + dur_h)):
                take = energy / dur_h
                lost[j] = max(0.0, lost[j] - take)
                extra[j] += take
            item = {'charge': name, 'jour': JOURS[day.weekday()], 'de': f'{hours[best].dt.hour}h',
                    'a': f'{(hours[best].dt.hour + dur_h) % 24}h', 'kwh': round(energy / 1000, 1),
                    'sur_surplus_perdu_kwh': round(best_gain / 1000, 1)}
            placed.append(item)
            return item

        now_h = snap.now.replace(minute=0, second=0, microsecond=0)
        for key in ('washer', 'dryer'):
            d, w = getattr(self.ctrl, key), windows.get(key)
            if not (d.configured and d.enabled):
                continue
            if d.is_running():
                flex.append({'charge': d.name, 'etat': 'en cours'})
                continue
            if not w or w.get('decision') == 'no_program':
                flex.append({'charge': d.name, 'etat': 'aucun programme sélectionné'})
                continue
            e, dur = w.get('energy_wh') or 0, w.get('duration_h') or 2
            flex.append({'charge': f"{d.name} ({w.get('program')})", 'kwh': round(e / 1000, 1),
                         'duree_h': dur, 'pic_w': w.get('peak_w'), 'decision': w.get('decision'),
                         'verdict': w.get('verdict')})
            if w.get('decision') in ('now', 'later_today'):
                st = snap.now if w['decision'] == 'now' else datetime.combine(
                    today, datetime.strptime(w['start_at'], '%H:%M').time())
                place(d.name, e, dur, today, fixed_start=st)
            elif w.get('decision') == 'tomorrow':
                place(d.name, e, dur, tomorrow)
        pump = self.ctrl.pump
        if pump.configured and pump.enabled and pump.must_run():
            e = pump.time_needed_h() * pump.nominal_power()
            flex.append({'charge': 'Pompe piscine', 'kwh': round(e / 1000, 1), 'duree_h': round(pump.time_needed_h(), 1),
                         'regle': 'uniquement sur surplus solaire'})
            place('Pompe piscine', e, pump.time_needed_h(), today, earliest=now_h)
        wh = self.ctrl.water
        if wh.configured and wh.enabled:
            e = wh.f('daily_wh', 3000)
            if wh.is_thermo():
                flex.append({'charge': 'Chauffe-eau thermodynamique', 'kwh_par_jour_estime': round(e / 1000, 1),
                             'etat': f"{'Auto' if wh.thermo_state == 'auto' else 'Arrêt'} — {wh.thermo_reason}",
                             'levier': 'Arrêt la nuit / Auto le jour (heure d\'arrêt réglable)'})
            elif wh.solar_dump_enabled():
                flex.append({'charge': 'Chauffe-eau (délestage)', 'kwh_max': round(e / 1000, 1)})
                place('Chauffe-eau', e, max(1, e / max(1, wh.nominal_power())), today, earliest=now_h)
        vm = self.ctrl.vmi
        if vm.configured and vm.enabled:
            flex.append({'charge': 'VMI (résistance d\'air)', 'etat': vm.reason, 'max_w': vm.heater_max_w(),
                         'regle': 'résistance modulée sur le seul surplus perdu, en dernier (rendement 1, la PAC fait ~3) ; '
                                  'source d\'air automatique (combles / extérieur / recyclage) ; directive vmi heater_max_pct'})
        ev = self.ctrl.ev
        if ev.configured and ev.enabled:
            flex.append({'charge': 'Voiture', 'branchee': ev.plugged(), 'en_charge': ev.is_running(),
                         'etat': ev.reason, 'regle': 'charge seulement sur surplus (batterie servie d\'abord), '
                         'intensité ajustée toutes les 30 s ; hors surplus, décharge de la batterie maison coupée'})
        th, ph = self.ctrl.thermal, self.ctrl.pac_home
        if ph.configured and ph.enabled:
            cf = ph.comfort(snap)
            text = ph.text(snap)
            entry = {'charge': 'PAC maison (stockage de chaleur)', 'modele_thermique': th.text()}
            if th.reliable() and cf['tint'] is not None and text is not None and cf['consigne']:
                h_eve = max(0.0, 19 - (snap.now.hour + snap.now.minute / 60))
                entry['valeur_du_stockage'] = {f'+{x} °C maintenant': th.storage_value(x, cf['consigne'], text, h_eve)
                                               for x in (0.5, 1.0, 1.5)}
            flex.append(entry)
        if offsets:
            up = sum(v for v in offsets.values() if v > 0)
            flex.append({'charge': 'PAC maison (préchauffage)', 'decalages_c': offsets,
                         'levier': 'monter la consigne pendant le surplus, baisser le soir'})
            if up:
                flex[-1]['stockage_thermique'] = 'oui'

        avant = {'aujourdhui': self._day_stats(hours, base, today), 'demain': self._day_stats(hours, base, tomorrow)}
        # pistes : où se trouve le gain possible
        pistes = []
        lt, bt = avant['aujourdhui']['surplus_perdu_kwh'], avant['aujourdhui']['achat_reseau_kwh']
        bd, ld = avant['demain']['achat_reseau_kwh'], avant['demain']['surplus_perdu_kwh']
        if lt >= 1 and bd >= 1:
            pistes.append(f"{lt:.1f} kWh de surplus perdus aujourd'hui alors que {bd:.1f} kWh seront achetés demain : "
                          f"stocker aujourd'hui (préchauffage maison, eau chaude, machines prévues demain à faire aujourd'hui).")
        elif lt >= 1:
            pistes.append(f"{lt:.1f} kWh de surplus perdus aujourd'hui ({', '.join(avant['aujourdhui']['heures_surplus_perdu_kwh'][:4])}) : "
                          f"y placer les consommations déplaçables.")
        if bt >= 0.5:
            pistes.append(f"{bt:.1f} kWh achetés aujourd'hui ({', '.join(avant['aujourdhui']['heures_achat_kwh'][:4])}) : "
                          f"réduire ou décaler les consommations de ces heures.")
        if bd >= 0.5 and lt < 1:
            pistes.append(f"{bd:.1f} kWh achetés demain ({', '.join(avant['demain']['heures_achat_kwh'][:4])}) : "
                          f"limiter la consommation du soir et de la nuit, ne rien ajouter sur la batterie ce soir.")
        if ld >= 1:
            pistes.append(f"{ld:.1f} kWh de surplus perdus demain : y reporter ce qui peut attendre.")
        if not pistes:
            pistes.append("Ni surplus perdu ni achat réseau notable prévu : rien à déplacer.")
        out = {'avant_optimisation': avant, 'pistes': pistes, 'charges_deplacables': flex, 'placement_propose': placed}
        if placed:
            hh = [Hour(dt=h.dt, pv_wh=h.pv_wh, load_wh=h.load_wh, extra_wh=h.extra_wh + x)
                  for h, x in zip(hours, extra)]
            after = self.run_sim(snap, hh)
            apres = {'aujourdhui': self._day_stats(hh, after, today), 'demain': self._day_stats(hh, after, tomorrow)}
            out['apres_placement'] = apres
            out['gain'] = {
                'surplus_valorise_kwh': round(sum(avant[d]['surplus_perdu_kwh'] - apres[d]['surplus_perdu_kwh'] for d in avant), 1),
                'achat_reseau_en_plus_kwh': round(sum(apres[d]['achat_reseau_kwh'] - avant[d]['achat_reseau_kwh'] for d in avant), 1),
            }
        return out

    @staticmethod
    def machine_message(d: Appliance, w: dict) -> str:
        """Phrase pour la commande « Conseil enerSol » de l'équipement : « je la lance… » si enerSol
        a le droit de piloter et que le démarrage à distance est prêt, sinon « conseil : lancez-la… »."""
        dec, verdict = w.get('decision'), w.get('verdict', '')
        if dec == 'no_program':
            return 'Aucun programme sélectionné : choisissez le programme pour avoir un conseil.'
        auto = d.can_pilot() and d.ready()
        if dec in ('now', 'later_today', 'tomorrow'):
            if auto:
                msg = ('Je la lance ' if dec == 'now' else 'Je la lancerai ') + verdict + '.'
            else:
                msg = 'Conseil : lancez-la ' + verdict + '.'
                if d.can_pilot():
                    msg += ' Activez le démarrage à distance pour que je la lance moi-même.'
        else:
            msg = 'Pas de lancement conseillé : ' + verdict + '.'
        if not w.get('appris'):
            msg += ' (Estimation : ce programme n\'est pas encore appris.)'
        return msg

    def min_solar_pct(self) -> float:
        return max(30.0, min(100.0, float(self.ctrl.g.get('appliance_min_solar_pct', 80) or 80)))

    STEP_MIN = 5

    def _fine_series(self, snap: Snapshot, hours: list) -> tuple:
        """Découpe l'horizon horaire en pas de 5 min (PV interpolé linéairement, conso répartie)."""
        n_per_h = 60 // self.STEP_MIN
        pv, load, times = [], [], []
        for i, h in enumerate(hours):
            nxt = hours[i + 1].pv_wh if i + 1 < len(hours) else h.pv_wh
            prv = hours[i - 1].pv_wh if i > 0 else h.pv_wh
            for k in range(n_per_h):
                # interpolation centrée : la production varie dans l'heure (montée le matin, descente le soir)
                f = (k + 0.5) / n_per_h
                w = h.pv_wh + (f - 0.5) * ((nxt - prv) / 2)
                pv.append(max(0.0, w) / n_per_h)
                load.append(h.load_wh / n_per_h)
                times.append(h.dt + timedelta(minutes=k * self.STEP_MIN))
        # la première heure n'est que partiellement restante
        skip = int(snap.now.minute // self.STEP_MIN)
        return pv[skip:], load[skip:], times[skip:]

    def _sim_fine(self, snap: Snapshot, pv: list, load: list, extra: list, lost_until: Optional[int] = None) -> tuple:
        L = self.ctrl.limits
        return simulate_steps(snap.battery_soc, pv, load, extra, self.STEP_MIN / 60.0, L.capacity_wh,
                              L.soc_critical, L.charge_max_w, L.discharge_max_w, L.efficiency, lost_until)

    def _solar_feasibility(self, snap: Snapshot, hours: list, base: SimResult, d: Appliance) -> dict:
        """Courbe de puissance apprise du cycle (programme + température) confrontée, tranche
        par tranche de 5 min, à la production solaire prévue, pour chaque heure de départ possible.

        Part solaire d'un départ = énergie du cycle couverte par le soleil LE JOUR MÊME :
          - directement : production − conso maison, tranche par tranche (suit la courbe) ;
          - ou via la batterie, seulement si elle se remplit quand même avant le soir
            (le cycle consomme alors du surplus qui aurait été perdu / exporté).
        Une batterie vidée la nuit puis rechargée le surlendemain ne compte PAS comme solaire.
        Toute énergie achetée au réseau à cause du cycle est retirée.

        PRIORITÉ À LA BATTERIE : si, avec le cycle, la batterie n'atteint plus 100 % avant le soir,
        seul le surplus qui aurait de toute façon été perdu compte comme solaire.
        Sans programme sélectionné, aucun conseil n'est donné (on ne devine pas le programme)."""
        prog = d.program()
        if not prog:
            return {'program': None, 'option': d.option(), 'ready': False, 'decision': 'no_program',
                    'verdict': 'aucun programme sélectionné : pas de conseil de lancement',
                    'directive': {}, 'best': None, 'today': False}
        curve, curve_src = d.expected_curve()
        energy = sum(curve)
        dur = len(curve) * d.SLOT_S / 3600.0
        thr = self.min_solar_pct()
        today = snap.now.date()
        pv, load, times = self._fine_series(snap, hours)
        n = len(pv)
        zero = [0.0] * n
        step_per_slot = max(1, int(d.SLOT_S // 60 // self.STEP_MIN))
        day_end = {}                                   # date → indice du premier pas du lendemain
        for i, t in enumerate(times):
            day_end[t.date()] = i + 1
        base_cache = {}

        def base(lu):
            if lu not in base_cache:
                base_cache[lu] = self._sim_fine(snap, pv, load, zero, lu)
            return base_cache[lu]

        def coverage(start: int) -> tuple:
            extra = [0.0] * n
            direct = 0.0
            for j, wh in enumerate(curve):
                for k in range(step_per_slot):
                    idx = start + j * step_per_slot + k
                    if idx < n:
                        part = wh / step_per_slot
                        extra[idx] += part
                        direct += min(part, max(0.0, pv[idx] - load[idx]))
            lu = day_end.get(times[min(start, n - 1)].date(), n)
            b_imp, b_lost, _ = base(lu)
            imp, lost, full = self._sim_fine(snap, pv, load, extra, lu)
            more = max(0.0, imp - b_imp)                   # achats réseau dus au cycle
            saved = max(0.0, b_lost - lost)                # surplus du jour récupéré (via batterie)
            # batterie pleine malgré le cycle → le soleil consommé en direct compte ;
            # sinon le cycle prendrait l'énergie de la batterie : seul le surplus perdu compte
            solar = min(max(direct, saved) if full else saved, energy - more)
            e = max(1.0, energy)
            return (max(0.0, min(100.0, 100.0 * solar / e)), more,
                    max(0.0, min(100.0, 100.0 * direct / e)), full)

        cov_now, extra_now, dir_now, full_now = coverage(0)

        def score(it):
            # au-dessus du seuil : on préfère le départ où le soleil couvre DIRECTEMENT la courbe
            # (pas celui où la batterie compense) ; sous le seuil : la meilleure part solaire
            cov, _, _, direct, _ = it
            return (1, round(direct), round(cov)) if cov >= thr else (0, round(cov), round(direct))

        best_today = best_tomorrow = None
        per_15 = 15 // self.STEP_MIN
        for i in range(per_15, min(len(pv), 40 * 12), per_15):          # départs possibles tous les 1/4 h
            t = times[i]
            if not (7 <= t.hour <= 17) or pv[i] <= 0:
                continue
            cov, more, direct, full = coverage(i)
            item = (cov, i, more, direct, full)
            if t.date() == today:
                if best_today is None or score(item) > score(best_today):
                    best_today = item
            elif t.date() == today + timedelta(days=1):
                if best_tomorrow is None or score(item) > score(best_tomorrow):
                    best_tomorrow = item

        def hm(i):
            t = times[i]
            return f"{t.hour}h{t.minute:02d}"

        def pct(it):
            cov, _, more, direct, full = it
            if not full and direct >= 20:
                return (f'{cov:.0f} % solaire : la batterie ne serait plus pleine ce soir '
                        f'(priorité à la recharge de la batterie)')
            if more >= 100:
                return (f'{direct:.0f} % couvert en direct par le soleil, mais ~{more / 1000:.1f} kWh '
                        f'achetés au réseau ensuite (batterie moins pleine le soir)')
            return f'{cov:.0f} % solaire dont {direct:.0f} % en direct'

        res = {
            'program': pretty_program(prog),
            'program_raw': prog,
            'appris': curve_src == 'courbe apprise',
            'option': d.option(), 'ready': d.ready(),
            'energy_wh': round(energy), 'duration_h': round(dur, 1),
            'peak_w': round(max(curve) * 3600 / d.SLOT_S) if curve else 0,
            'curve_source': curve_src, 'energy_source': d._estimate('energy_wh', 0)[1],
            'min_solar_pct': thr,
            'now_solar_pct': round(cov_now), 'now_direct_pct': round(dir_now),
            'now_extra_grid_kwh': round(extra_now / 1000, 2),
        }
        if best_today:
            res['best_today'] = {'at': hm(best_today[1]), 'solar_pct': round(best_today[0]), 'direct_pct': round(best_today[3])}
        if best_tomorrow:
            res['best_tomorrow'] = {'at': 'demain ' + hm(best_tomorrow[1]), 'solar_pct': round(best_tomorrow[0]),
                                    'direct_pct': round(best_tomorrow[3])}
        now_item = (cov_now, 0, extra_now, dir_now, full_now)
        if cov_now >= thr and (not best_today or best_today[3] - dir_now < 15):
            res['verdict'] = f'maintenant ({pct(now_item)})'
            res['decision'] = 'now'
            res['directive'] = {'mode': 'battery_ok', 'hours': [snap.now.hour]}
        elif best_today and best_today[0] >= thr:
            t = times[best_today[1]]
            res['verdict'] = f'à {hm(best_today[1])} ({pct(best_today)})'
            res['decision'] = 'later_today'
            res['start_at'] = t.strftime('%H:%M')
            res['directive'] = {'mode': 'battery_ok', 'hours': [t.hour], 'start_at': t.strftime('%H:%M')}
        elif best_tomorrow and best_tomorrow[0] >= thr:
            res['verdict'] = f'demain à {hm(best_tomorrow[1])} ({pct(best_tomorrow)})'
            res['decision'] = 'tomorrow'
            res['directive'] = {'mode': 'block'}
        else:
            cands = [x for x in (best_today, best_tomorrow) if x] + [now_item]
            best = max(cands, key=lambda x: x[0])
            res['verdict'] = (f'pas de créneau ≥ {thr:.0f} % solaire sur 2 jours '
                              f'(meilleur : {"maintenant" if best[1] == 0 else hm(best[1])}, {pct(best)})')
            res['decision'] = 'insufficient'
            res['directive'] = {}
        res['best'] = (res.get('best_today') or res.get('best_tomorrow') or {}).get('at')
        res['today'] = res['decision'] in ('now', 'later_today')
        res['extra_grid_kwh'] = res['now_extra_grid_kwh']
        if not res['ready']:
            res['directive'] = {}
        return res

    # ── consignes PAC maison ──────────────────────────────────────
    def _pac_home_offsets(self, snap: Snapshot, hours: list, base: SimResult, night: dict) -> dict:
        p = self.ctrl.pac_home
        if not (p.configured and p.enabled and p.can_adjust_consigne()):
            return {}
        # le stockage de chaleur sur le soleil est fait en temps réel (surchauffe solaire, sur le surplus
        # réellement perdu) : ici, seulement la légère baisse du soir si la batterie risque de se vider
        mode = p.effective_mode(snap)
        out = {}
        floor_tonight = base.floor_hit_at is not None and base.floor_hit_at < snap.now + timedelta(hours=16)
        if mode == 'heat' and floor_tonight:
            for hh in range(18, 23):
                out.setdefault(str(hh), -0.5)
        return out

    # ── conseils déterministes ────────────────────────────────────
    def _advice(self, days: dict, base: SimResult, night: dict, windows: dict) -> list:
        tips = []
        d1 = days[1]
        # conseils = leviers chiffrés (pas de constat d'heure de batterie pleine ou de SOC mini)
        tips += (self.plan_opt or {}).get('pistes', [])
        for k in (1, 2, 3):
            d = days[k]
            if d['balance_wh'] is None:
                continue
            tm = '' if d.get('t_min') is None else f", {d['t_min']:.0f} à {d['t_max']:.0f} °C"
            ch = f" dont ~{fmt_kwh(d.get('dont_chauffage_wh') or 0)} de chauffage" if d.get('dont_chauffage_wh') else ''
            unsure = '' if d.get('chauffage_appris') else ' (chauffage pas encore appris : besoin sans doute sous-estimé)'
            besoin = f"{fmt_kwh(d['load_wh'])} de besoin{ch}{tm}{unsure}"
            # sans modèle de chauffage fiable, on garde 3 kWh de marge avant de parler d'excédent
            seuil = 3000 if d.get('chauffage_appris') else 6000
            if d['balance_wh'] < -2000:
                tips.append(f"{d['jour'].capitalize()} : déficitaire ({fmt_kwh(d['pv_wh'])} prévus pour "
                            f"{besoin}) → éviter les gros consommateurs ce jour-là.")
            elif d['balance_wh'] > seuil:
                tips.append(f"{d['jour'].capitalize()} : excédentaire ({fmt_kwh(d['pv_wh'])} prévus pour "
                            f"{besoin}) → bon jour pour les gros consommateurs, une fois la batterie pleine.")
            else:
                tips.append(f"{d['jour'].capitalize()} : {fmt_kwh(d['pv_wh'])} prévus pour {besoin} → "
                            f"pas d'excédent sûr, ne rien ajouter.")
        for key, w in windows.items():
            if key in ('washer', 'dryer') and w.get('verdict'):
                name = 'Lave-linge' if key == 'washer' else 'Sèche-linge'
                if w.get('decision') == 'no_program':
                    tips.append(f"{name} : aucun programme sélectionné, pas de conseil de lancement.")
                    continue
                prog = f"{w['program']}{' ' + w['option'] + '°' if (w.get('option') or '').isdigit() else ''}"
                appris = '' if w.get('appris') else ', programme pas encore appris : estimation'
                tip = f"{name} ({prog}, {w['energy_wh'] / 1000:.1f} kWh{appris}) : {w['verdict']}."
                if w.get('decision') not in ('now', 'later_today'):
                    js = w.get('jours_suivants_possibles') or []
                    tip += (f" Jours suivants possibles : {', '.join(js)}." if js
                            else " Aucun des 3 prochains jours n'a assez de surplus.")
                tips.append(tip)
        if (d1['balance_wh'] or 0) < 0 and (days[0]['balance_wh'] or 0) > 0:
            tips.append("Demain plus faible qu'aujourd'hui : profiter du solaire du jour pour chauffer (ECS, maison).")
        return tips
