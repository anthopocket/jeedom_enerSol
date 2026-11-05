#!/usr/bin/env python3
"""enersold — démon enerSol.

Boucles :
  - temps réel (cycle configurable, 10 s par défaut) : lecture groupée, décisions, actions ;
  - planification (15 min) : prévisions, simulation du SOC, directives ;
  - météo (1 h) : Open-Meteo J à J+3 ;
  - historique (au démarrage puis chaque nuit) : apprentissage de la consommation et du chauffage.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from consumption import ConsumptionModel     # noqa: E402
from controller import Controller            # noqa: E402
from forecast import PVForecaster            # noqa: E402
from jeedom import Bus, JeedomClient         # noqa: E402
from planner import Planner
import summary                  # noqa: E402

log = logging.getLogger('enerSol')


class Daemon:

    PLAN_EVERY_S = 900
    WEATHER_EVERY_S = 3600
    EVENTS_EVERY_S = 30

    def __init__(self, args):
        self.args = args
        self.datadir = args.datadir
        os.makedirs(self.datadir, exist_ok=True)
        self.client = JeedomClient(args.callback, args.apikey)
        self.bus = Bus()
        self.ctrl = Controller(self.bus, self.datadir)
        self.pv = PVForecaster(self.datadir)
        self.load = ConsumptionModel(self.datadir)
        self.planner = Planner(self.ctrl, self.pv, self.load)
        self.cfg: dict = {}
        self.cycle_s = 10.0
        self._stop = asyncio.Event()
        self._plan_now = asyncio.Event()
        self._last_plan = 0.0
        self._last_events = 0.0
        self._last_weather = 0.0
        self._last_history_day = ''

    # ── configuration ─────────────────────────────────────────────
    def _read_config_file(self) -> dict:
        try:
            with open(self.args.config) as fh:
                return json.load(fh)
        except Exception as e:  # noqa: BLE001
            log.error('Lecture configuration %s : %s', self.args.config, e)
            return {}

    def load_config(self):
        raw = self._read_config_file()
        if not raw:
            return
        self.cfg = raw
        g = self.cfg.get('global', {})
        self.cycle_s = max(5.0, float(g.get('cycle', 10) or 10))
        self.ctrl.configure(self.cfg)
        self._plan_now.set()
        log.info('Configuration chargée (cycle %.0f s)', self.cycle_s)

    async def push_events(self, values: dict):
        if values:
            await self.client.event(values)

    # ── boucle temps réel ─────────────────────────────────────────
    async def realtime_loop(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                values = await self.client.read(self.ctrl.all_cmd_ids())
                self.bus.set_values(values)
                snap = self.ctrl.run_cycle(self.cycle_s)
                if snap:
                    curtailed = snap.battery_soc >= 98 and snap.grid_power > -50 and snap.pv_power > 0
                    self.pv.record_pv(snap.now, snap.pv_power, curtailed)
                    self.load.record(snap.now, snap.load_power,
                                     snap.text if snap.text is not None else self.pv.temp_at(snap.now),
                                     snap.present if self.ctrl.g.get('cmd_presence') else None)
                await self.bus.flush(self.client)
                while self.ctrl.notifications:
                    title, msg = self.ctrl.notifications.pop(0)
                    await self.client.notify(title, msg)
                if snap and time.monotonic() - self._last_events >= self.EVENTS_EVERY_S:
                    self._last_events = time.monotonic()
                    ev = self.ctrl.events()
                    try:
                        ev['synthese'] = summary.encoded(self.ctrl, self.planner)
                    except Exception:  # noqa: BLE001
                        log.exception('Synthèse tuile')
                    await self.push_events(ev)
                if self.ctrl.replan_requested:
                    self.ctrl.replan_requested = False
                    self._plan_now.set()
                if snap and (self._plan_now.is_set() or time.monotonic() - self._last_plan >= self.PLAN_EVERY_S):
                    await self.replan(snap)
            except Exception:  # noqa: BLE001
                log.exception('Cycle temps réel')
            elapsed = time.monotonic() - t0
            if elapsed > self.cycle_s:
                log.debug('Cycle lent : %.1f s', elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.5, self.cycle_s - elapsed))
            except asyncio.TimeoutError:
                pass

    async def replan(self, snap):
        self._plan_now.clear()
        self._last_plan = time.monotonic()
        g = self.cfg.get('global', {})
        if time.monotonic() - self._last_weather >= self.WEATHER_EVERY_S or not self.pv.weather:
            if await self.pv.fetch(float(g.get('meteo_latitude', 43.6)), float(g.get('meteo_longitude', 1.44)),
                                   30.0, 0.0):
                self._last_weather = time.monotonic()
        self.pv.seed_from_solcast(snap.forecast_by_hour, snap.now.date())
        plan = self.planner.compute(snap)
        merged = self.planner.plan.get('directives_det', {})
        self.ctrl.apply_directives(merged)
        days = plan['days']
        base = plan['baseline']
        ev = {
            **{k: days[i]['pv_wh'] for i, k in enumerate(('pv_today_wh', 'pv_j1_wh', 'pv_j2_wh', 'pv_j3_wh'))
               if days[i]['pv_wh'] is not None},
            'load_night_wh': plan['night']['deficit_wh'],
            'soc_min_forecast': base['soc_min'],
            'soc_min_at': base['soc_min_at'] or '',
            'soc_full_at': base['soc_full_at'] or 'non prévue',
            'soc_forecast': base['soc_by_hour'],
            'night_reserve_soc': (merged.get('battery') or {}).get('night_reserve_soc', plan['night']['reserve_soc']),
            **{f'{k}_conseil': self.planner.machine_message(getattr(self.ctrl, k), w)
               for k, w in plan['windows'].items() if k in ('washer', 'dryer')},
            'conseils': ' '.join(plan['advice'])[:2000],
        }
        acc = self.pv.accuracy_yesterday()
        if acc is not None:
            ev['pv_accuracy'] = acc
        await self.push_events(ev)
        kwh = lambda v: f'{v / 1000:.1f} kWh' if isinstance(v, (int, float)) else '?'
        line = ('Plan : solaire aujourd\'hui %s, demain %s | batterie au plus bas %s %% (%s) | pleine %s | réserve nuit %s %%'
                % (kwh(days[0]['pv_wh']), kwh(days[1]['pv_wh']), round(base['soc_min']), base['soc_min_at'],
                   base['soc_full_at'] or 'non prévue', round(ev['night_reserve_soc'])))
        if self.planner.nowcast and abs(self.planner.nowcast[0] - 1) >= 0.15:
            line += f' | soleil réel {self.planner.nowcast[0] * 100:.0f} % du prévu → prochaines heures corrigées'
        # identique au plan précédent → en DEBUG seulement
        (log.info if line != getattr(self, '_last_plan_line', None) else log.debug)(line)
        self._last_plan_line = line

    # ── historique du chauffage (plusieurs années) → modèle thermique ──
    def _heat_history_ids(self):
        g = self.cfg.get('global', {})
        ph = (self.cfg.get('devices', {}) or {}).get('pac_home', {}) or {}
        text = g.get('cmd_text') or ph.get('cmd_text')
        power = g.get('cmd_heat_power') or ph.get('cmd_power')
        tint = g.get('cmd_tint_ref') or ph.get('cmd_tint_salon')
        ids = [str(x or '') for x in (text, power, tint)]
        return ids if all(i.isdigit() for i in ids) else None

    async def import_heat_history(self):
        """Une fois (et à chaque changement des commandes) : lit des années d'historique par tranches de
        90 jours, construit les mesures horaires de chauffe et les donne au modèle thermique."""
        ids = self._heat_history_ids()
        days = int(float(self.cfg.get('global', {}).get('heat_history_days', 1095) or 0))
        if not ids or days <= 0:
            return
        th = self.ctrl.thermal
        th.set_zone(self.cfg.get('global', {}))
        zone = th.zone
        key = f"v4|{','.join(ids)}|{days}|{zone}"   # v4 : chauffage zoné (étage seul / toute la maison)
        if self.ctrl.thermal.hist_info.get('key') == key:
            return
        log.info('Apprentissage du chauffage : lecture de %d jours d\'historique (par tranches de 90 jours)…', days)
        series = {int(i): [] for i in ids}
        split = {str(ids[1]): list(zone)} if zone else None          # la puissance du chauffage, découpée
        for start in range(days, 0, -90):
            if self._stop.is_set():
                return
            part = await self.client.history_weighted(ids, start, max(0, start - 90), split)
            if not part:
                log.warning('Apprentissage du chauffage : historique illisible (%d → %d jours), on réessaiera', start, start - 90)
                return
            for k, v in part.items():
                series.setdefault(k, []).extend(v)
            await asyncio.sleep(2)                     # on laisse respirer Jeedom
        t, p, ti = (series.get(int(i), []) for i in ids)
        house = series.get(f'{ids[1]}:maison') if zone else None
        t = await self._complete_text_history(t, ti, days)
        samples = th.samples_from_history(t, p, ti, house)
        info = {'key': key, 'date': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'heures_lues': {'exterieur': len(t), 'chauffage': len(p), 'interieur': len(ti)}}
        self.ctrl.thermal.seed_history(samples, info)
        log.info('Apprentissage du chauffage : %d heures de chauffe utilisables (sur %d h d\'historique), précision r²=%.2f '
                 '(%s) → %s', len(samples), len(ti), th.r2,
                 'utilisé' if th.reliable() else f'trop imprécis, NON utilisé sous {th.MIN_R2}', ' ; '.join(th.text()))
        self._plan_now.set()

    async def _complete_text_history(self, text: list, tint: list, days: int) -> list:
        """La sonde extérieure est souvent plus récente que le reste : on complète les heures manquantes
        avec l'archive météo Open-Meteo (températures mesurées), recalée sur la sonde (écart moyen sur les
        heures communes : ombre, exposition…)."""
        g = self.cfg.get('global', {})
        have = {k: v for k, v in text if v is not None}
        need = [k for k, _ in tint if k not in have]
        if len(need) < 0.2 * max(1, len(tint)) or not (g.get('meteo_latitude') and g.get('meteo_longitude')):
            return text
        start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        end = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        arch = await self.pv.fetch_temp_archive(float(g['meteo_latitude']), float(g['meteo_longitude']), start, end)
        if not arch:
            return text
        common = [(have[k], v) for k, v in arch if k in have]
        offset = sum(a - b for a, b in common) / len(common) if len(common) >= 100 else 0.0
        added = [[k, round(v + offset, 2)] for k, v in arch if k not in have]
        log.info('Températures extérieures : %d h de la sonde, %d h complétées par l\'archive météo '
                 '(recalée de %+.1f °C sur la sonde)', len(have), len(added), offset)
        return sorted(text + added)

    # ── historique ────────────────────────────────────────────────
    async def history_loop(self):
        await asyncio.sleep(20)
        try:
            await self.import_heat_history()
        except Exception:  # noqa: BLE001
            log.exception('Apprentissage du chauffage (historique)')
        while not self._stop.is_set():
            today = datetime.now().strftime('%Y-%m-%d')
            if today != self._last_history_day and (not self._last_history_day or datetime.now().hour == 3):
                g = self.cfg.get('global', {})
                ids = [i for i in (g.get('cmd_load_power'), g.get('cmd_text')) if str(i or '').isdigit()]
                if ids:
                    hist = await self.client.history(ids, days=45)
                    if hist:
                        temps = hist.get(int(g['cmd_text']), []) if str(g.get('cmd_text', '')).isdigit() else []
                        loads = hist.get(int(g['cmd_load_power']), []) if str(g.get('cmd_load_power', '')).isdigit() else []
                        have = {k for k, v in temps if v is not None}
                        missing = sum(1 for k, _ in loads if k not in have)
                        if missing > 0.2 * max(1, len(loads)) and g.get('meteo_latitude') and g.get('meteo_longitude'):
                            # pas de sonde extérieure, ou sonde récente : on complète avec Open-Meteo
                            om = await self.pv.fetch_temp_history(float(g['meteo_latitude']), float(g['meteo_longitude']), 45)
                            temps = [[k, v] for k, v in om if k not in have] + [[k, v] for k, v in temps if v is not None]
                            log.info('Températures extérieures : %d h de la sonde, %d h complétées par Open-Meteo',
                                     len(have), len(temps) - len(have))
                        self.load.import_history(hist.get(int(g['cmd_load_power']), []) if str(g.get('cmd_load_power', '')).isdigit() else [],
                                                 temps)
                        self._last_history_day = today
                        self._plan_now.set()
                else:
                    self._last_history_day = today
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1800)
            except asyncio.TimeoutError:
                pass

    # ── socket Jeedom → démon ─────────────────────────────────────
    async def handle_client(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            msg = json.loads(line.decode() or '{}')
        except Exception:  # noqa: BLE001
            writer.close()
            return
        writer.close()
        if msg.get('apikey') != self.args.apikey:
            log.warning('Socket : clé API invalide')
            return
        action = msg.get('action', '')
        log.debug('Socket : %s', action)
        if action == 'reload_config':
            old_text = self.cfg.get('global', {}).get('cmd_text')
            self.load_config()
            if self.cfg.get('global', {}).get('cmd_text') != old_text:
                log.info('Sonde extérieure modifiée : nouvel apprentissage de la consommation dans la demi-heure')
                self._last_history_day = ''
            asyncio.create_task(self.import_heat_history())     # rien à faire si les commandes n'ont pas changé
        elif action == 'refresh':
            self._plan_now.set()
        elif action == 'set_override':
            self.ctrl.set_override(msg.get('key', ''), msg.get('state', 'auto'))
        elif action == 'rename_profile':
            dev = self.ctrl.devices.get(msg.get('device', ''))
            if dev is not None and hasattr(dev, 'rename_profile'):
                dev.rename_profile(msg.get('profile', ''), msg.get('program', ''))
                self._plan_now.set()
        elif action == 'forget_profile':
            dev = self.ctrl.devices.get(msg.get('device', ''))
            if dev is not None and hasattr(dev, 'forget_profile'):
                dev.forget_profile(msg.get('profile', ''))
                self._plan_now.set()
        elif action == 'stop':
            self._stop.set()

    # ── démarrage ─────────────────────────────────────────────────
    async def main(self):
        with open(self.args.pid, 'w') as fh:
            fh.write(str(os.getpid()))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)
        self.load_config()
        if not await self.client.ping():
            log.error('Jeedom injoignable via %s — vérifier la configuration réseau', self.args.callback)
        server = await asyncio.start_server(self.handle_client, '127.0.0.1', int(self.args.socketport))
        log.info('━━ enerSol démarré — socket %s, données %s ━━', self.args.socketport, self.datadir)
        tasks = [asyncio.create_task(self.realtime_loop()), asyncio.create_task(self.history_loop())]
        await self._stop.wait()
        log.info('Arrêt du démon')
        # sécurité : ne jamais laisser la décharge de la batterie maison coupée derrière nous
        try:
            if self.ctrl.ev.configured and self.ctrl.ev.batt_blocked:
                self.ctrl.ev.block_battery(False, 'arrêt du démon')
                await self.bus.flush(self.client)
        except Exception:  # noqa: BLE001
            log.exception('Rétablissement de la décharge batterie')
        server.close()
        for t in tasks:
            t.cancel()
        self.pv.save()
        self.load.save()
        await self.client.close()
        try:
            os.remove(self.args.pid)
        except OSError:
            pass


def main():
    p = argparse.ArgumentParser(description='enerSol daemon')
    p.add_argument('--loglevel', default='info')
    p.add_argument('--socketport', default='55790')
    p.add_argument('--callback', required=True)
    p.add_argument('--apikey', required=True)
    p.add_argument('--pid', default='/tmp/jeedom/enerSol/deamon.pid')
    p.add_argument('--config', required=True)
    p.add_argument('--datadir', required=True)
    args = p.parse_args()
    level = {'debug': logging.DEBUG, 'info': logging.INFO, 'notice': logging.INFO,
             'warning': logging.WARNING, 'error': logging.ERROR}.get(str(args.loglevel).lower(), logging.INFO)
    logging.basicConfig(level=level, format='[%(asctime)s][%(levelname)s] %(name)s : %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')
    logging.getLogger('aiohttp').setLevel(logging.WARNING)
    asyncio.run(Daemon(args).main())


if __name__ == '__main__':
    main()
