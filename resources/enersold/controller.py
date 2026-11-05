"""Couche temps réel (toutes les 10 s) : instantané des mesures, arbitrage du surplus, décisions
par appareil, imprévus et pics, journal du jour.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

from devices import (pretty_program, EVCharger, VMI, Appliance, Battery, HomeHeatPump, Limits, PoolHeatPump,
                     PoolPump, Snapshot, WaterHeater)
from jeedom import Bus
from thermal import ThermalModel
from experience import Experience

log = logging.getLogger('enerSol.controller')


class Controller:

    def __init__(self, bus: Bus, datadir: str):
        self.bus = bus
        self.datadir = datadir
        self.thermal = ThermalModel(datadir)
        self.exp = Experience(datadir)
        self.g: dict = {}
        self.limits = Limits()
        self.pump = PoolPump(bus)
        self.pac_pool = PoolHeatPump(bus)
        self.pac_home = HomeHeatPump(bus, datadir)
        self.washer = Appliance(bus, 'washer', 'Lave-linge', 4, datadir)
        self.dryer = Appliance(bus, 'dryer', 'Sèche-linge', 5, datadir)
        self.water = WaterHeater(bus)
        self.battery = Battery(bus)
        self.ev = EVCharger(bus)
        self.vmi = VMI(bus)
        self._pump_deficit_since = 0.0
        self.solar_net_by_hour: dict = {}       # heure → Wh de soleil net prévu (planificateur)
        self.flow: dict = {}                    # état du surplus, calculé une fois par cycle (_flow)
        self.notifications: list = []      # (titre, message) à envoyer par le démon
        self._notified: dict = {}
        self.lost_today_remaining_wh = 0.0
        self.unplanned = False
        self.peak = None
        self.relief = False
        self.pv_tomorrow_wh = None
        self.pv_today_wh = None
        self.devices = {d.key: d for d in (self.battery, self.pump, self.pac_pool, self.pac_home,
                                          self.washer, self.dryer, self.water, self.ev, self.vmi)}
        self.night_reserve_soc: Optional[float] = None
        self.replan_requested = False
        self._ready_state: dict = {}
        self.last_snapshot: Optional[Snapshot] = None
        self.remaining = 0.0
        self._last_day = ''

    # ── configuration ─────────────────────────────────────────────
    def configure(self, cfg: dict):
        g = cfg.get('global', {})
        self.g = g
        self.thermal.set_manual(g)
        self.thermal.set_zone(g)
        L = self.limits
        L.soc_critical = float(g.get('soc_critical', 20))
        L.capacity_wh = float(g.get('battery_capacity_wh', 10000))
        L.discharge_max_w = float(g.get('battery_discharge_max_w', 3500))
        L.efficiency = min(1.0, max(0.5, float(g.get('battery_efficiency', 0.92))))
        L.hysteresis_off = float(g.get('hysteresis_off', -100))
        L.comfort_min_temp = float(g.get('comfort_min_temp', 19))
        L.comfort_max_offset = float(g.get('comfort_max_offset', 1.5))
        devs = cfg.get('devices', {})
        for key, dev in self.devices.items():
            dev.configure(devs.get(key, {}))
        L.charge_max_w = self.battery.f('charge_max_w', 3500)
        log.info('Configuration : %s', ', '.join(f'{d.name}={("oui" if d.enabled else "désactivé (observation)") if d.configured else "non"}'
                                                 for d in self.devices.values()))

    def forecast_hour_cmds(self) -> dict:
        out = {}
        for h in range(6, 22):
            v = str(self.g.get(f'cmd_forecast_h{h:02d}', '') or '')
            if v.isdigit():
                out[h] = v
        return out

    def all_cmd_ids(self) -> list:
        ids = [v for k, v in self.g.items() if k.startswith('cmd_') and k != 'cmd_notify'
               and str(v).strip().isdigit()]
        for d in self.devices.values():
            ids += d.info_cmds()
        return ids

    # ── instantané ────────────────────────────────────────────────
    def build_snapshot(self) -> Optional[Snapshot]:
        b, g = self.bus, self.g
        pv = b.num(g.get('cmd_pv_power'), None)
        soc = b.num(g.get('cmd_battery_soc'), None)
        if pv is None or soc is None:
            log.warning('PV ou SOC illisible — cycle ignoré')
            return None
        now = datetime.now()
        fh = {h: (b.num(c, 0.0) or 0.0) for h, c in self.forecast_hour_cmds().items()}
        thr = float(g.get('solar_threshold_wh', 100))
        solar = [h for h, w in fh.items() if w >= thr]
        s_start, s_end = (min(solar), max(solar)) if solar else (0, 0)
        grid = b.num(g.get('cmd_grid_power'), 0.0) or 0.0
        snap = Snapshot(
            now=now, pv_power=pv, battery_soc=soc,
            grid_power=grid,
            load_power=b.num(g.get('cmd_load_power'), 0.0) or 0.0,
            text=b.num(g.get('cmd_text'), None),
            season=b.text(g.get('cmd_season'), '') or self._season_from_date(now),
            present=b.flag(g.get('cmd_presence')) if g.get('cmd_presence') else True,
            forecast_by_hour=fh,
            forecast_curr_h=fh.get(now.hour, 0.0),
            forecast_next_h=fh.get(now.hour + 1, 0.0),
            forecast_remain=sum(w for h, w in fh.items() if h >= now.hour),
            forecast_tomorrow=b.num(g.get('cmd_forecast_tomorrow'), 0.0) or 0.0,
            solar_start_h=s_start, solar_end_h=s_end,
            in_solar_window=bool(solar) and s_start <= now.hour <= s_end,
        )
        pc = self.pump.cfg
        if pc:
            snap.iopool_mode = b.text(pc.get('cmd_iopool_mode'), 'summer') or 'summer'
            snap.iopool_temps_min = b.num(pc.get('cmd_iopool_temps_min'), 6.0) or 6.0
            snap.iopool_temp_eau = b.num(pc.get('cmd_iopool_temp_eau'), 20.0) or 20.0
        # surplus = injection réseau (mesure du Cerbo, auto-suffisante)
        snap.available = 0.0 if (pv < 50 or soc < self.limits.soc_critical) else max(0.0, -grid)
        return snap

    @staticmethod
    def _season_from_date(d: datetime) -> str:
        m = d.month
        return 'winter' if m in (12, 1, 2) else 'spring' if m in (3, 4, 5) else 'summer' if m in (6, 7, 8) else 'autumn'

    # ── directives ────────────────────────────────────────────────
    def apply_directives(self, directives: dict):
        for key, dev in self.devices.items():
            dev.set_directive(directives.get(key))
        r = (directives.get('battery') or {}).get('night_reserve_soc')
        self.night_reserve_soc = None if r is None else max(self.limits.soc_critical, min(90.0, float(r)))

    def reserve_floor(self, snap: Snapshot) -> float:
        """SOC plancher pour les consommations « sur batterie » à cet instant."""
        if not snap.in_solar_window and self.night_reserve_soc is not None:
            return self.night_reserve_soc
        return self.limits.soc_critical

    # ── cycle ─────────────────────────────────────────────────────
    def run_cycle(self, cycle_s: float) -> Optional[Snapshot]:
        snap = self.build_snapshot()
        if snap is None:
            return None
        day = snap.now.strftime('%Y-%m-%d')
        if self._last_day and day != self._last_day:
            for d in (self.pump, self.water, self.battery):
                d.reset_daily()
            log.info('━━ Nouveau jour : compteurs remis à zéro ━━')
        self._last_day = day

        for d in self.devices.values():
            if d.configured:
                d.sync(snap)
        self._chain_washer_dryer(snap)
        for d in (self.washer, self.dryer):
            if d.configured:
                r = d.ready() and not d.is_running()
                if r and not self._ready_state.get(d.key):
                    log.info('%s → prêt (%s) : évaluation solaire demandée', d.name, d.profile_key())
                    self.replan_requested = True
                self._ready_state[d.key] = r

        rem = snap.available
        # appareil désactivé : aucune commande envoyée, on tient seulement compte de sa conso
        for d in (self.pump, self.pac_pool, self.pac_home):
            if d.configured and not d.enabled and d.is_running():
                rem -= d.power()
        self._flow(snap)
        self._detect_unplanned(snap)
        self._detect_peak(snap)
        self.relief = self.unplanned or bool(self.peak)
        # ARBITRAGE DU SURPLUS — la batterie est toujours servie d'abord, puis la température (surchauffe
        # PAC, résistance VMI), puis machines, voiture, pompe, chauffe-eau (ordre PRIO, corrigé par
        # l'expérience). Un seul nouveau démarrage à la fois (claim) : le suivant attend que les mesures
        # se stabilisent. Les appareils désactivés ne sont pas décidés du tout (seule leur conso compte).
        self._thermal_update(snap)
        if self.pac_home.configured and self.pac_home.enabled:
            d = self.pac_home.directive or {}
            offset = 0.0
            if self.pac_home.ai_control:
                by_hour = d.get('consigne_offset_by_hour') or {}
                offset = float(by_hour.get(str(snap.now.hour), d.get('consigne_offset', 0)) or 0)
            # surchauffe / surrefroidissement solaire en temps réel : le surplus qui serait perdu
            # (batterie pleine) est stocké dans la maison
            boost = self._surplus_boost(snap)
            mode = self.pac_home.effective_mode(snap)
            if boost:
                offset = max(offset, boost) if mode == 'heat' else min(offset, -boost)
            pk = self.peak
            if pk and mode in ('heat', 'cool') and self.pac_home.ai_control:
                # décision prise UNE fois au début du pic, gardée pendant tout le pic (pas de clignotement) ;
                # seule exception : la maison descend sous le confort minimal → la pause s'arrête
                if 'pause' not in pk:
                    rest = max(1.0, (pk['until'] - time.time()) / 60)
                    pk['pause'], pk['drop'] = self._pac_pause_ok(snap, rest)
                tint_now = self.pac_home.comfort(snap).get('tint')
                if pk['pause'] and mode == 'heat' and tint_now is not None and tint_now < self.limits.comfort_min_temp + 0.1:
                    pk['pause'] = False
                    log.info('Pic %s → la maison atteint le confort minimal : fin de la pause du chauffage', pk['who'])
                ok, drop = pk['pause'], pk['drop']
                if ok:
                    offset = -1.0 if mode == 'heat' else 1.0
                    if not pk['logged']:
                        pk['logged'] = True
                        pk['pac'] = f'chauffage en pause ~{pk["mn"]:.0f} min (≈ −{drop:.1f} °C)'.replace('.', ',')
                        log.info('Pic %s → PAC en pause courte (consigne %+.0f °C), la maison perdra ~%.1f °C',
                                 pk['who'], offset, drop)
                        self._jr('pics', f"{pk['started']} {pk['who']} chauffe l'eau (~{pk['w'] / 1000:.1f} kW) → "
                                         f"{pk['pac']}".replace('.', ',', 1))
                elif not pk['logged']:
                    pk['logged'] = True
                    pk['pac'] = 'chauffage maintenu (confort trop juste)'
                    self._jr('pics', f"{pk['started']} {pk['who']} chauffe l'eau → chauffage maintenu (confort)")
            elif pk and not pk['logged']:
                pk['logged'] = True
                self._jr('pics', f"{pk['started']} {pk['who']} chauffe l'eau (~{pk['w'] / 1000:.1f} kW) → appareils souples en pause".replace('.', ',', 1))
            self.pac_home.apply_offset(offset, self.limits, snap)

        if self.vmi.configured and self.vmi.enabled:
            rem = self._decide_vmi(snap, rem)
        if self.water.configured and self.water.c('cmd_power'):
            w = self.water
            self.exp.water_tick(snap.now, w.power(), snap.in_solar_window,
                                (w.thermo_state != 'stop') if w.is_thermo() else None, cycle_s)
        if self.water.configured and self.water.enabled and self.water.is_thermo():
            self._decide_water_thermo(snap)
        rem = self._decide_generic(snap, rem)
        if self.ev.configured:
            rem = self._decide_ev(snap, rem)
        if self.pump.configured and self.pump.enabled:
            rem = self._decide_pump(snap, rem)
        if self.pac_pool.configured and self.pac_pool.enabled:
            rem = self._decide_pac_pool(snap, rem)
        if self.pac_home.configured and self.pac_home.enabled:
            rem = self._decide_pac_home(snap, rem)
        self._journal_tick(snap, cycle_s)
        self._watchdog(snap)
        self.pump.tick_runtime(cycle_s)

        loads = sum((d.power() or (d.nominal_power() if d.is_running() else 0))
                    for d in self.devices.values() if d is not self.battery and d.configured and d.is_running())
        if self.battery.configured and self.battery.enabled:
            self.battery.run(snap, loads, self.limits)

        self.remaining = rem
        self.last_snapshot = snap
        # ligne d'état : en INFO toutes les 5 min ou si ça bouge vraiment, sinon en DEBUG
        now_m = time.monotonic()
        prev = getattr(self, '_last_state_log', None)
        moved = (prev is None or now_m - prev[0] >= 300
                 or abs(snap.pv_power - prev[1]) >= 300 or abs(snap.battery_soc - prev[2]) >= 2
                 or abs(snap.available - prev[3]) >= 300)
        if moved:
            self._last_state_log = (now_m, snap.pv_power, snap.battery_soc, snap.available)
        (log.info if moved else log.debug)(
            '━━ pv=%.0fW soc=%.0f%% réseau=%.0fW surplus=%.0fW reste=%.0fW solaire=%dh→%dh(%s)',
            snap.pv_power, snap.battery_soc, snap.grid_power, snap.available, rem,
            snap.solar_start_h, snap.solar_end_h, 'IN' if snap.in_solar_window else 'OUT')
        return snap

    # ── pompe ─────────────────────────────────────────────────────
    PUMP_DEFICIT_W = 100        # tolérance avant de considérer que la pompe tire sur la batterie
    PUMP_DEFICIT_S = 180        # durée du déficit avant arrêt (évite de couper à chaque nuage)

    def _decide_pump(self, snap: Snapshot, rem: float) -> float:
        p = self.pump
        ov = p.override
        if ov == 'off':
            p.turn_off()
            return rem
        if ov == 'auto' and not p.is_season_active(snap.season):
            p.turn_off()
            return rem
        if p.state == 'starting_v1':
            return rem - p.handle_sequence()
        if ov == 'on':
            p.turn_on()
            return rem - p.nominal_power()
        if ov == 'manual':
            return rem - (p.nominal_power() if p.is_running() else 0)
        if snap.iopool_mode == 'winter':
            p.turn_off()
            return rem
        if not snap.in_solar_window or snap.pv_power < 100:
            if p.is_running():
                p.turn_off()
            return rem
        # Filtration UNIQUEMENT sur la production solaire : pas de marche forcée en fin de journée,
        # si la durée préconisée n'est pas atteinte, ce n'est pas grave.
        # Bilan solaire = production − consommation maison (pompe comprise si elle tourne) :
        #   > 0 → le soleil couvre tout (le reste va dans la batterie ou le réseau)
        #   < 0 → la maison tire sur la batterie ou le réseau
        solar_balance = snap.pv_power - snap.load_power
        if p.is_running():
            if not p.must_run():
                log.info('Pompe → OFF (durée de filtration atteinte : %.1f h)', p.runtime_h)
                p.turn_off()
                return rem
            if self.relief and solar_balance < -self.PUMP_DEFICIT_W:
                p.turn_off()
                self._shed('pompe piscine arrêtée')
                return rem
            if self.spare(p.nominal_power()) < 0.5 * p.nominal_power() or solar_balance < -self.PUMP_DEFICIT_W:
                if not self._pump_deficit_since:
                    self._pump_deficit_since = time.monotonic()
                elif time.monotonic() - self._pump_deficit_since >= self.PUMP_DEFICIT_S:
                    log.info('Pompe → OFF (plus assez de soleil pour elle et la batterie) — %.1f/%.1f h faites',
                             p.runtime_h, p.quota_h)
                    p.turn_off()
                    self._pump_deficit_since = 0.0
                    return rem
            else:
                self._pump_deficit_since = 0.0
            return rem - p.nominal_power()
        self._pump_deficit_since = 0.0
        if not p.must_run() or p.directive_blocks(snap):
            return rem
        need = p.min_surplus()
        # surplus utilisable : l'injection réseau (batterie pleine), ou la charge batterie
        # si la batterie sera pleine aujourd'hui de toute façon
        usable = self.spare(0.0)
        if usable < need and snap.forecast_next_h > snap.forecast_curr_h * 1.2:
            return rem
        if usable >= need:
            self.want('Pompe piscine')
        if usable >= need and self.claim('Pompe piscine'):
            log.info('Pompe → ON sur surplus solaire (%.0f W, %.1f/%.1f h)', usable, p.runtime_h, p.quota_h)
            self._jr('pompe', f"{snap.now:%H:%M} Pompe piscine lancée au soleil")
            p.turn_on()
            rem -= p.nominal_power()
        return rem

    # ── PAC piscine ───────────────────────────────────────────────
    def _decide_pac_pool(self, snap: Snapshot, rem: float) -> float:
        p = self.pac_pool
        ov = p.override
        if (ov == 'off' or (ov == 'auto' and not p.is_season_active(snap.season))
                or not snap.in_solar_window or not p.needs_heat(snap) or not self.pump.is_running()
                or (ov == 'auto' and p.directive_blocks(snap))):
            if p.is_running():
                p.turn_off()
            return rem
        pw = p.nominal_power()
        if p.is_running():
            if ov != 'on' and rem + pw + self.limits.hysteresis_off < 0:
                log.info('PAC piscine → OFF (déficit %.0f W)', rem)
                p.turn_off()
            else:
                rem -= pw
        elif ov == 'on' or rem >= p.min_surplus():
            p.turn_on()
            rem -= pw
        return rem

    # ── PAC maison (corrigée) ─────────────────────────────────────
    def _decide_pac_home(self, snap: Snapshot, rem: float) -> float:
        p = self.pac_home
        L = self.limits
        if p.control() == 'consigne' or p.override == 'manual':
            return rem - p.power()
        ov = p.override
        if ov == 'off':
            p.turn_off()
            return rem
        if ov == 'on':
            if not p.is_running():
                p.turn_on()
            return rem - p.nominal_power()

        cf = p.comfort(snap)
        text = p.text(snap)
        tint = cf['tint']
        # garde-fou confort : en chauffe, sous la température minimale → toujours autorisé (réseau si besoin)
        if cf['mode'] == 'heat' and tint is not None and tint < L.comfort_min_temp - 0.3:
            if not p.is_running() and p.turn_on():
                log.info('PAC maison → ON (confort minimal : %.1f °C < %.1f °C)', tint, L.comfort_min_temp)
            return rem - p.nominal_power()

        if not cf['need']:
            # le thermostat régule lui-même autour de la consigne : on ne coupe pas pour « confort atteint »
            return rem - (p.power() if p.is_running() else 0.0)
        if text is not None and cf['consigne'] is not None:
            if (cf['mode'] == 'cool' and text < cf['consigne']) or (cf['mode'] == 'heat' and text > cf['consigne']):
                if p.is_running():
                    p.turn_off()
                return rem

        ok, why = self._pac_home_energy_ok(snap, rem)
        if p.directive_blocks(snap) and rem < p.min_surplus():
            ok, why = False, 'créneau bloqué par le plan'
        if p.is_running():
            if ok or cf['delta'] > 2.0:
                return rem - p.power()
            log.info('PAC maison → OFF (%s)', why)
            p.turn_off()
            return rem
        if ok:
            if p.turn_on():
                log.info('PAC maison → ON (%s, mode=%s, Δ=%.1f °C)', why, cf['mode'], cf['delta'])
            return rem - p.nominal_power()
        log.debug('PAC maison → attente (%s)', why)
        return rem

    def _pac_home_energy_ok(self, snap: Snapshot, rem: float):
        p, L = self.pac_home, self.limits
        if rem >= p.min_surplus():
            return True, f'surplus {rem:.0f} W'
        # le chauffage fait partie de la conso de base prévue : pas de réserve de nuit pour lui
        floor = L.soc_critical
        if snap.battery_soc <= floor:
            return False, f'SOC {snap.battery_soc:.0f}% ≤ plancher {floor:.0f}%'
        if snap.in_solar_window:
            hours = max(0.0, snap.solar_end_h - snap.now.hour)
            need = p.avg_power() * hours
            avail = snap.forecast_remain + L.usable_wh(snap.battery_soc, floor)
            bilan = avail - need - L.recharge_wh(snap.battery_soc)
            if bilan >= 0 or p.directive_allows_battery():
                return True, f'bilan du jour OK ({bilan:.0f} Wh)'
            return False, f'bilan du jour insuffisant ({bilan:.0f} Wh)'
        return True, f'nuit sur batterie (SOC {snap.battery_soc:.0f}% > {floor:.0f}%)'

    # ── électroménager + chauffe-eau ──────────────────────────────
    def _decide_generic(self, snap: Snapshot, rem: float) -> float:
        L = self.limits
        for d in sorted((self.washer, self.dryer, self.water), key=lambda x: x.priority):
            if not d.configured:
                continue
            if d is self.water and d.is_thermo():      # géré à part (Auto / Arrêt)
                if d.is_running():
                    rem -= d.power()
                continue
            if not d.enabled:                       # désactivé : on ne touche à rien
                if d.is_running():
                    rem -= d.power() or d.nominal_power()
                continue
            ov = d.override
            if ov == 'manual':
                if d.is_running():
                    rem -= d.power() or d.nominal_power()
                continue
            if ov == 'force':
                if not d.is_running():
                    log.info('%s → DÉMARRAGE FORCÉ', d.name)
                    d.turn_on(force=True) if isinstance(d, Appliance) else d.turn_on()
                d.override = 'auto'
                rem -= d.power() or d.nominal_power()
                continue
            if ov == 'off' or (ov == 'auto' and not d.is_season_active(snap.season)):
                if d.is_running() and d.can_be_stopped():
                    if isinstance(d, WaterHeater):
                        d.turn_off(force=(ov == 'off'))
                    else:
                        d.turn_off()
                elif d.is_running():
                    rem -= d.power()
                continue
            if isinstance(d, WaterHeater) and ov == 'auto' and not d.solar_dump_enabled():
                # délestage solaire non coché : enerSol observe seulement le chauffe-eau
                if d.is_running():
                    rem -= d.power()
                continue
            if isinstance(d, WaterHeater) and d.is_running() and not d.started_by_us and ov != 'on':
                rem -= d.power()                    # chauffe lancée par le ballon lui-même : on n'y touche pas
                continue
            if d.is_running():
                pw = d.power() or d.nominal_power()
                if (isinstance(d, WaterHeater) and ov != 'on' and
                        (not snap.in_solar_window or rem + pw + L.hysteresis_off < 0)):
                    d.turn_off()
                else:
                    rem -= pw
                continue
            # ── démarrage ? ──
            if isinstance(d, WaterHeater):
                if ov == 'on' or (d.solar_dump_enabled() and snap.in_solar_window
                                  and not d.directive_blocks(snap) and rem >= d.min_surplus()
                                  and self.claim('Chauffe-eau')):
                    if d.turn_on():
                        rem -= d.nominal_power()
                continue
            # ── opportunité temps réel : du soleil est perdu MAINTENANT ──
            if isinstance(d, Appliance) and d.program() and ov == 'auto' and self._machine_opportunity(snap, d):
                continue
            # sans programme connu, ou sans droit de lancer : rien (le conseil reste affiché)
            if not d.ready() or not d.program() or not d.can_pilot():
                continue
            if ov == 'on':
                if d.turn_on():
                    rem -= d.nominal_power()
                continue
            if d.directive_blocks(snap):
                continue
            # créneau validé par le planificateur (part solaire suffisante d'après le profil appris) :
            # démarrage même sans surplus instantané, la batterie fait tampon
            start_at = (d.directive or {}).get('start_at')
            if start_at and d.directive_allows_battery():
                hh, mm = (int(x) for x in start_at.split(':'))
                if snap.now.hour * 60 + snap.now.minute < hh * 60 + mm:
                    continue
            if d.directive_allows_battery() and self.relief:
                self._shed(f'{d.name.lower()} repoussé')
                continue
            if d.directive_allows_battery():
                floor = self.reserve_floor(snap)
                self.want('machine')
                if snap.battery_soc > floor + 5 and self.claim('machine'):
                    log.info('%s → ON (créneau solaire validé d\'après la courbe du cycle, %.0f Wh)', d.name, d.cycle_energy_wh())
                    if d.turn_on():
                        self._jr('machines', f"{snap.now:%H:%M} {d.name} lancé au soleil")
                        rem -= d.nominal_power()
                else:
                    log.info('%s → attente : SOC %.0f%% trop proche du plancher %.0f%%', d.name, snap.battery_soc, floor)
        return rem

    # ── état du surplus : calculé UNE fois par cycle, utilisé par tous les appareils ──
    # le soleil restant doit couvrir 1,3 fois ce qu'il manque à la batterie, plus le matin (prévision moins sûre) :
    # 1,3 + 0,08 par heure de soleil restante, au plus 2

    def _flow(self, snap: Snapshot):
        """export, bilan solaire, soleil perdu (et depuis quand), déficit (et depuis quand), et surtout
        battery_ok : la batterie est pleine, OU elle est « en avance » — le soleil net encore prévu
        aujourd'hui (corrigé par le soleil réel) couvre 1,5 fois ce qu'il lui manque. Tant que ce n'est
        pas le cas, AUCUN appareil ne prend la charge de la batterie : il n'a que l'export."""
        now = time.monotonic()
        prev = self.flow or {}
        export = max(0.0, -snap.grid_power)
        balance = snap.pv_power - snap.load_power
        soc_full = float(self.g.get('surplus_boost_soc', 97) or 97)
        lost = export >= 250 or (snap.battery_soc >= soc_full and balance >= 200)
        deficit = balance < -300 or snap.grid_power > 300
        lost_since = (prev.get('lost_since') or now) if lost else 0.0
        def_since = (prev.get('def_since') or now) if deficit else 0.0
        L = self.limits
        need = max(0.0, 100.0 - snap.battery_soc) / 100.0 * L.capacity_wh / max(0.5, L.efficiency) ** 0.5
        net = self.solar_net_by_hour
        # soleil net MESURÉ (production − conso, sans ce que enerSol a lui-même ajouté : résistance VMI,
        # voiture sur surplus), lissé sur ~30 min, comparé au prévu : corrige à la fois une prévision PV trop
        # optimiste et une conso (chauffage…) plus forte que prévu
        ours = (self.vmi.power() if self.vmi.configured and self.vmi.heater_pct else 0.0) + \
               (self.ev.power() if self.ev.configured and self.ev.is_running() and self.ev.started_by_us else 0.0)
        meas = balance + ours
        ema = prev.get('net_ema')
        ema = meas if ema is None else ema + (meas - ema) * min(1.0, 10.0 / 1800)
        ahead, ratio = False, None
        if net and snap.in_solar_window:
            h = snap.now.hour
            fc_now = net.get(h, 0.0)
            ratio = max(0.0, min(1.2, ema / max(fc_now, 300.0)))
            rest = (fc_now * max(0.0, 60 - snap.now.minute) / 60.0 + sum(w for k, w in net.items() if k > h)) * ratio
            # hystérésis : « en avance » à 1,5 × le besoin, ne l'est plus sous 1,15 × (évite le yo-yo)
            margin = max(1.3, min(2.0, 1.3 + 0.08 * max(0.0, snap.solar_end_h + 1 - h - snap.now.minute / 60)))
            ahead = rest >= ((margin - 0.35) * need + 200 if prev.get('ahead') else margin * need + 500)
        self.flow = {'export': export, 'balance': balance, 'lost': lost, 'deficit': deficit,
                     'lost_since': lost_since, 'def_since': def_since,
                     'lost_2min': bool(lost_since) and now - lost_since >= 120,
                     'deficit_s': (now - def_since) if def_since else 0.0,
                     'battery_ok': snap.battery_soc >= soc_full or ahead, 'ahead': ahead,
                     'battery_need_wh': round(need), 'net_ema': ema,
                     # ce que la batterie pourrait encore prendre en plus maintenant (W)
                     'headroom': max(0.0, self.limits.charge_max_w - max(0.0, balance - export)),
                     'net_ratio': None if ratio is None else round(ratio, 2)}

    def spare(self, own_w: float) -> float:
        """Puissance qu'un appareil qui consomme déjà own_w peut utiliser, batterie servie d'abord :
        - batterie pleine ou en avance : tout le bilan solaire (production − conso, own_w compris) ;
        - sinon : seulement ce que la batterie ne peut pas absorber (export au-delà de sa charge max).
        Négatif = l'appareil prend trop et doit baisser."""
        fl = self.flow
        if fl['battery_ok'] or fl['balance'] < 0:
            return own_w + fl['balance']
        return own_w + fl['export'] - fl['headroom']

    # ── surchauffe solaire (temps réel, toutes les 10 s) ──────────
    BOOST_STEP_C = 0.5
    BOOST_UP_S = 300            # +0,5 °C toutes les 5 min tant que le surplus est perdu
    BOOST_DOWN_S = 300          # -0,5 °C après 5 min à tirer sur la batterie / le réseau

    def _surplus_boost(self, snap: Snapshot) -> float:
        """Décalage de consigne (°C, ≥ 0) qui stocke le surplus perdu dans la maison.

        Surplus perdu = batterie (quasi) pleine et production supérieure à la consommation :
        exporté ou bridé par l'onduleur. La batterie est donc toujours servie d'abord.
        Monte par pas de 0,5 °C toutes les 5 min (jusqu'au maximum réglé),
        redescend si la maison tire sur la batterie ou le réseau, retombe à 0 hors solaire.
        Jamais au-delà de la température intérieure maximale (minimale en climatisation)."""
        p, L, g = self.pac_home, self.limits, self.g
        st = self.__dict__.setdefault('_boost', {'c': 0.0, 'ts': 0.0, 'why': ''})
        d = p.directive or {}
        bmax = float(d.get('surplus_boost_max', g.get('surplus_boost_max', L.comfort_max_offset)) or 0)
        bmax = max(0.0, min(L.comfort_max_offset, bmax))
        mode = p.effective_mode(snap)
        th = self.thermal
        if th.reliable() and mode == 'heat' and bmax > 0.5:
            # chaleur stockée trop tôt = perdue avant le soir : si moins de 40 % resterait à 19h,
            # on se limite à +0,5 °C (le surplus de l'après-midi fera le reste)
            text = p.text(snap)
            h_eve = max(0.0, 19 - (snap.now.hour + snap.now.minute / 60))
            if text is not None and th.keep_factor(text, h_eve) < 0.4:
                bmax = 0.5
        if not p.ai_control or bmax <= 0 or mode not in ('heat', 'cool') or not snap.in_solar_window \
                or not p.can_adjust_consigne() or p.override not in ('auto', 'manual'):
            if st['c']:
                log.info('PAC maison → fin de la surchauffe solaire (%s)',
                         'hors solaire' if not snap.in_solar_window else 'désactivée')
            st.update(c=0.0, why='')
            return 0.0
        now = time.monotonic()
        if self.relief and st['c']:
            st.update(c=0.0, ts=now, why='imprévu')
            self._shed('surchauffe PAC arrêtée')
            return 0.0
        fl = self.flow
        export, balance, lost = fl['export'], fl['balance'], fl['lost']
        cf = p.comfort(snap)
        tmax = float(g.get('comfort_max_temp', 23) or 23) if mode == 'heat' else float(g.get('comfort_min_cool', 23) or 23)
        too_far = cf['tint'] is not None and (cf['tint'] >= tmax if mode == 'heat' else cf['tint'] <= tmax)
        if lost and not too_far and st['c'] < bmax:
            self.want('Surchauffe PAC')
        if fl['lost_2min'] and not too_far and st['c'] < bmax and now - st['ts'] >= self.BOOST_UP_S \
                and self.claim('Surchauffe PAC'):
            st['c'] = min(bmax, st['c'] + self.BOOST_STEP_C)
            st['ts'] = now
            st['why'] = f"surplus perdu ~{max(export, balance):.0f} W, batterie {snap.battery_soc:.0f} %"
            log.info('PAC maison → %s solaire %+.1f °C (%s)', 'surchauffe' if mode == 'heat' else 'surrefroidissement',
                     st['c'] if mode == 'heat' else -st['c'], st['why'])
            if st['c'] <= self.BOOST_STEP_C:
                self._jr('surchauffe', f"{snap.now:%H:%M} Maison chauffée avec le soleil en trop (chaleur gardée pour le soir)")
        elif st['c'] and (too_far or fl['deficit_s'] >= self.BOOST_DOWN_S) \
                and now - st['ts'] >= self.BOOST_DOWN_S:
            st['c'] = max(0.0, st['c'] - self.BOOST_STEP_C)
            st['ts'] = now
            st['why'] = 'température max atteinte' if too_far else 'la maison tire sur la batterie'
            log.info('PAC maison → surchauffe réduite à %+.1f °C (%s)', st['c'], st['why'])
        return st['c']

    # ── machines : lancement sur surplus perdu constaté (temps réel) ──
    def _lost_now(self, snap: Snapshot) -> tuple:
        """(surplus perdu depuis ≥ 2 min ?, puissance perdue estimée en W). Si la surchauffe PAC
        absorbe déjà ce surplus, il compte quand même : les machines passent avant la surchauffe."""
        fl = self.flow
        if fl['lost_2min']:
            return True, max(fl['export'], fl['balance'])
        return False, 0.0

    def _machine_opportunity(self, snap: Snapshot, d: Appliance) -> bool:
        """Le soleil est perdu en ce moment : si le cycle appris (courbe) peut être couvert par ce
        surplus et par celui encore prévu aujourd'hui, on lance tout de suite (même avant l'heure
        prévue) — ou, si enerSol n'a pas le droit de lancer, on prévient l'utilisateur une fois."""
        if not snap.in_solar_window:
            return False
        lost, lost_w = self._lost_now(snap)
        if not lost:
            return False
        energy = d.cycle_energy_wh()
        hours_left = max(0.0, snap.solar_end_h + 1 - (snap.now.hour + snap.now.minute / 60))
        dur = min(d.cycle_duration_h(), hours_left)
        covered = max(lost_w * dur, float(getattr(self, 'lost_today_remaining_wh', 0) or 0))
        if covered < 0.7 * energy:
            return False
        txt = (f"surplus perdu ~{lost_w:.0f} W (batterie {snap.battery_soc:.0f} %), "
               f"cycle {energy / 1000:.1f} kWh{'' if d.profiles.get(d.profile_key()) else ' estimé'}")
        if d.can_pilot() and d.ready():
            self.want('machine')
            if not self.claim('machine'):
                return True
            log.info('%s → LANCEMENT sur surplus perdu : %s', d.name, txt)
            if not d.turn_on():
                return True
            self._jr('machines', f"{snap.now:%H:%M} {d.name} lancé avec le soleil en trop ({pretty_program(d.program())})")
            return True
        key = (d.key, d.profile_key(), snap.now.strftime('%Y-%m-%d'))
        last = self._notified.get(key, 0.0)
        if time.monotonic() - last >= 3 * 3600:
            self._notified[key] = time.monotonic()
            why = ('mode « ne rien faire »' if d.observe_only else
                   'démarrage à distance non activé' if not d.ready() else 'pilotage non autorisé')
            self.notifications.append((f'enerSol — {d.name}',
                                       f"☀️ Soleil perdu en ce moment : lancez le {d.name.lower()} "
                                       f"({pretty_program(d.program())}) — {txt}. ({why})"))
            log.info('%s → notification : lancer maintenant (%s)', d.name, txt)
        return False

    def _chain_washer_dryer(self, snap: Snapshot):
        """Fin du lavage : on prépare le séchage. Le linge doit être mis dans le sèche-linge à la main,
        donc on prévient avec la bonne décision (maintenant / plus tard aujourd'hui / attendre)."""
        w, dr = self.washer, self.dryer
        ev = getattr(w, 'finished_event', None)
        if not ev:
            return
        w.finished_event = None
        if not (dr.configured and dr.enabled) or (ev.get('duration_mn') or 0) < 20:
            return
        hab = dr.habitual_profile()
        if hab:
            key, prof = hab
            e = prof['energy_wh']
            prog = pretty_program(key.split('|')[0]) + (f" {key.split('|')[1]}" if '|' in key else '')
            dur = (prof.get('duration_mn') or 90) / 60
        else:
            e, prog, dur = dr.f('cycle_energy_wh', 1800), 'programme habituel', 1.5
        lost, lost_w = self._lost_now(snap)
        remaining = float(self.lost_today_remaining_wh or 0)
        hours_left = max(0.0, snap.solar_end_h + 1 - (snap.now.hour + snap.now.minute / 60))
        if snap.in_solar_window and lost and (lost_w * min(dur, hours_left) >= 0.7 * e or remaining >= 0.7 * e):
            advice = f"le soleil est perdu en ce moment : lancez le séchage maintenant ({prog}, ~{e / 1000:.1f} kWh)."
        elif snap.in_solar_window and remaining >= 0.7 * e:
            advice = (f"encore ~{remaining / 1000:.1f} kWh de surplus prévus aujourd'hui : mettez le linge et "
                      f"sélectionnez « {prog} », enerSol lancera le séchage au bon moment.")
        else:
            advice = (f"pas assez de soleil aujourd'hui pour le séchage ({prog}, ~{e / 1000:.1f} kWh) : "
                      f"étendez le linge, ou sélectionnez le programme et enerSol attendra un créneau solaire.")
        self.notifications.append(('enerSol — lavage terminé', f"👕 Lavage terminé ({pretty_program(ev.get('program'))}). "
                                   f"Sèche-linge : {advice}"))
        self._jr('machines', f"{snap.now:%H:%M} Lavage terminé : conseil séchage envoyé")
        log.info('Lave-linge terminé → conseil séchage : %s', advice)

    def boost_state(self) -> dict:
        st = getattr(self, '_boost', None) or {}
        return {'decalage_c': st.get('c', 0.0), 'raison': st.get('why', '')}

    # ── journal quotidien (ce qui s'est réellement passé) ─────────
    def _jr(self, kind: str, text: str):
        """Note une décision prise aujourd'hui (pour le journal et la tuile)."""
        j = self._journal_today()
        j['decisions'].setdefault(kind, []).append(text)
        j['decisions'][kind] = j['decisions'][kind][-12:]

    def _journal_today(self) -> dict:
        today = datetime.now().strftime('%Y-%m-%d')
        j = self.__dict__.get('_jday')
        if not j or j['date'] != today:
            if j:
                self._journal_close(j)
            saved = self._journal_load_partial()
            if saved and saved.get('date') == today:
                self._jday = saved
                return saved
            j = {'date': today, 'pv_wh': 0.0, 'conso_wh': 0.0, 'achat_wh': 0.0, 'export_wh': 0.0,
                 'achat_soleil_wh': 0.0, 'perdu_estime_wh': 0.0, 'min_batterie_pleine': 0.0,
                 'soc_min': 100.0, 'soc_max': 0.0, 'voiture_surplus_wh': 0.0, 'voiture_hors_surplus_wh': 0.0,
                 'surchauffe_max_c': 0.0, 'surchauffe_min': 0.0, 'decisions': {}}
            self._jday = j
        return j

    def _journal_load_partial(self) -> Optional[dict]:
        try:
            with open(os.path.join(self.datadir, 'journal_today.json')) as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return None

    def _journal_save_partial(self, j: dict):
        try:
            with open(os.path.join(self.datadir, 'journal_today.json'), 'w') as fh:
                json.dump(j, fh, ensure_ascii=False)
        except OSError:
            pass

    def _journal_close(self, j: dict):
        path = os.path.join(self.datadir, 'journal.json')
        try:
            with open(path) as fh:
                days = json.load(fh)
        except Exception:  # noqa: BLE001
            days = []
        days = [d for d in days if d.get('date') != j['date']] + [self.journal_summary(j)]
        try:
            with open(path, 'w') as fh:
                json.dump(days[-30:], fh, ensure_ascii=False, indent=1)
            log.info('Journal du %s : achat %.1f kWh, surplus perdu estimé %.1f kWh, PV %.1f kWh',
                     j['date'], j['achat_wh'] / 1000, j['perdu_estime_wh'] / 1000, j['pv_wh'] / 1000)
        except OSError:
            pass

    @staticmethod
    def journal_summary(j: dict) -> dict:
        k = lambda x: round(x / 1000, 1)
        return {'date': j['date'], 'pv_kwh': k(j['pv_wh']), 'conso_kwh': k(j['conso_wh']),
                'achat_reseau_kwh': k(j['achat_wh']), 'dont_achat_en_journee_kwh': k(j['achat_soleil_wh']),
                'export_kwh': k(j['export_wh']), 'surplus_perdu_estime_kwh': k(j['perdu_estime_wh']),
                'heures_batterie_pleine': round(j['min_batterie_pleine'] / 60, 1),
                'soc_min': round(j['soc_min']), 'soc_max': round(j['soc_max']),
                'voiture_surplus_kwh': k(j['voiture_surplus_wh']), 'voiture_hors_surplus_kwh': k(j['voiture_hors_surplus_wh']),
                'surchauffe_max_c': j['surchauffe_max_c'], 'surchauffe_heures': round(j['surchauffe_min'] / 60, 1),
                'vmi_resistance_surplus_kwh': k(j.get('vmi_resistance_wh', 0.0)),
                'decisions': j['decisions']}


    def _journal_tick(self, snap: Snapshot, cycle_s: float):
        j = self._journal_today()
        h = cycle_s / 3600.0
        j['pv_wh'] += max(0.0, snap.pv_power) * h
        j['conso_wh'] += max(0.0, snap.load_power) * h
        j['achat_wh'] += max(0.0, snap.grid_power) * h
        j['export_wh'] += max(0.0, -snap.grid_power) * h
        if snap.in_solar_window:
            j['achat_soleil_wh'] += max(0.0, snap.grid_power) * h
        soc_full = float(self.g.get('surplus_boost_soc', 97) or 97)
        if snap.battery_soc >= soc_full:
            j['min_batterie_pleine'] += cycle_s / 60.0
            j['perdu_estime_wh'] += max(0.0, max(-snap.grid_power, snap.pv_power - snap.load_power)) * h
        j['soc_min'] = min(j['soc_min'], snap.battery_soc)
        j['soc_max'] = max(j['soc_max'], snap.battery_soc)
        if self.ev.configured and self.ev.is_running():
            key = 'voiture_hors_surplus_wh' if self.ev.batt_blocked or not self.ev.started_by_us else 'voiture_surplus_wh'
            j[key] += self.ev.power() * h
        b = (getattr(self, '_boost', None) or {}).get('c', 0.0)
        if b:
            j['surchauffe_max_c'] = max(j['surchauffe_max_c'], b)
            j['surchauffe_min'] += cycle_s / 60.0
        if time.monotonic() - getattr(self, '_jsaved', 0.0) >= 900:
            self._jsaved = time.monotonic()
            self._journal_save_partial(j)

    # ── imprévus (four, plaques, bouilloire…) ─────────────────────
    UNPLANNED_W = 1500          # appel non piloté au-dessus de la conso habituelle du moment
    UNPLANNED_ON_S = 30
    UNPLANNED_OFF_S = 120

    def _managed_power(self) -> float:
        tot = 0.0
        for d in self.devices.values():
            if d is self.battery or not d.configured:
                continue
            try:
                if d.is_running():
                    tot += d.power() or 0.0
            except Exception:  # noqa: BLE001
                pass
        return tot

    def _detect_unplanned(self, snap: Snapshot):
        """Repère un gros appel que enerSol ne pilote pas (four, plaques…) et lève self.unplanned.
        Les appareils pilotés lâchent alors immédiatement ce qu'ils prenaient, puis reprennent."""
        now = time.monotonic()
        st = self.__dict__.setdefault('_unp', {'base': None, 'on_since': 0.0, 'off_since': 0.0, 'active': False,
                                               'w': 0.0, 'started': '', 'shed': []})
        other = max(0.0, snap.load_power - self._managed_power())
        if st['base'] is None:
            st['base'] = other
        jump = other - st['base']
        if not st['active']:
            st['base'] += (other - st['base']) * 0.01          # conso habituelle, suivie lentement (~15 min)
            if jump >= self.UNPLANNED_W:
                st['on_since'] = st['on_since'] or now
                if now - st['on_since'] >= self.UNPLANNED_ON_S:
                    st.update(active=True, w=jump, started=snap.now.strftime('%H:%M'), shed=[], off_since=0.0)
                    if now - getattr(self, '_vmi_on_ts', -1e9) < 90:
                        st['shed'].append('résistance VMI coupée')
                    log.info('Imprévu : +%.0f W non pilotés (four, plaques…) → je libère mes appareils', jump)
                    self.replan_requested = True
            else:
                st['on_since'] = 0.0
        else:
            st['w'] = max(st['w'], jump)
            if jump < self.UNPLANNED_W * 0.5:
                st['off_since'] = st['off_since'] or now
                if now - st['off_since'] >= self.UNPLANNED_OFF_S:
                    shed = ', '.join(st['shed']) or 'rien à libérer'
                    self._jr('imprevus', f"{st['started']} Imprévu ~{st['w'] / 1000:.1f} kW (four, plaques…) → {shed}".replace('.', ',', 1))
                    log.info('Imprévu terminé (pic ~%.0f W) — reprise normale (%s)', st['w'], shed)
                    st.update(active=False, on_since=0.0, off_since=0.0)
                    self.replan_requested = True
            else:
                st['off_since'] = 0.0
        self.unplanned = st['active']

    # ── pic d'une machine (chauffe de l'eau…) : on fait de la place sans toucher au confort ──
    PEAK_W = 1000.0
    PEAK_MAX_MN = 40

    def _detect_peak(self, snap: Snapshot):
        """Une machine en marche (lancée par enerSol, forcée ou à la main) est dans sa phase
        gourmande ET la maison achète au réseau ou vide la batterie : self.peak est levé.
        Le chauffage fait alors une courte pause et les appareils souples lâchent, le temps du pic."""
        now = time.time()
        cur = getattr(self, 'peak', None)
        deficit = snap.grid_power > 150 or (snap.pv_power - snap.load_power) < -300
        found = None
        for d in (self.washer, self.dryer):
            if not (d.configured and d.is_running()):
                continue
            p = d.power() if d.c('cmd_power') else d.expected_power_now()
            if p >= self.PEAK_W:
                rem = d.peak_remaining_mn(self.PEAK_W) or 10.0
                found = (d, p, min(self.PEAK_MAX_MN, rem))
                break
        if cur:
            cur['seen'] = now if found else cur.get('seen', now)
            if now - cur['seen'] >= 60 or now >= cur['until']:
                log.info('Fin du pic %s : le chauffage et les appareils reprennent normalement', cur['who'])
                self.peak = None
                self.replan_requested = True
            return
        if found and deficit:
            d, p, rem = found
            self.peak = {'who': d.name, 'w': p, 'until': now + rem * 60, 'mn': rem,
                         'started': snap.now.strftime('%H:%M'), 'pac': '', 'logged': False}
            log.info('Pic %s : ~%.0f W pendant ~%.0f min (%s) → je fais de la place', d.name, p, rem,
                     'achat réseau' if snap.grid_power > 150 else 'la batterie se vide')

    def _pac_pause_ok(self, snap: Snapshot, minutes: float) -> tuple:
        """Le chauffage peut-il faire une pause de `minutes` sans sortir du confort ?
        → (oui/non, baisse de température estimée en °C)"""
        p = self.pac_home
        cf = p.comfort(snap)
        tint, text = cf.get('tint'), p.text(snap)
        if tint is None:
            return False, 0.0
        th = self.thermal
        h = minutes / 60.0
        if th.house_reliable() and text is not None:
            drop = (tint - text) * (1 - th.keep_factor(text, h))
        else:
            drop = (tint - (text if text is not None else 5.0)) * h / 20.0     # maison bien isolée : ~20 h
        return tint - drop >= self.limits.comfort_min_temp + 0.2, round(drop, 2)

    def _shed(self, what: str):
        pk = getattr(self, 'peak', None)
        if pk is not None and not self.unplanned:
            pk.setdefault('shed', [])
            if what not in pk['shed']:
                pk['shed'].append(what)
                log.info('Pic %s → %s', pk['who'], what)
            return
        st = self.__dict__.get('_unp')
        if st and st['active'] and what not in st['shed']:
            st['shed'].append(what)
            log.info('Imprévu → %s', what)

    # ── arbitre du surplus ───────────────────────────────────────
    SETTLE_S = 180              # après un démarrage sur surplus, 3 min avant d'en autoriser un autre

    # 1) la batterie (jamais en concurrence : seul le surplus qu'elle ne peut pas prendre est distribué)
    # 2) la TEMPÉRATURE de la maison par la PAC (surchauffe : ~3 kWh de chaleur pour 1 kWh)
    # 3) machines et voiture (1 kWh de soleil évite ~1 kWh acheté plus tard), puis résistance VMI (1 kWh
    #    n'évite que ~0,3 kWh de PAC), pompe, chauffe-eau. L'expérience peut remonter la VMI (prio()).
    PRIO = {'Surchauffe PAC': 1, 'machine': 2, 'Voiture': 3, 'VMI résistance': 4, 'Pompe piscine': 5, 'Chauffe-eau': 6}

    def _thermal_update(self, snap):
        """Alimente le modèle thermique : PAC (T-One), résistance VMI (puissance 0-10 V) et air des combles.
        Sans PAC configurée, on apprend quand même le refroidissement et la VMI (sonde intérieure de la VMI)."""
        v = self.vmi
        tint = text = None
        pac_w, mode = 0.0, None
        if self.pac_home.configured:
            cf = self.pac_home.comfort(snap)
            tint, text, mode = cf['tint'], self.pac_home.text(snap), cf['mode']
            pac_w = self.pac_home.power()
        # mêmes sondes que l'historique appris (salon, puissance du chauffage) si elles sont renseignées
        g = self.g
        if str(g.get('cmd_tint_ref', '')).isdigit():
            tint = self.bus.num(g['cmd_tint_ref'], tint)
        if str(g.get('cmd_heat_power', '')).isdigit():
            pac_w = self.bus.num(g['cmd_heat_power'], pac_w) or 0.0
            if not self.pac_home.configured:
                text = snap.text
                mode = 'heat' if (snap.season == 'winter' or (text is not None and text < 15)) else 'off'
        if v.configured:
            if tint is None:
                tint = v.t('int')
            if text is None:
                text = v.t('ext') if v.t('ext') is not None else snap.text
            if mode is None:
                mode = 'heat' if (snap.season == 'winter' or (text is not None and text < 15)) else 'off'
        if mode is None:
            return
        vmi_w = att = pct = 0.0
        if v.configured:
            if v.heater_pct:
                vmi_w, pct = v.power(), v.heater_pct
            tatt = v.t('attic')
            if v.source == 'attic' and tatt is not None and tint is not None:
                att = max(0.0, tatt - tint) * (v.fan_pct or 0) / 100.0
        self.thermal.update(snap.now, tint, text, pac_w, mode, vmi_w=vmi_w, attic_rate=att, vmi_pct=pct)

    # ── surveillance des allers-retours ──────────────────────────
    WHO_DEVICE = {'Surchauffe PAC': 'pac_home', 'VMI résistance': 'vmi', 'Voiture': 'ev',
                  'Pompe piscine': 'pump', 'Chauffe-eau': 'water_heater'}
    MOD_CMDS = ('cmd_heater', 'cmd_fan', 'cmd_current', 'cmd_charge_slider')   # réglages progressifs
    MAX_MOD_PER_H = 40          # réglages progressifs : au plus 40 par heure et par commande
    MAX_ONOFF_PER_H = 8         # marche / arrêt, consignes, registres : au plus 8 par heure
    CALM_S = 1800               # au-delà : appareil « calmé » 30 min (réglages 3 fois plus lents, pas de démarrage)

    def calm(self, key: str) -> bool:
        return time.monotonic() < (self.__dict__.get('_calm') or {}).get(key, 0.0)

    def _watchdog(self, snap):
        now = time.monotonic()
        if now - getattr(self, '_wd_ts', 0.0) < 60:
            return
        self._wd_ts = now
        recent = {}
        for ts, cid in self.bus.sent:
            if now - ts <= 3600:
                recent[cid] = recent.get(cid, 0) + 1
        calm = self.__dict__.setdefault('_calm', {})
        for key, d in self.devices.items():
            if not d.configured or self.calm(key):
                continue
            worst = None
            for k, v in d.cfg.items():
                if not k.startswith('cmd_') or not str(v).isdigit():
                    continue
                n = recent.get(int(v), 0)
                lim = self.MAX_MOD_PER_H if k in self.MOD_CMDS else self.MAX_ONOFF_PER_H
                if n > lim and (worst is None or n / lim > worst[1] / worst[2]):
                    worst = (k, n, lim)
            if worst:
                calm[key] = now + self.CALM_S
                log.warning('%s : %d ordres « %s » en 1 h (normal ≤ %d) → allers-retours, je le calme 30 min',
                            d.name, worst[1], worst[0], worst[2])
                self._jr('allers_retours', f"{snap.now:%H:%M} {d.name} : {worst[1]} ordres en 1 h → réglages ralentis 30 min")

    def prio(self, who: str) -> int:
        """Ordre de base, corrigé par l'expérience : si les compteurs montrent que 1 kWh de résistance
        VMI chauffe mieux la maison que 1 kWh de PAC (PAC peu efficace par grand froid…), la VMI passe devant."""
        if who == 'VMI résistance':
            r = self.thermal.vmi_ratio()
            if r is not None and r > 1.1:
                return 0                # mesuré : la VMI chauffe mieux que la PAC → elle passe devant
        return self.PRIO.get(who, 1)

    def want(self, who: str):
        """Un appareil signale qu'il voudrait le surplus (même s'il attend encore sa temporisation) :
        les appareils moins prioritaires ne doivent pas le lui prendre."""
        self.__dict__.setdefault('_wants', {})[who] = time.monotonic()

    def claim(self, who: str) -> bool:
        """Autorise UN nouveau démarrage (ou palier de surchauffe) sur le surplus :
        - refusé si un appareil plus prioritaire attend ce surplus (want récent) ;
        - refusé pendant 3 min après un autre démarrage, le temps que les mesures intègrent sa
          consommation (sinon tous partiraient sur le même surplus)."""
        now = time.monotonic()
        if getattr(self, 'relief', False):
            return False                     # imprévu ou pic en cours : aucun nouveau démarrage
        if self.calm(self.WHO_DEVICE.get(who, '')):
            return False                     # cet appareil fait des allers-retours : pas de nouveau démarrage
        prio = self.prio(who)
        for other, ts in (self.__dict__.get('_wants') or {}).items():
            if other != who and now - ts <= 30 and self.prio(other) < prio:
                log.debug('Arbitre : %s attend (%s est prioritaire)', who, other)
                return False
        a = self.__dict__.setdefault('_arb', {'ts': -1e9, 'who': ''})
        if now - a['ts'] < self.SETTLE_S:
            if a['who'] != who:
                log.debug('Arbitre : %s attend (%s vient de démarrer)', who, a['who'])
            return False
        a.update(ts=now, who=who)
        return True

    # ── voiture : charge sur surplus + protection de la batterie maison ──
    EV_START_S = 120            # surplus suffisant depuis 2 min avant de démarrer
    EV_STOP_S = 180             # manque de surplus depuis 3 min avant d'arrêter
    EV_ADJUST_S = 30            # réglage de l'intensité toutes les 30 s
    EV_RESTART_S = 600          # après un arrêt faute de surplus, 10 min avant de relancer

    def _decide_ev(self, snap: Snapshot, rem: float) -> float:
        v = self.ev
        st = self.__dict__.setdefault('_ev', {'ok_since': 0.0, 'low_since': 0.0, 'adj': 0.0})
        now = time.monotonic()
        charging = v.is_running()
        ev_w = v.power() if charging else 0.0
        protect = bool(v.cfg.get('batt_protect', True))
        if not v.enabled:
            return rem - ev_w
        ov = v.override
        mode = str(v.cfg.get('ev_mode', 'surplus'))
        balance = snap.pv_power - snap.load_power          # la conso maison inclut la borne
        # surplus que la voiture peut prendre : ce qu'elle consomme déjà + ce qui serait perdu
        # (batterie servie d'abord : la charge batterie ne compte que si elle sera pleine aujourd'hui)
        # balance < 0 : la maison (voiture comprise) tire déjà sur la batterie → la voiture doit baisser
        usable = self.spare(ev_w)

        if not charging and v.started_by_us and time.time() - v._last_cmd > 300:
            v.started_by_us = False                     # charge terminée ou voiture débranchée
            if not st.get('seen_charging'):
                v._retry_after = time.time() + 1800     # la borne n'a pas démarré : pas de nouvel essai avant 30 min
                log.warning('Voiture → la charge n\'a pas démarré après l\'ordre : nouvel essai dans 30 min')
        if charging:
            st['seen_charging'] = True
        elif not v.started_by_us:
            st['seen_charging'] = False

        if ov == 'off':
            if charging:
                v.turn_off('arrêt demandé')
        elif ov == 'on':
            if not charging and v.plugged():
                v.set_current(int(v.f('max_a', 16)))
                v.turn_on('charge forcée')
                v.forced = True
        elif mode == 'surplus':
            v.forced = False
            if charging and v.started_by_us and self.relief:
                if v.c('cmd_current'):
                    if v.current_a > v.f('min_a', 6):
                        v.set_current(int(v.f('min_a', 6)))
                        self._shed('voiture au minimum')
                elif balance < -300:
                    v.turn_off('imprévu')
                    self._shed('charge voiture arrêtée')
            elif charging and v.started_by_us:
                if not snap.in_solar_window:
                    v.turn_off('fin du solaire')
                elif v.c('cmd_current'):
                    # surplus lissé (~1 min) ; on ne change l'intensité que d'au moins 2 A, au plus toutes les
                    # 30 s — sauf pour BAISSER tout de suite si la maison tire sur la batterie / le réseau
                    st['ema'] = usable if st.get('ema') is None else st['ema'] + (usable - st['ema']) * 0.15
                    target = int(st['ema'] // v.volts())
                    min_a = int(v.f('min_a', 6))
                    if balance < -150 and v.current_a > min_a:
                        v.set_current(max(min_a, int(usable // v.volts())))
                        st['adj'] = now
                    if target < min_a:
                        if v.current_a > min_a:
                            v.set_current(min_a)
                        st['low_since'] = st['low_since'] or now
                        if now - st['low_since'] >= self.EV_STOP_S:
                            v.turn_off(f'surplus insuffisant ({usable:.0f} W)')
                            st['low_since'], st['ema'] = 0.0, None
                            v._retry_after = time.time() + self.EV_RESTART_S      # pas de redémarrage avant 10 min
                    else:
                        st['low_since'] = 0.0
                        if now - st['adj'] >= self.EV_ADJUST_S * (3 if self.calm('ev') else 1) \
                                and abs(target - v.current_a) >= 2:
                            v.set_current(target)
                            st['adj'] = now
                        v.reason = f'surplus solaire {v.current_a} A (~{v.current_a * v.volts() / 1000:.1f} kW)'
                else:
                    if balance < -300:
                        st['low_since'] = st['low_since'] or now
                        if now - st['low_since'] >= self.EV_STOP_S:
                            v.turn_off(f'la maison tire {-balance:.0f} W sur la batterie / le réseau')
                            st['low_since'] = 0.0
                    else:
                        st['low_since'] = 0.0
            elif not charging and not v.started_by_us and v.plugged() and snap.in_solar_window \
                    and not v.directive_blocks(snap):
                if usable >= v.min_surplus() and time.time() >= getattr(v, '_retry_after', 0.0):
                    self.want('Voiture')
                    st['ok_since'] = st['ok_since'] or now
                    if now - st['ok_since'] >= self.EV_START_S and self.claim('Voiture'):
                        v.set_current(int(usable // v.volts()))
                        v.turn_on(f'surplus solaire {usable:.0f} W')
                        self._jr('voiture', f"{snap.now:%H:%M} Voiture en charge avec le soleil")
                        st['ok_since'] = 0.0
                else:
                    st['ok_since'] = 0.0
        # protection : la voiture charge hors surplus (lancée à la main, forcée, la nuit…) → la batterie
        # maison ne se vide pas dans la voiture
        # (pas pendant les 2 min qui suivent un ordre d'enerSol : la borne met du temps à remonter son état)
        off_surplus = charging and time.time() - v._last_cmd >= 120 and (
            not v.started_by_us or getattr(v, 'forced', False) or not snap.in_solar_window)
        if charging and getattr(v, 'forced', False):
            off_surplus = True
        if protect:
            v.block_battery(off_surplus, 'voiture en charge hors surplus' if off_surplus else
                            ('voiture en charge sur surplus' if charging else 'voiture hors charge'))
        if off_surplus:
            v.reason = 'charge hors surplus — décharge de la batterie maison coupée' if protect else 'charge hors surplus'
        elif not charging and not v.started_by_us and not v.reason.startswith('arrêt'):
            v.reason = 'en attente de surplus' if v.plugged() else 'non branchée'
        return rem - (v.power() if v.is_running() else 0.0)

    # ── VMI : source d'air, ventilateur, résistance sur surplus ───
    VMI_ADJUST_S = 60           # réglage de la résistance au plus une fois par minute
    VMI_STEP_PCT = 15           # … et seulement si l'écart vaut la peine
    VMI_STABLE_S = 120          # 2 min de surplus (ou de manque) avant d'allumer (ou d'éteindre)

    def _decide_vmi(self, snap: Snapshot, rem: float) -> float:
        """Hiver : air le plus chaud gratuit (combles / capteur solaire, extérieur si plus chaud que
        dedans, sinon recyclage) et résistance modulée sur le seul surplus perdu (en dernier : la PAC,
        3 fois plus efficace, passe avant). Été : air extérieur quand il est plus frais (freecooling)."""
        v, g = self.vmi, self.g
        d = v.directive or {}
        tint = v.t('int')
        if tint is None and self.pac_home.configured:
            tint = self.pac_home.comfort(snap)['tint']
        text = v.t('ext') if v.t('ext') is not None else snap.text
        tatt = v.t('attic')
        tblow = v.t('blow')
        target = v.f('t_target', 20)
        tcool = v.f('t_cool', 24)
        summer = snap.season == 'summer' or (text is not None and text >= 18)
        heat = tint is not None and tint < target + 1.5 and not summer
        cool = tint is not None and tint > tcool and (text is not None) and text < tint - 2
        # 1) source d'air
        if heat:
            if tatt is not None and tatt >= tint + 2 and v.c('cmd_air_attic'):
                src, why = 'attic', f'combles {tatt:.0f} °C > intérieur {tint:.0f} °C'
            elif text is not None and text >= tint + 2:
                src, why = 'ext', f'extérieur {text:.0f} °C plus chaud que dedans'
            else:
                src, why = 'recycle', 'hiver : on garde la chaleur de la maison'
        elif cool:
            src, why = 'ext', f'freecooling : extérieur {text:.0f} °C < intérieur {tint:.0f} °C'
        else:
            src, why = ('ext', 'été : air extérieur') if summer else ('recycle', 'hiver : recyclage')
        v.set_source(src, why)
        # 2) résistance sur le surplus perdu uniquement
        now = time.monotonic()
        st = self.__dict__.setdefault('_vmi', {'adj': 0.0, 'ema': None, 'ok_since': 0.0, 'low_since': 0.0})
        balance = snap.pv_power - snap.load_power           # la conso maison inclut la résistance
        battery_ok = self.flow['battery_ok']
        hw = v.power()
        usable = self.spare(hw)
        # cascade : la voiture (prioritaire) prend d'abord ; la VMI n'a que ce que la voiture ne peut plus prendre
        ev = self.ev
        if (ev.configured and ev.enabled and ev.is_running() and ev.started_by_us and ev.c('cmd_current')
                and self.prio('Voiture') < self.prio('VMI résistance')):
            usable -= max(0.0, (ev.f('max_a', 16) - ev.current_a) * ev.volts())
        maxpct = max(0.0, min(100.0, float(d.get('heater_max_pct', 100) or 0))) if v.ai_control else 100.0
        tmax = float(g.get('comfort_max_temp', 23) or 23)
        if self.relief and v.heater_pct:
            v.set_heater(0)
            self._shed('résistance VMI coupée')
        allowed = (not self.relief and heat and snap.in_solar_window and v.c('cmd_heater') and d.get('mode') != 'block'
                   and v.override != 'off' and (tint is None or tint < tmax)
                   and (tblow is None or tblow < v.f('t_blow_max', 45)))
        # la VMI pourrait prendre du surplus libéré (utilisé par l'arbitrage chauffe-eau ↔ VMI)
        self._vmi_can_heat = bool(allowed and battery_ok and v.heater_max_w() > 0)
        # surplus lissé sur ~1 min : un nuage de quelques secondes ne fait pas bouger la résistance
        st['ema'] = usable if st['ema'] is None else st['ema'] + (usable - st['ema']) * 0.15
        if not allowed:
            st.update(ema=None, ok_since=0.0, low_since=0.0)
            if v.heater_pct:
                v.set_heater(0)
                v.reason = 'résistance coupée (' + ('hors solaire' if not snap.in_solar_window else
                                                    'température max' if tint is not None and tint >= tmax else 'pas de besoin') + ')'
        else:
            target = min(maxpct, self.exp.vmi_pct_for(st['ema'], v.heater_max_w()))   # courbe 0-10 V apprise
            if v.heater_pct:
                if balance < -150:
                    # la maison tire sur la batterie / le réseau : on baisse tout de suite (jamais on ne monte ici)
                    v.set_heater(min(v.heater_pct, max(0.0, self.exp.vmi_pct_for(usable, v.heater_max_w()))))
                    st['adj'] = now
                elif target < 10:
                    st['low_since'] = st['low_since'] or now
                    if now - st['low_since'] >= self.VMI_STABLE_S:
                        v.set_heater(0)
                elif now - st['adj'] >= self.VMI_ADJUST_S * (3 if self.calm('vmi') else 1) \
                        and abs(target - v.heater_pct) >= self.VMI_STEP_PCT:
                    v.set_heater(target)
                    st['adj'] = now
                if target >= 10:
                    st['low_since'] = 0.0
            elif target >= 15:
                st['ok_since'] = st['ok_since'] or now
                self.want('VMI résistance')
                if now - st['ok_since'] >= self.VMI_STABLE_S and self.claim('VMI résistance'):
                    v.set_heater(target)
                    st['adj'], st['ok_since'] = now, 0.0
                    self._jr('vmi', f"{snap.now:%H:%M} VMI : air chauffé avec le soleil en trop")
            else:
                st['ok_since'] = 0.0
            if v.heater_pct:
                v.reason = f'résistance {v.heater_pct:.0f} % (~{v.power():.0f} W) sur surplus perdu, air : {v.SOURCE_FR[src]}'
            else:
                v.reason = f'air : {v.SOURCE_FR[src]} — {why}'
        # 3) ventilateur
        if v.heater_pct:
            v.set_fan(60 if v.heater_pct <= 50 else 100, 'résistance en marche')     # deux paliers seulement
        elif (heat and src == 'attic') or (heat and src == 'ext') or (cool and src == 'ext'):
            v.set_fan(v.f('fan_max_pct', 100), why)
        else:
            v.set_fan(v.f('fan_min_pct', 30))
        if v.heater_pct:
            self._vmi_on_ts = time.monotonic()
            if v.c('cmd_power') and time.time() - getattr(v, '_heater_ts', 0.0) >= 60:
                self.exp.vmi_curve_tick(v.heater_pct, v.power())
        if v.heater_pct and self.__dict__.get('_jday'):
            self._jday['vmi_resistance_wh'] = self._jday.get('vmi_resistance_wh', 0.0) + v.power() * 10 / 3600
        return rem - (v.power() if v.heater_pct else 0.0)

    # ── chauffe-eau thermodynamique ──────────────────────────────
    def _decide_water_thermo(self, snap: Snapshot):
        """Arrêt la nuit (à partir de l'heure réglée), Auto dès que le soleil revient.
        Garde-fous (pas de mesure du niveau d'eau chaude) :
          - jamais plus de « arrêt maximum » heures d'affilée ;
          - pas d'arrêt pour la nuit si le solaire prévu demain est trop faible ;
          - commandes manuelles ON (= Auto) / OFF (= Arrêt) prioritaires."""
        w = self.water
        ov = w.override
        if ov == 'on':
            w.set_thermo(True, 'forcé par l\'utilisateur')
            return
        if ov == 'off':
            w.set_thermo(False, 'forcé par l\'utilisateur')
            return
        try:
            hh, mm = (int(x) for x in str(w.cfg.get('night_stop', '21:00') or '21:00').split(':'))
        except ValueError:
            hh, mm = 21, 0
        now_min = snap.now.hour * 60 + snap.now.minute
        stop_min = hh * 60 + mm
        # période d'arrêt : de l'heure réglée jusqu'au retour du soleil (début de la fenêtre solaire)
        night = now_min >= stop_min or snap.now.hour < snap.solar_start_h
        if snap.in_solar_window:
            night = False
        if not night:
            pause, why = self._water_vs_vmi(snap)
            if pause:
                w.set_thermo(False, why)
            else:
                w.set_thermo(True, why or 'journée : chauffe sur le soleil')
            return
        if (self.__dict__.get('_wv') or {}).get('paused_at'):
            self._wv['paused_at'] = 0.0               # la nuit arrive : fin de la pause « VMI »
        max_off = w.f('max_off_h', 12) * 3600
        if w.thermo_state == 'stop' and time.time() - w._stop_since >= max_off:
            w.set_thermo(True, f'arrêt maximum atteint ({max_off / 3600:.0f} h)')
            w._forced_auto_ts = time.time()
            return
        if w.thermo_state == 'auto' and time.time() - getattr(w, '_forced_auto_ts', 0.0) < 10 * 3600:
            return                                   # déjà relancé cette nuit après l'arrêt maximum
        pv_tom = self.pv_tomorrow_wh
        # la nuit avant minuit, « demain » = jour suivant ; après minuit, c'est la journée qui commence
        if snap.now.hour < 12:
            pv_tom = self.pv_today_wh
        if pv_tom is not None and pv_tom < w.f('dark_kwh', 5) * 1000:
            w.set_thermo(True, f'solaire prévu faible ({pv_tom / 1000:.1f} kWh) : pas d\'arrêt cette nuit')
            return
        w.set_thermo(False, 'nuit : la chauffe est décalée sur le soleil de demain')

    # ── chauffe-eau ↔ VMI : à qui donner le soleil ? (appris sur les compteurs) ──
    WV_RES_S = 120              # résistance du chauffe-eau en marche depuis 2 min
    WV_PAUSE_MN = 45            # pause maxi d'affilée
    WV_DAY_MN = 120             # pause maxi par jour
    WV_COOLDOWN_S = 3600        # pas de nouvelle pause avant 1 h
    WV_MARGIN = 1.2             # la VMI doit rapporter au moins 20 % de plus

    def stockage_values(self, snap) -> dict:
        """kWh évités plus tard pour 1 kWh de soleil donné maintenant, par appareil."""
        e = self.exp
        wv = e.water_value()
        rest = e.water_rest_kwh()
        w_now = wv['valeur'] if (rest is None or rest > 0.3) else wv['valeur'] * 0.3
        text = None
        if snap is not None:
            text = self.vmi.t('ext') if self.vmi.configured and self.vmi.t('ext') is not None else snap.text
        vv = e.vmi_value(self.thermal, text, 100)
        keep = 1.0
        if snap is not None and self.thermal.house_reliable():
            keep = self.thermal.keep_factor(text, snap.solar_end_h - (snap.now.hour + snap.now.minute / 60))
        return {'chauffe_eau': round(w_now, 2), 'vmi': round(vv['valeur'] * keep, 2),
                'chauffe_eau_reste_kwh': rest, 'chaleur_gardee_le_soir': round(keep, 2),
                'chauffe_eau_appris': wv['appris'], 'vmi_appris': vv['appris']}

    def _water_vs_vmi(self, snap) -> tuple:
        """Le chauffe-eau passe sur sa résistance (~1,8 kW) en journée alors que la VMI pourrait prendre ce
        soleil pour chauffer la maison : si l'expérience dit que la VMI en tire plus ET que le soleil restant
        suffit encore pour l'eau, le chauffe-eau attend (45 min maxi, 2 h par jour). Sinon il chauffe."""
        w = self.water
        st = self.__dict__.setdefault('_wv', {'res_since': 0.0, 'paused_at': 0.0, 'resumed_at': -1e9,
                                              'day': '', 'mn': 0.0, 'why': ''})
        day = snap.now.strftime('%Y-%m-%d')
        if st['day'] != day:
            st.update(day=day, mn=0.0)
        now = time.monotonic()
        vals = self.stockage_values(snap)
        rest = vals['chauffe_eau_reste_kwh']
        left_h = snap.solar_end_h - (snap.now.hour + snap.now.minute / 60)
        pv_left = (snap.forecast_remain or 0) / 1000
        safe = (left_h >= 2.5 and st['mn'] < self.WV_DAY_MN and rest is not None
                and pv_left * 0.6 >= rest + 2 and not self.relief)
        better = vals['vmi'] > vals['chauffe_eau'] * self.WV_MARGIN
        vmi_ok = self.vmi.configured and self.vmi.enabled and getattr(self, '_vmi_can_heat', False)
        if w.ai_control is False:
            safe = False
        if st['paused_at']:
            el = (now - st['paused_at']) / 60
            if el < self.WV_PAUSE_MN and safe and better and vmi_ok:
                return True, st['why']
            st['mn'] += el
            st['paused_at'], st['resumed_at'] = 0.0, now
            log.info('Chauffe-eau → reprise après %.0f min de pause (soleil rendu à l\'eau chaude)', el)
            return False, 'journée : chauffe sur le soleil (après une pause pour la VMI)'
        res = w.thermo_state != 'stop' and max(0.0, w.power()) >= self.exp.RES_W
        st['res_since'] = (st['res_since'] or now) if res else 0.0
        if (res and now - st['res_since'] >= self.WV_RES_S and now - st['resumed_at'] >= self.WV_COOLDOWN_S
                and safe and better and vmi_ok):
            st['paused_at'] = now
            st['why'] = (f"pause : le soleil chauffe mieux la maison par la VMI "
                         f"({vals['vmi']:.2f} contre {vals['chauffe_eau']:.2f} kWh évités) ; "
                         f"l'eau chauffera plus tard au soleil (reste ~{rest:.1f} kWh)").replace('.', ',')
            log.info('Chauffe-eau → pause pour la VMI : 1 kWh de soleil évite %.2f kWh via la VMI contre %.2f via '
                     'le chauffe-eau ; reste %.1f kWh d\'eau à chauffer, %.1f kWh de soleil attendus',
                     vals['vmi'], vals['chauffe_eau'], rest, pv_left)
            self._jr('chauffe_eau', f"{snap.now:%H:%M} Chauffe-eau en pause : le soleil va à la VMI (meilleur rendement mesuré)")
            return True, st['why']
        return False, ''

    def water_paused_for_vmi(self) -> bool:
        return bool((self.__dict__.get('_wv') or {}).get('paused_at'))

    # ── état pour Jeedom ──────────────────────────────────────────
    def events(self) -> dict:
        snap = self.last_snapshot
        out = {}
        if snap:
            out.update({
                'pv_power': round(snap.pv_power, 0),
                'battery_soc': round(snap.battery_soc, 1),
                'load_power': round(snap.load_power, 0),
                'grid_power': round(snap.grid_power, 0),
                'available_power': round(snap.available, 0),
                'remaining_power': round(self.remaining, 0),
                'solar_start_h': snap.solar_start_h,
                'solar_end_h': snap.solar_end_h,
                'in_solar_window': snap.in_solar_window,
                'pv_remain_wh': round(snap.forecast_remain, 0),
            })
        for key, d in self.devices.items():
            if not d.configured:
                continue
            out[f'{key}_running'] = d.is_running()
            out[f'{key}_power'] = round(d.power(), 0)
            out[f'{key}_override'] = d.override
            out[f'{key}_decision'] = _describe_directive(d.directive)
            if d is self.pac_home and d.enabled and getattr(self, '_boost', {}).get('c'):
                b = self._boost
                out[f'{key}_decision'] = f"surchauffe solaire {b['c']:+.1f} °C — {b['why']}"
            if d in (self.ev, self.vmi) and d.enabled:
                out[f'{key}_decision'] = d.reason
            if d is self.water and d.is_thermo() and d.enabled:
                out[f'{key}_decision'] = (f"{'Auto' if d.thermo_state == 'auto' else 'Arrêt' if d.thermo_state == 'stop' else '?'}"
                                    f" — {d.thermo_reason}")
            if isinstance(d, Appliance):
                out[f'{key}_observe'] = d.observe_only
                out[f'{key}_program'] = d.selected_program_text()
                out[f'{key}_pilotage'] = d.pilot_text()
                if d.is_running():
                    rest = f', fin dans {d.remaining_mn:.0f} min' if getattr(d, 'remaining_mn', 0) else ''
                    out[f'{key}_conseil'] = f'en cours{rest} — cycle enregistré à la fin'
                if d.observe_only:
                    out[f'{key}_decision'] = 'ne rien faire : observation et enregistrement des cycles'
                out[f'{key}_last_cycle'] = d.last_cycle_text() or 'aucun cycle enregistré'
        try:
            out['experience'] = self.exp.text(self.thermal)[:2000]
        except Exception as e:  # noqa: BLE001
            log.debug('expérience : %s', e)
        return out

    def set_override(self, key: str, state: str):
        d = self.devices.get(key)
        if d:
            d.set_override(state)
        else:
            log.warning('Override : appareil inconnu %s', key)


def _describe_directive(d: dict) -> str:
    if not d:
        return 'auto'
    parts = []
    if d.get('mode') == 'block':
        parts.append('bloqué')
    elif d.get('mode') == 'battery_ok':
        parts.append('autorisé sur batterie')
    if d.get('hours'):
        hs = sorted(d['hours'])
        parts.append(f'créneau {hs[0]}h→{hs[-1] + 1}h')
    if d.get('consigne_offset'):
        parts.append(f'consigne {float(d["consigne_offset"]):+.1f} °C')
    if d.get('consigne_offset_by_hour'):
        hs = sorted(int(h) for h in d['consigne_offset_by_hour'])
        parts.append(f'consignes ajustées ({len(hs)} h)')
    if d.get('night_reserve_soc') is not None:
        parts.append(f'réserve nuit {float(d["night_reserve_soc"]):.0f}%')
    if d.get('priority_charge'):
        parts.append('charge batterie prioritaire')
    return ', '.join(parts) or 'auto'
