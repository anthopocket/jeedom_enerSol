"""Appareils pilotés : lecture via le Bus (requêtes groupées), commandes envoyées seulement au changement.

Chaque appareil accepte des « directives » du planificateur (bloquer, autoriser sur
batterie, créneaux horaires, décalage de consigne). Un appareil désactivé n'est jamais commandé.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from jeedom import Bus

log = logging.getLogger('enerSol.devices')



# ═══════════════════════════════════════════════════════════════════
# INSTANTANÉ ÉNERGÉTIQUE
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Snapshot:
    now:               datetime = field(default_factory=datetime.now)
    pv_power:          float = 0.0
    battery_soc:       float = 50.0
    grid_power:        float = 0.0
    load_power:        float = 0.0
    available:         float = 0.0
    text:              Optional[float] = None
    season:            str = 'summer'
    present:           bool = True
    forecast_by_hour:  dict = field(default_factory=dict)   # {h: Wh} aujourd'hui (Solcast)
    forecast_curr_h:   float = 0.0
    forecast_next_h:   float = 0.0
    forecast_remain:   float = 0.0
    forecast_tomorrow: float = 0.0
    solar_start_h:     int = 0
    solar_end_h:       int = 0
    in_solar_window:   bool = False
    iopool_mode:       str = 'summer'
    iopool_temps_min:  float = 6.0
    iopool_temp_eau:   float = 20.0


@dataclass
class Limits:
    """Bornes de sécurité — jamais franchies, quoi que décide le planificateur."""
    soc_critical:        float = 20.0
    capacity_wh:         float = 10000.0
    charge_max_w:        float = 3500.0
    discharge_max_w:     float = 3500.0
    efficiency:          float = 0.92
    hysteresis_off:      float = -100.0
    comfort_min_temp:    float = 19.0
    comfort_max_offset:  float = 1.5

    def usable_wh(self, soc: float, floor_soc: Optional[float] = None) -> float:
        floor_soc = self.soc_critical if floor_soc is None else max(floor_soc, self.soc_critical)
        return max(0.0, soc - floor_soc) / 100.0 * self.capacity_wh

    def recharge_wh(self, soc: float) -> float:
        return max(0.0, 100.0 - soc) / 100.0 * self.capacity_wh


# ═══════════════════════════════════════════════════════════════════
# BASE
# ═══════════════════════════════════════════════════════════════════

class BaseDevice:

    MAX_TURN_ON_FAILURES = 3
    key = 'base'
    name = 'Appareil'

    def __init__(self, bus: Bus):
        self.bus = bus
        self.cfg: dict = {}
        self.configured = False
        self.enabled = True
        self.priority = 9               # ordre interne fixe (plus petit = servi d'abord)
        self.seasons: list = []
        self.ai_control = True
        self.override = 'auto'          # auto | on | off | force | manual
        self.directive: dict = {}       # posée par le planificateur
        self._fails = 0
        self.circuit_open = False

    # ── configuration ─────────────────────────────────────────────
    def configure(self, cfg: dict):
        self.cfg = cfg or {}
        self.configured = bool(cfg)
        self.enabled = bool(self.cfg.get('enabled', True)) and self.configured
        s = str(self.cfg.get('seasons', '') or '')
        self.seasons = [x.strip() for x in s.split(',') if x.strip()]
        self.ai_control = bool(self.cfg.get('ai_control', True))
        self.reset_circuit()
        # désactivé = enerSol ne touche plus à l'appareil (il observe seulement)
        if not self.enabled and self.override == 'off':
            self.override = 'auto'

    def c(self, k: str) -> str:
        return str(self.cfg.get(k, '') or '')

    def f(self, k: str, default: float) -> float:
        try:
            return float(self.cfg.get(k, default))
        except (TypeError, ValueError):
            return default

    # commandes ACTION : ne doivent jamais être lues (dans Jeedom, lire une action l'exécute)
    ACTION_KEYS = ('cmd_start', 'cmd_pause', 'cmd_resume', 'cmd_on', 'cmd_off', 'cmd_charge_slider', 'cmd_current', 'cmd_batt_',
                   'cmd_fan', 'cmd_heater', 'cmd_air_',
                   'cmd_force_charge', 'cmd_set_', 'cmd_notify')

    def info_cmds(self) -> list:
        """Commandes INFO à lire à chaque cycle. La liste vient du schéma PHP (_info_keys) ;
        en secours, on exclut tout ce qui ressemble à une action."""
        keys = self.cfg.get('_info_keys')
        if isinstance(keys, list):
            cand = [(k, self.cfg.get(k)) for k in keys]
        else:
            cand = [(k, v) for k, v in self.cfg.items() if k.startswith('cmd_')]
        return [str(v) for k, v in cand
                if str(v or '').strip().isdigit() and not k.startswith(self.ACTION_KEYS)]

    def is_season_active(self, season: str) -> bool:
        return not self.seasons or season in self.seasons

    # ── circuit breaker ───────────────────────────────────────────
    def _on_start_result(self, ok: bool):
        if ok:
            self._fails = 0
            return
        self._fails += 1
        log.warning('%s → échec démarrage (%d/%d)', self.name, self._fails, self.MAX_TURN_ON_FAILURES)
        if self._fails >= self.MAX_TURN_ON_FAILURES:
            self.circuit_open = True
            self.override = 'off'
            log.error('%s → CIRCUIT OUVERT : vérifier les commandes Jeedom', self.name)

    def reset_circuit(self):
        self.circuit_open = False
        self._fails = 0
        if self.override == 'off' and self.enabled:
            self.override = 'auto'

    def set_override(self, state: str):
        if state != 'off' and self.circuit_open:
            self.reset_circuit()
        self.override = state
        log.info('%s → mode %s', self.name, state)

    # ── directives ────────────────────────────────────────────────
    def set_directive(self, d: Optional[dict]):
        self.directive = dict(d or {})

    def directive_blocks(self, snap: Snapshot) -> bool:
        if not self.ai_control:
            return False
        if self.directive.get('mode') == 'block':
            return True
        hours = self.directive.get('hours')
        if hours and snap.now.hour not in hours:
            return True
        return False

    def directive_allows_battery(self) -> bool:
        return self.ai_control and self.directive.get('mode') == 'battery_ok'

    # ── à implémenter ─────────────────────────────────────────────
    def sync(self, snap: Snapshot): pass
    def is_running(self) -> bool: return False
    def power(self) -> float: return 0.0
    def nominal_power(self) -> float: return self.f('nominal_w', 1000.0)
    def min_surplus(self) -> float: return self.f('min_surplus', 1000.0)
    def can_be_stopped(self) -> bool: return True
    def turn_on(self) -> bool: return False
    def turn_off(self) -> bool: return False

    def status(self) -> dict:
        return {
            'key': self.key, 'name': self.name, 'configured': self.configured,
            'enabled': self.enabled, 'running': self.is_running(),
            'power': round(self.power(), 0), 'override': self.override,
            'priority': self.priority, 'circuit_open': self.circuit_open,
            'directive': self.directive,
        }


# ═══════════════════════════════════════════════════════════════════
# POMPE PISCINE
# ═══════════════════════════════════════════════════════════════════

class PoolPump(BaseDevice):

    key = 'pump'
    name = 'Pompe piscine'
    START_DELAY = 20.0
    STATE_GRACE_S = 60.0

    def __init__(self, bus: Bus):
        super().__init__(bus)
        self.priority = 1
        self.state = 'off'          # off | starting_v1 | running_v2
        self._v1_time = 0.0
        self.runtime_h = 0.0
        self._last_cmd_ts = 0.0
        self._quota_date = ''
        self.quota_h = 6.0

    def sync(self, snap: Snapshot):
        mn = self.bus.num(self.c('cmd_runtime_mn'), None)
        if mn is not None and mn >= 0:
            self.runtime_h = mn / 60.0
        today = snap.now.strftime('%Y-%m-%d')
        if self._quota_date != today:
            self.quota_h = snap.iopool_temps_min
            self._quota_date = today
            log.info('Pompe → durée de filtration figée à %.1f h', self.quota_h)
        state_cmd = self.c('cmd_state_v2') or self.c('cmd_state_v1')
        # juste après un ordre d'enerSol, l'état remonté peut avoir quelques secondes de retard
        if state_cmd and time.time() - self._last_cmd_ts >= self.STATE_GRACE_S:
            real_on = self.bus.flag(state_cmd)
            if real_on and self.state == 'off':
                self.state = 'running_v2'
                if self.override == 'auto':
                    self.override = 'manual'
                    log.info('Pompe → en marche hors plugin (manuel)')
            elif not real_on and self.state == 'running_v2':
                self.state = 'off'
                if time.time() - self._v1_time < 600:
                    # démarrée par enerSol mais l'état ne suit pas : pas de nouvel essai avant 30 min
                    self._retry_after = time.time() + 1800
                    log.warning('Pompe → ne tourne pas après l\'ordre de démarrage : nouvel essai dans 30 min')
                if self.override == 'manual':
                    self.override = 'auto'

    def tick_runtime(self, cycle_s: float):
        if not self.c('cmd_runtime_mn') and self.state == 'running_v2':
            self.runtime_h += cycle_s / 3600.0

    def reset_daily(self):
        if not self.c('cmd_runtime_mn'):
            self.runtime_h = 0.0
        if self.override != 'auto':
            self.override = 'auto'

    def must_run(self) -> bool:
        return self.runtime_h < self.quota_h

    def time_needed_h(self) -> float:
        return max(0.0, self.quota_h - self.runtime_h)

    def handle_sequence(self) -> float:
        if self.state == 'starting_v1':
            if time.time() - self._v1_time >= self.START_DELAY:
                log.info('Pompe → V2 ON')
                self.bus.exec(self.c('cmd_on_v2'))
                self.state = 'running_v2'
                self._last_cmd_ts = time.time()
            return self.f('power_v1', 500)
        return 0.0

    def is_running(self): return self.state == 'running_v2'
    def power(self): return self.bus.num(self.c('cmd_power'), 0.0) or 0.0
    def nominal_power(self): return self.f('power_v2', 350)
    def min_surplus(self): return self.f('min_surplus', 400)

    def turn_on(self):
        if self.circuit_open or not self.enabled:
            return False
        if self.state == 'off' and time.time() < getattr(self, '_retry_after', 0.0):
            return False
        if self.state == 'off':
            log.info('Pompe → V1 ON (début de séquence)')
            if self.bus.exec(self.c('cmd_on_v1'), on_result=self._on_start_result):
                self.state = 'starting_v1'
                self._v1_time = time.time()
                self._last_cmd_ts = time.time()
                return True
            return False
        return True

    def turn_off(self):
        if self.state != 'off':
            log.info('Pompe → OFF')
            self.bus.exec(self.c('cmd_off_v1'))
            self.bus.exec(self.c('cmd_off_v2'))
            self.state = 'off'
            self._last_cmd_ts = time.time()
        return True

    def status(self):
        s = super().status()
        s.update({'state': self.state, 'runtime_h': round(self.runtime_h, 2), 'quota_h': self.quota_h})
        return s


# ═══════════════════════════════════════════════════════════════════
# PAC PISCINE
# ═══════════════════════════════════════════════════════════════════

class PoolHeatPump(BaseDevice):

    key = 'pac_pool'
    name = 'PAC piscine'

    def __init__(self, bus: Bus):
        super().__init__(bus)
        self.priority = 2
        self.running = False

    def target_temp(self) -> float:
        return self.bus.num(self.c('cmd_temp_cible'), None) or self.f('temp_cible_local', 28.0)

    def sync(self, snap: Snapshot):
        if self.c('cmd_state'):
            self.running = self.bus.flag(self.c('cmd_state'))
        elif self.c('cmd_power'):
            self.running = self.power() > 100

    def needs_heat(self, snap: Snapshot) -> bool:
        return (bool(self.c('cmd_on')) and snap.iopool_mode != 'winter'
                and snap.iopool_temp_eau < self.target_temp())

    def is_running(self): return self.running
    def power(self): return self.bus.num(self.c('cmd_power'), 0.0) or 0.0
    def nominal_power(self):
        p = self.power()
        return p if p > 100 else self.f('nominal_w', 2000)
    def min_surplus(self): return self.f('min_surplus', 800)

    def turn_on(self):
        if self.circuit_open or not self.enabled or not self.c('cmd_on'):
            return False
        if time.time() - getattr(self, '_on_ts', 0.0) < 600:
            return False                    # ON déjà envoyé il y a moins de 10 min : on ne le renvoie pas
        log.info('PAC piscine → ON')
        self.running = True
        self._on_ts = time.time()
        return self.bus.exec(self.c('cmd_on'), on_result=self._on_start_result)

    def turn_off(self):
        if self.running:
            log.info('PAC piscine → OFF')
            self.running = False
            self.bus.exec(self.c('cmd_off'))
        return True


# ═══════════════════════════════════════════════════════════════════
# PAC MAISON
# ═══════════════════════════════════════════════════════════════════

class HomeHeatPump(BaseDevice):

    key = 'pac_home'
    name = 'PAC maison'
    ROOMS = ('salon', 'ch1', 'ch2', 'ch3')
    RUN_THRESHOLD_W = 40.0

    def __init__(self, bus: Bus, datadir: str):
        super().__init__(bus)
        self.priority = 3
        self.running = False
        self._started_by_plugin_ts = 0.0
        self._order_pending = False       # ON envoyé, la PAC n'a pas encore démarré
        self._retry_after = 0.0
        self._history: list = []                 # [(ts, W)] sur 4 h
        self._avg_file = os.path.join(datadir, 'pac_home_power_avg.json')
        self._saved_avg = self._load_avg()
        self._last_save = 0.0
        self._base_consignes: dict = {}          # consignes avant ajustement
        self._applied_offset = 0.0
        self.mode = 'stop'

    # ── lectures ──────────────────────────────────────────────────
    def control(self) -> str:
        return self.c('control') or 'onoff'

    def temps(self) -> dict:
        out = {}
        for r in self.ROOMS:
            v = self.bus.num(self.c('cmd_tint_' + r), None)
            if v is not None:
                out[r] = v
        return out

    def consignes(self) -> dict:
        out = {}
        for r in self.ROOMS:
            v = self.bus.num(self.c('cmd_consigne_' + r), None)
            if v is not None:
                out[r] = v
        return out

    def text(self, snap: Snapshot) -> Optional[float]:
        v = self.bus.num(self.c('cmd_text'), None)
        return v if v is not None else snap.text

    def _raw_mode(self) -> str:
        if not self.c('cmd_mode'):
            return 'heat'
        v = self.bus.num(self.c('cmd_mode'), 5.0)
        return {4: 'heat', 2: 'cool'}.get(int(v or 5), 'stop')

    def effective_mode(self, snap: Snapshot) -> str:
        """Mode utile : le mode réel si la PAC tourne, sinon déduit (corrige le blocage mode=5)."""
        m = self._raw_mode()
        if m != 'stop':
            return m
        t, c = self.temps(), self.consignes()
        if t and c:
            tm, cm = sum(t.values()) / len(t), sum(c.values()) / len(c)
            if abs(tm - cm) > 0.3:
                return 'cool' if tm > cm else 'heat'
        return 'cool' if snap.season in ('spring', 'summer') else 'heat'

    def comfort(self, snap: Snapshot) -> dict:
        t, c = self.temps(), self.consignes()
        mode = self.effective_mode(snap)
        tm = sum(t.values()) / len(t) if t else None
        cm = sum(c.values()) / len(c) if c else None
        need, delta = False, 0.0
        if tm is not None and cm is not None:
            if mode == 'heat':
                delta = max(0.0, max(c.get(p, cm) - t.get(p, tm) for p in t))
                need = (cm - tm) > 0.5
            elif mode == 'cool':
                delta = max(0.0, max(t.get(p, tm) - c.get(p, cm) for p in t))
                need = (tm - cm) > 0.5
        return {'mode': mode, 'tint': tm, 'consigne': cm, 'delta': delta, 'need': need}

    # ── état ──────────────────────────────────────────────────────
    def sync(self, snap: Snapshot):
        p = self.power()
        real_on = p > self.RUN_THRESHOLD_W
        if real_on:
            self._order_pending = False
        if real_on != self.running:
            self.running = real_on
            if real_on and time.time() - self._started_by_plugin_ts > 600:
                # en « ON/OFF » enerSol démarre la PAC lui-même : si c'est le thermostat qui l'a fait, on le
                # laisse faire (manuel). En « consignes seulement », c'est le fonctionnement normal.
                if self.control() != 'consigne' and self.override == 'auto':
                    log.info('PAC maison → démarrée par le thermostat (%.0f W) : enerSol le laisse faire', p)
                    self.override = 'manual'
            if not real_on and self.override == 'manual':
                self.override = 'auto'
        now = time.time()
        if real_on:
            self._history.append((now, p))
            cutoff = now - 4 * 3600
            self._history = [(t, v) for t, v in self._history if t >= cutoff]
            if now - self._last_save > 1800 and len(self._history) > 10:
                self._save_avg(self.avg_power())
                self._last_save = now
        elif self._history:
            self._save_avg(sum(v for _, v in self._history) / len(self._history))
            self._history = []
        self.mode = self.effective_mode(snap)

    def avg_power(self) -> float:
        if self._history and time.time() - self._history[0][0] >= 1800:
            return sum(v for _, v in self._history) / len(self._history)
        return self._saved_avg or self.f('nominal_w', 1000)

    def _load_avg(self) -> float:
        try:
            with open(self._avg_file) as fh:
                return float(json.load(fh).get('power_avg_w', 0))
        except Exception:  # noqa: BLE001
            return 0.0

    def _save_avg(self, v: float):
        self._saved_avg = v
        try:
            with open(self._avg_file, 'w') as fh:
                json.dump({'power_avg_w': round(v, 1), 'ts': datetime.now().isoformat(timespec='minutes')}, fh)
        except OSError as e:
            log.debug('PAC maison → sauvegarde moyenne : %s', e)

    # ── consignes ─────────────────────────────────────────────────
    def can_adjust_consigne(self) -> bool:
        return self.control() in ('consigne', 'both') and any(
            self.c('cmd_set_consigne_' + r) for r in self.ROOMS)

    def apply_offset(self, offset: float, limits: Limits, snap: Snapshot):
        """Décalage des consignes, borné. 0 = retour aux consignes d'origine."""
        if not self.can_adjust_consigne():
            return
        offset = max(-limits.comfort_max_offset, min(limits.comfort_max_offset, float(offset or 0)))
        if abs(offset - self._applied_offset) < 0.05:
            return
        if not self._base_consignes:
            self._base_consignes = self.consignes()
        mode = self.effective_mode(snap)
        for r, base in self._base_consignes.items():
            cmd = self.c('cmd_set_consigne_' + r)
            if not cmd:
                continue
            target = base + offset
            if mode == 'heat':
                target = max(target, limits.comfort_min_temp)
            target = round(target * 2) / 2      # pas de 0,5 °C
            self.bus.exec(cmd, slider=target)
        log.info('PAC maison → décalage consignes %+.1f °C', offset)
        self._applied_offset = offset
        if offset == 0:
            self._base_consignes = {}

    # ── commandes ─────────────────────────────────────────────────
    def is_running(self): return self.running
    def power(self): return self.bus.num(self.c('cmd_power'), 0.0) or 0.0
    def nominal_power(self): return self.avg_power()
    def min_surplus(self): return self.f('min_surplus', 1000)

    START_WAIT_S = 600          # après un ON : 10 min pour démarrer avant de conclure qu'elle ne démarre pas
    RETRY_S = 1800              # puis 30 min avant un nouvel essai

    def turn_on(self) -> bool:
        """Envoie ON une seule fois, puis attend que la PAC démarre (True = ordre envoyé maintenant)."""
        if self.circuit_open or not self.enabled or not self.c('cmd_on'):
            return False
        now = time.time()
        if self._order_pending:
            if now - self._started_by_plugin_ts < self.START_WAIT_S:
                return False                       # ordre déjà envoyé : on attend, on ne renvoie pas
            self._order_pending = False
            self._retry_after = now + self.RETRY_S
            log.warning('PAC maison → ne démarre pas 10 min après l\'ordre ON (commande, RF ou thermostat ?) : '
                        'nouvel essai dans 30 min')
            return False
        if now < self._retry_after:
            return False
        log.info('PAC maison → ON')
        self._started_by_plugin_ts = now
        self._order_pending = True
        return self.bus.exec(self.c('cmd_on'), on_result=self._on_start_result)

    def turn_off(self):
        if self.running and self.c('cmd_off'):
            log.info('PAC maison → OFF')
            self.running = False
            self.bus.exec(self.c('cmd_off'))
        return True

    def status(self):
        s = super().status()
        s.update({'mode': self.mode, 'avg_power_w': round(self.avg_power(), 0),
                  'consigne_offset': self._applied_offset, 'control': self.control()})
        return s


# ═══════════════════════════════════════════════════════════════════
# ÉLECTROMÉNAGER (hOn)
# ═══════════════════════════════════════════════════════════════════

PROGRAM_FR = {
    'synthetic': 'synthétique', 'cotton': 'coton', 'cottons': 'coton', 'mixed': 'mixte', 'delicate': 'délicat',
    'delicates': 'délicat', 'wool': 'laine', 'quick': 'rapide', 'rapid': 'rapide', 'eco': 'éco', 'eco_40_60': 'éco 40-60',
    'baby': 'bébé', 'sport': 'sport', 'shirts': 'chemises', 'jeans': 'jean', 'duvet': 'couette', 'rinse': 'rinçage',
    'spin': 'essorage', 'refresh': 'rafraîchir', 'allergy': 'anti-allergie', 'hygiene': 'hygiène', 'dark': 'foncé',
    'auto': 'auto', 'timed': 'minuté', 'extra_dry': 'extra sec', 'cupboard_dry': 'prêt à ranger', 'iron_dry': 'prêt à repasser',
}


def pretty_program(name: str) -> str:
    """« iot_wash_synthetic » → « synthétique » (affichage seulement ; la clé apprise reste brute)."""
    n = str(name or '').strip()
    if not n or n == 'defaut':
        return 'programme inconnu'
    k = n.lower()
    for pre in ('iot_', 'wash_', 'dry_', 'tumble_', 'wm_', 'td_'):
        if k.startswith(pre):
            k = k[len(pre):]
    return PROGRAM_FR.get(k, k.replace('_', ' '))


class Appliance(BaseDevice):
    """Lave-linge / sèche-linge hOn.

    Chaque cycle est enregistré (programme, température ou niveau de séchage,
    durée, énergie) et alimente un profil par couple « programme | option ».
    Mesure de l'énergie, par ordre de préférence :
      1. puissance de l'appareil (cmd_power) intégrée ;
      2. compteur d'énergie de l'appareil (cmd_energy, kWh) : différence fin − début ;
      3. à défaut, surconsommation de la maison pendant le cycle par rapport
         à sa consommation des 15 minutes précédant le démarrage (estimation).
    """

    MAX_TURN_ON_FAILURES = 5
    MAX_CYCLES = 60
    SLOT_S = 300                              # courbe de puissance enregistrée par tranches de 5 minutes
    START_GRACE_S = 180                       # après un lancement, hOn met jusqu'à 1-2 min à remonter l'état
    PROGRAM_DELAY_S = 60                      # programme / température lus 1 min après le départ
    MIN_CYCLE_MN = 10                         # en dessous : faux départ, jamais enregistré

    def __init__(self, bus: Bus, key: str, name: str, priority: int, datadir: str):
        super().__init__(bus)
        self.key = key
        self.name = name
        self.priority = priority
        self.running = False
        self.remaining_mn = 0.0
        self._program = ''
        self._saved_program = ''
        self._saved_option = ''
        self._last_start_ts = 0.0
        self._cycle_start = 0.0
        self._energy_acc = 0.0
        self._house_acc = 0.0
        self._energy_counter_start: Optional[float] = None
        self._last_sample = 0.0
        self._curve_cur: list = []
        self._seen_program: Optional[tuple] = None   # (programme, ts) vu machine prête
        self._seen_option: Optional[tuple] = None
        self._cycle_started_at: Optional[datetime] = None                # Wh par tranche de 5 min du cycle en cours
        self._house_history: list = []            # [(ts, W maison)] sur 20 min
        self._baseline_w = 0.0
        self.last_cycle: dict = {}
        self._file = os.path.join(datadir, f'profiles_{key}.json')
        self.profiles, self.cycles = self._load()

    # ── persistance ───────────────────────────────────────────────
    def _load(self):
        try:
            with open(self._file) as fh:
                d = json.load(fh)
            return d.get('profiles', {}), d.get('cycles', [])
        except Exception:  # noqa: BLE001
            return {}, []

    def _save(self):
        try:
            with open(self._file, 'w') as fh:
                json.dump({'profiles': self.profiles, 'cycles': self.cycles[-self.MAX_CYCLES:]},
                          fh, indent=1, ensure_ascii=False)
        except OSError:
            pass

    # ── programme et option ───────────────────────────────────────
    def program(self) -> str:
        if not self.c('cmd_program'):
            return self._program
        # valeur courante uniquement : un programme mémorisé relancerait la machine après son cycle
        self._program = self.bus.text(self.c('cmd_program'), '')
        return self._program

    EMPTY_PROGRAM = ('', '-', '--', 'none', 'null', 'aucun', 'unknown', 'inconnu', '0', 'off', 'standby')

    def running_program(self) -> str:
        """Programme réellement en cours, lu pendant le cycle : commande « retour d'état » si
        renseignée, sinon « programme sélectionné ». Vide tant que la machine ne l'affiche pas."""
        cmd = self.c('cmd_program_state') or self.c('cmd_program')   # par défaut : « Programme sélectionné »
        v = self.bus.text(cmd, '').strip() if cmd else ''
        return '' if v.lower() in self.EMPTY_PROGRAM else v

    def option(self) -> str:
        """Température de lavage ou niveau de séchage, normalisé (ex. « 40 »)."""
        v = self.bus.text(self.c('cmd_temperature'), '')
        if not v:
            return ''
        try:
            return str(int(round(float(v.replace('°c', '').replace('°', '').strip()))))
        except ValueError:
            return v

    def profile_key(self, program: Optional[str] = None, option: Optional[str] = None) -> str:
        prog = (self.program() if program is None else program) or 'defaut'
        opt = self.option() if option is None else option
        return f'{prog}|{opt}' if opt else prog

    def ready(self) -> bool:
        if self.c('cmd_hon_waiting') and not self.bus.flag(self.c('cmd_hon_waiting')):
            return False
        if not self.c('cmd_program'):
            # pas de commande programme (ex. sèche-linge) : « connecté hOn » suffit
            return bool(self.c('cmd_hon_waiting'))
        return bool(self.program())

    # ── estimation à partir des profils appris ────────────────────
    def _estimate(self, field_name: str, default: float) -> tuple:
        """(valeur, source) pour le programme/option courants."""
        prog = self.program() or self._saved_program or 'defaut'
        opt = self.option() or self._saved_option
        exact = self.profiles.get(self.profile_key(prog, opt))
        if exact and exact.get('n', 0) >= 1:
            return float(exact[field_name]), f'appris ({exact["n"]} cycle(s))'
        # même programme, autres options : régression linéaire sur l'option numérique
        same = [(k.split('|', 1)[1], v) for k, v in self.profiles.items()
                if k.split('|', 1)[0] == prog and '|' in k and v.get('n', 0) >= 1]
        pts = []
        for o, v in same:
            try:
                pts.append((float(o), float(v[field_name])))
            except ValueError:
                pass
        try:
            x = float(opt) if opt else None
        except ValueError:
            x = None
        if x is not None and len(pts) >= 2:
            n = len(pts)
            mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
            den = sum((p[0] - mx) ** 2 for p in pts)
            slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else 0.0
            return max(0.0, my + slope * (x - mx)), f'interpolé ({n} options du programme)'
        if same:
            vals = [float(v[field_name]) for _, v in same]
            return sum(vals) / len(vals), 'moyenne du programme'
        plain = self.profiles.get(prog)
        if plain and plain.get('n', 0) >= 1:
            return float(plain[field_name]), 'moyenne du programme'
        alls = [float(v[field_name]) for v in self.profiles.values() if v.get('n', 0) >= 1]
        if alls:
            return sum(alls) / len(alls), 'moyenne de tous les cycles'
        return default, 'valeur par défaut'

    def expected_curve(self) -> tuple:
        """Courbe prévue (Wh par tranche de 5 min) pour le programme/option en cours + source."""
        energy, _ = self._estimate('energy_wh', self.f('cycle_energy_wh', 1500))
        dur_mn = self._estimate('duration_mn', 120.0)[0]
        prog = self.program() or self._saved_program or 'defaut'
        opt = self.option() or self._saved_option
        exact = self.profiles.get(self.profile_key(prog, opt))
        curve, src = None, None
        if exact and exact.get('curve'):
            curve, src = exact['curve'], 'courbe apprise'
        else:
            cands = [(k.split('|', 1)[1] if '|' in k else '', v) for k, v in self.profiles.items()
                     if k.split('|', 1)[0] == prog and v.get('curve')]
            if cands:
                def dist(o):
                    try:
                        return abs(float(o) - float(opt))
                    except ValueError:
                        return 999.0
                curve, src = min(cands, key=lambda c: dist(c[0]))[1]['curve'], 'courbe du programme (option la plus proche)'
        if curve and sum(curve) > 0:
            k = energy / sum(curve)
            return [x * k for x in curve], src
        # forme type si rien d'appris : lave-linge = chauffe au début, sèche-linge = plat
        n = max(1, int(round(dur_mn * 60 / self.SLOT_S)))
        if self.key == 'washer':
            head = max(1, n // 4)
            w = [3.0 if i < head else 0.6 for i in range(n)]
        else:
            w = [1.0] * n
        tot = sum(w)
        return [energy * x / tot for x in w], 'forme type (pas encore de courbe apprise)'

    def expected_power_now(self) -> float:
        """Puissance attendue à cet instant du cycle d'après la courbe (si pas de mesure directe)."""
        if not self.running:
            return 0.0
        curve, _ = self.expected_curve()
        slot = int((time.time() - self._cycle_start) // self.SLOT_S)
        return curve[slot] * 3600 / self.SLOT_S if 0 <= slot < len(curve) else 0.0

    def peak_remaining_mn(self, threshold_w: float = 1000.0) -> float:
        """Minutes restantes de la phase gourmande en cours (ex. chauffe de l'eau), d'après la courbe apprise."""
        if not self.running:
            return 0.0
        curve, _ = self.expected_curve()
        slot = int((time.time() - self._cycle_start) // self.SLOT_S)
        n = 0
        for i in range(max(0, slot), len(curve)):
            if curve[i] * 3600 / self.SLOT_S >= threshold_w:
                n += 1
            else:
                break
        return n * self.SLOT_S / 60.0

    def cycle_energy_wh(self) -> float:
        return self._estimate('energy_wh', self.f('cycle_energy_wh', 1500))[0]

    def cycle_duration_h(self) -> float:
        return self._estimate('duration_mn', 120.0)[0] / 60.0

    # ── suivi du cycle ────────────────────────────────────────────
    def _house_baseline(self, now: float) -> float:
        pts = [w for t, w in self._house_history if now - t <= 900]
        return sum(pts) / len(pts) if pts else 0.0

    def _start_tracking(self, now: float):
        self._cycle_start = now
        self._energy_acc = 0.0
        self._house_acc = 0.0
        self._curve_cur = []
        self._last_sample = now
        self._baseline_w = self._house_baseline(now)
        self._energy_counter_start = self.bus.num(self.c('cmd_energy'), None) if self.c('cmd_energy') else None
        if not self._saved_program:
            self._saved_program = self.program()
        # hOn efface souvent le programme sélectionné au démarrage : on reprend celui vu juste avant
        if not self._saved_program and self._seen_program and now - self._seen_program[1] < 3600:
            self._saved_program = self._seen_program[0]
        opt = self.option()
        if opt not in ('', '0'):
            self._saved_option = opt
        elif not self._saved_option and self._seen_option and now - self._seen_option[1] < 3600:
            self._saved_option = self._seen_option[0]
        self._cycle_started_at = datetime.now()

    def sync(self, snap: Snapshot):
        now = time.time()
        if not self.running:
            p, o = self.program(), self.option()
            if p:
                self._seen_program = (p, now)
            if o not in ('', '0'):
                self._seen_option = (o, now)
            self._house_history.append((now, snap.load_power))
            self._house_history = [(t, w) for t, w in self._house_history if now - t <= 1200]
        if self.c('cmd_remaining'):
            self.remaining_mn = self.bus.num(self.c('cmd_remaining'), 0.0) or 0.0
            real_on = self.remaining_mn > 0
        elif self.c('cmd_power'):
            real_on = self.power() > 50
        else:
            # ni temps restant ni puissance : cycle considéré terminé après sa durée estimée
            real_on = self.running and (now - self._last_start_ts) < self.cycle_duration_h() * 3600
        if real_on and not self.running:
            self.running = True
            self._fails = 0
            self.circuit_open = False
            if now - self._last_start_ts > 120:
                # démarrage manuel : on mémorise tout de suite le programme et la température affichés
                self._saved_program = self.program()
                self._saved_option = self.option()
                if self.override == 'auto':
                    self.override = 'manual'
                    log.info('%s → démarré hors plugin', self.name)
            self._start_tracking(now)
        if real_on and self.running:
            dt = now - self._last_sample
            house_part = max(0.0, snap.load_power - self._baseline_w) * dt / 3600.0
            self._house_acc += house_part
            if self.c('cmd_power'):
                part = self.power() * dt / 3600.0
                self._energy_acc += part
            else:
                part = house_part
            slot = int((now - self._cycle_start) // self.SLOT_S)
            while len(self._curve_cur) <= slot:
                self._curve_cur.append(0.0)
            self._curve_cur[slot] += part
            self._last_sample = now
            # « Programme sélectionné » est lu dès la sélection (avant le départ) ; après le départ, hOn
            # le rafraîchit toutes les minutes (cron) : la valeur lue à partir d'1 min confirme le programme
            # réellement en cours et fait foi
            rp = self.running_program() if now - self._cycle_start >= self.PROGRAM_DELAY_S else ''
            if rp and rp != self._saved_program:
                log.info('%s → programme lu pendant le cycle : %s (avant : %s)', self.name, rp,
                         self._saved_program or 'inconnu')
                self._saved_program = rp
            elif not self._saved_program:
                self._saved_program = self.program()
            opt = self.option() if now - self._cycle_start >= self.PROGRAM_DELAY_S else ''
            if opt not in ('', '0') and opt != self._saved_option:
                self._saved_option = opt          # la température s'affiche parfois après le départ
        if not real_on and self.running and now - self._last_start_ts > self.START_GRACE_S:
            self.running = False
            self._end_cycle(now)
            if self.override in ('manual', 'force'):
                self.override = 'auto'

    def _end_cycle(self, now: float):
        dur = (now - self._cycle_start) / 60.0
        prog = self._saved_program or self._program or 'defaut'
        opt = self._saved_option
        energy, method = None, None
        if self.c('cmd_power') and self._energy_acc > 50:
            energy, method = self._energy_acc, 'puissance appareil'
        elif self.c('cmd_energy') and self._energy_counter_start is not None:
            end = self.bus.num(self.c('cmd_energy'), None)
            if end is not None and end >= self._energy_counter_start:
                energy, method = (end - self._energy_counter_start) * 1000.0, 'compteur appareil'
        if energy is None and self._house_acc > 100:
            energy, method = self._house_acc, 'surconso maison (estimation)'
        if dur < self.MIN_CYCLE_MN:
            # faux départ (pause/reprise, mise en route annulée…) : rien n'est enregistré
            self._saved_program, self._saved_option = '', ''
            if now - self._last_start_ts < self.START_GRACE_S + 120:
                # lancé par enerSol mais la machine n'a pas démarré : pas de nouvel essai avant 30 min
                self._retry_after = now + 1800
                log.warning('%s → la machine n\'a pas démarré après l\'ordre : nouvel essai dans 30 min', self.name)
            log.info('%s → faux départ ignoré (%.0f mn, non enregistré)', self.name, dur)
            return
        start_dt = self._cycle_started_at or (datetime.now() - timedelta(minutes=dur))
        cycle = {
            'date': start_dt.strftime('%Y-%m-%d %H:%M'),          # heure de DÉPART
            'end': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'program': prog, 'option': opt, 'duration_mn': round(dur),
            'energy_wh': round(energy) if energy is not None else None, 'method': method,
            'by_plugin': now - self._last_start_ts < self.cycle_duration_h() * 3600 + 600,
        }
        self.cycles.append(cycle)
        self.last_cycle = cycle
        self.finished_event = cycle            # lu (et remis à None) par le contrôleur
        if dur > 10 and energy is not None and energy > 50:
            key = self.profile_key(prog, opt)
            p = self.profiles.get(key, {'energy_wh': 0.0, 'duration_mn': 0.0, 'n': 0})
            # mesure estimée (surconso maison) : poids réduit dans la moyenne
            w = 0.5 if method == 'surconso maison (estimation)' else 1.0
            n = min(p['n'], 4)
            p['energy_wh'] = round((p['energy_wh'] * n + energy * w + p['energy_wh'] * (1 - w) * (n > 0)) / (n + 1) if n else energy, 0)
            p['duration_mn'] = round((p['duration_mn'] * n + dur) / (n + 1), 0)
            p['n'] = p['n'] + 1
            p['method'] = method
            curve = [round(x, 1) for x in self._curve_cur]
            if sum(curve) > 0:
                # recalage : la courbe mesurée est ramenée à l'énergie retenue pour ce cycle
                k = energy / sum(curve)
                curve = [round(x * k, 1) for x in curve]
                old_c = p.get('curve') or []
                if old_c:
                    m = min(p['n'] - 1, 4)
                    L = max(len(old_c), len(curve))
                    oc = old_c + [0.0] * (L - len(old_c))
                    nc = curve + [0.0] * (L - len(curve))
                    curve = [round((oc[i] * m + nc[i] * w + oc[i] * (1 - w)) / (m + 1), 1) for i in range(L)]
                p['curve'] = curve
                p['peak_w'] = round(max(curve) * 3600 / self.SLOT_S)
            self.profiles[key] = p
        self._save()
        self._saved_program, self._saved_option = '', ''
        log.info('%s → cycle terminé : %s %s, %.0f mn, %s Wh (%s)', self.name, prog,
                 f'{opt}°' if opt.isdigit() else opt, dur,
                 cycle['energy_wh'] if energy is not None else '?', method or 'énergie non mesurable')

    def last_cycle_text(self) -> str:
        c = self.last_cycle or (self.cycles[-1] if self.cycles else None)
        if not c:
            return ''
        opt = c.get('option') or ''
        opt = f' · {opt}°C' if opt.isdigit() and self.key == 'washer' else (f' · {opt}' if opt else '')
        e = f" · {c['energy_wh'] / 1000:.2f} kWh" if c.get('energy_wh') else ''
        h, m = divmod(int(c.get('duration_mn') or 0), 60)
        d = c['date']
        day = 'aujourd\'hui' if d[:10] == datetime.now().strftime('%Y-%m-%d') else \
            ('hier' if d[:10] == (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d') else d[8:10] + '/' + d[5:7])
        return f"lancé {day} {d[11:16]} · {pretty_program(c['program'])}{opt}{e} · {h}h{m:02d}"

    def rename_profile(self, key: str, program: str):
        """Change le nom du programme d'un profil appris (ex. « defaut » → « iot_wash_synthetic »),
        en gardant la température. Si le profil cible existe déjà, les deux sont fusionnés."""
        program = str(program or '').strip()
        if key not in self.profiles or not program:
            return
        opt = key.split('|', 1)[1] if '|' in key else ''
        new_key = self.profile_key(program, opt)
        if new_key == key:
            return
        src = self.profiles.pop(key)
        dst = self.profiles.get(new_key)
        if dst:
            n1, n2 = dst.get('n', 0), src.get('n', 0)
            tot = max(1, n1 + n2)
            for f in ('energy_wh', 'duration_mn'):
                dst[f] = round((dst.get(f, 0) * n1 + src.get(f, 0) * n2) / tot, 0)
            c1, c2 = dst.get('curve') or [], src.get('curve') or []
            if c1 and c2:
                L = max(len(c1), len(c2))
                c1, c2 = c1 + [0.0] * (L - len(c1)), c2 + [0.0] * (L - len(c2))
                dst['curve'] = [round((c1[i] * n1 + c2[i] * n2) / tot, 1) for i in range(L)]
                dst['peak_w'] = round(max(dst['curve']) * 3600 / self.SLOT_S)
            elif c2:
                dst['curve'], dst['peak_w'] = c2, src.get('peak_w')
            dst['n'] = n1 + n2
        else:
            self.profiles[new_key] = src
        for c in self.cycles:
            if self.profile_key(c.get('program') or 'defaut', c.get('option') or '') == key:
                c['program'] = program
        self._save()
        log.info('%s : programme appris « %s » renommé en « %s »', self.name, key, new_key)

    def habitual_profile(self) -> Optional[tuple]:
        """(clé, profil) du programme le plus utilisé — sert à prévoir la machine suivante."""
        best = max(self.profiles.items(), key=lambda kv: kv[1].get('n', 0), default=None)
        return best if best and best[1].get('n') else None

    def selected_program_text(self) -> str:
        """Pour le tableau de bord : programme sélectionné (ou en cours) et ce qu'enerSol en sait."""
        running = self.running
        prog = (self._saved_program or self.program()) if running else self.program()
        opt = (self._saved_option or self.option()) if running else self.option()
        if not prog:
            return 'en cours (programme pas encore lu)' if running else 'aucun programme sélectionné'
        o = f' · {opt}°C' if opt.isdigit() and self.key == 'washer' else (f' · {opt}' if opt else '')
        txt = ('en cours : ' if running else '') + pretty_program(prog) + o
        p = self.profiles.get(self.profile_key(prog, opt))
        if p and p.get('n'):
            h, m = divmod(int(p.get('duration_mn') or 0), 60)
            pic = f" · pic {p['peak_w']} W" if p.get('peak_w') else ''
            txt += f" — appris ({p['n']} cycle{'s' if p['n'] > 1 else ''} · {p['energy_wh'] / 1000:.2f} kWh · {h}h{m:02d}{pic})"
        else:
            est, src = self._estimate('energy_wh', self.f('cycle_energy_wh', 1500))
            txt += f" — pas encore appris (estimation {est / 1000:.1f} kWh : {src})"
        return txt

    def forget_profile(self, key: str):
        """Oublie un programme appris (et ses cycles) — demandé depuis la page de l'équipement."""
        if key in self.profiles:
            del self.profiles[key]
            self.cycles = [c for c in self.cycles
                           if self.profile_key(c.get('program') or 'defaut', c.get('option') or '') != key]
            self._save()
            log.info('%s : programme appris « %s » oublié', self.name, key)


    def is_running(self): return self.running
    def power(self): return self.bus.num(self.c('cmd_power'), 0.0) or 0.0
    def nominal_power(self):
        if self.running:
            return self.power() if self.power() > 0 else self.expected_power_now()
        curve, _ = self.expected_curve()
        return max(curve) * 3600 / self.SLOT_S if curve else 1500.0
    def min_surplus(self): return self.f('min_surplus', 1500)
    def can_be_stopped(self): return False

    @property
    def observe_only(self) -> bool:
        return bool(self.cfg.get('observe_only', False))

    def can_pilot(self) -> bool:
        """enerSol a-t-il le droit de lancer la machine lui-même ?"""
        return self.enabled and not self.observe_only and bool(self.c('cmd_start'))

    def pilot_text(self) -> str:
        if not self.enabled:
            return 'désactivé (aucune commande)'
        if self.observe_only:
            return 'conseil seulement — vous lancez la machine'
        if not self.c('cmd_start'):
            return 'conseil seulement — commande Démarrer non renseignée'
        if not self.ready():
            return 'enerSol peut lancer — démarrage à distance non activé sur la machine'
        return 'enerSol lance la machine au bon moment (démarrage à distance prêt)'

    def turn_on(self, force: bool = False):
        if self.circuit_open or not self.enabled or self.running:
            return False
        if self.observe_only and not force:
            return False                            # mode « ne rien faire » : on regarde, on enregistre
        if not force and time.time() < getattr(self, '_retry_after', 0.0):
            return False
        now = time.time()
        self._saved_program = self.program()
        self._saved_option = self.option()
        est, src = self._estimate('energy_wh', self.f('cycle_energy_wh', 1500))
        log.info('%s → DÉMARRAGE (%s, estimé %.0f Wh — %s)', self.name,
                 self.profile_key(self._saved_program, self._saved_option), est, src)
        self.running = True
        self._last_start_ts = now
        self._start_tracking(now)
        return self.bus.exec(self.c('cmd_start'), on_result=self._on_start_result)

    def turn_off(self):
        return False

    def status(self):
        s = super().status()
        est, src = self._estimate('energy_wh', self.f('cycle_energy_wh', 1500))
        s.update({'program': self.program(), 'option': self.option(), 'ready': self.ready(),
                  'remaining_mn': self.remaining_mn, 'cycle_energy_wh': round(est),
                  'cycle_energy_source': src, 'cycle_duration_h': round(self.cycle_duration_h(), 1),
                  'last_cycle': self.last_cycle_text()})
        return s


# ═══════════════════════════════════════════════════════════════════
# CHAUFFE-EAU
# ═══════════════════════════════════════════════════════════════════

class WaterHeater(BaseDevice):

    key = 'water_heater'
    name = 'Chauffe-eau'
    POWER_THRESHOLD = 50.0
    RETRY_S = 900.0                     # ballon déjà chaud (pas de chauffe après ON) : nouvel essai 15 min après

    def __init__(self, bus: Bus):
        super().__init__(bus)
        self.priority = 6
        self._last_power = 0.0
        self.started_by_us = False      # vrai seulement si c'est enerSol qui a lancé la chauffe
        self.thermo_state = ''          # thermodynamique : '' (inconnu) | 'auto' | 'stop'
        self.thermo_reason = ''
        self._stop_since = 0.0
        self._last_on_ts = 0.0
        self.energy_today_wh = 0.0
        self._last_ts = time.time()

    def sync(self, snap: Snapshot):
        self._last_power = self.power()
        now = time.time()
        if self.is_running():
            self.energy_today_wh += self._last_power * (now - self._last_ts) / 3600.0
        self._last_ts = now

    def reset_daily(self):
        self.energy_today_wh = 0.0

    # ── thermodynamique ──
    def is_thermo(self) -> bool:
        return str(self.cfg.get('wh_type', 'resistif')) == 'thermo'

    def set_thermo(self, auto: bool, reason: str):
        """Auto / Arrêt pour un chauffe-eau thermodynamique (envoyé seulement au changement)."""
        want = 'auto' if auto else 'stop'
        if self.thermo_state == want:
            return
        cmd = self.c('cmd_on' if auto else 'cmd_off')
        if not cmd:
            return
        log.info('Chauffe-eau → %s (%s)', 'AUTO' if auto else 'ARRÊT', reason)
        self.bus.exec(cmd)
        self.thermo_state = want
        self.thermo_reason = reason
        if not auto:
            self._stop_since = time.time()

    def solar_dump_enabled(self) -> bool:
        return bool(self.cfg.get('wh_enabled', False))

    def is_running(self): return self._last_power >= self.POWER_THRESHOLD
    def power(self): return self.bus.num(self.c('cmd_power'), 0.0) or 0.0
    def nominal_power(self): return self.f('nominal_w', 2000)
    def min_surplus(self): return self.f('min_surplus', 1000)

    def turn_on(self):
        if self.circuit_open or not self.enabled:
            return False
        if time.time() - self._last_on_ts < self.RETRY_S:
            return False                # ON déjà envoyé récemment sans chauffe : ballon chaud, on attend
        log.info('Chauffe-eau → ON (surplus solaire)')
        self._last_on_ts = time.time()
        self.started_by_us = True
        return self.bus.exec(self.c('cmd_on'), on_result=self._on_start_result)

    def turn_off(self, force: bool = False):
        # enerSol n'arrête QUE la chauffe qu'il a lui-même lancée (jamais celle des heures creuses
        # ou du thermostat du ballon), sauf commande OFF explicite de l'utilisateur
        if self.started_by_us or (force and self.is_running()):
            log.info('Chauffe-eau → OFF')
            self.started_by_us = False
            return self.bus.exec(self.c('cmd_off'))
        return True

    def status(self):
        s = super().status()
        s['energy_today_wh'] = round(self.energy_today_wh, 0)
        return s


# ═══════════════════════════════════════════════════════════════════
# VMI (ventilation par insufflation : ventilateur + résistance 0-10 V + registres)
# ═══════════════════════════════════════════════════════════════════

class VMI(BaseDevice):
    """VMI : choisit la meilleure source d'air (combles / capteur solaire, extérieur, recyclage),
    règle le ventilateur, et module la résistance sur le seul surplus solaire perdu."""

    key = 'vmi'
    name = 'VMI'
    SOURCE_FR = {'attic': 'combles / capteur solaire', 'ext': 'air extérieur', 'recycle': 'recyclage air intérieur'}

    def __init__(self, bus: Bus):
        super().__init__(bus)
        self.priority = 7
        self.source = ''
        self._source_ts = 0.0
        self.fan_pct = None
        self.heater_pct = 0.0
        self.reason = ''

    # lectures
    def t(self, k: str) -> Optional[float]:
        return self.bus.num(self.c('cmd_t_' + k), None) if self.c('cmd_t_' + k) else None

    def heater_max_w(self) -> float: return self.f('heater_max_w', 2000)
    def power(self) -> float:
        if self.c('cmd_power'):
            return self.bus.num(self.c('cmd_power'), 0.0) or 0.0
        return self.heater_pct / 100 * self.heater_max_w()
    def is_running(self) -> bool: return self.heater_pct > 0
    def nominal_power(self): return self.heater_max_w()

    # actions (envoyées seulement au changement)
    def set_source(self, src: str, why: str):
        if src == self.source or time.time() - self._source_ts < self.f('source_min_s', 600):
            return
        cmd = self.c({'attic': 'cmd_air_attic', 'ext': 'cmd_air_ext', 'recycle': 'cmd_air_recycle'}[src])
        if not cmd:
            return
        self.bus.exec(cmd)
        log.info('VMI → %s (%s)', self.SOURCE_FR[src], why)
        self.source, self._source_ts = src, time.time()

    def set_fan(self, pct: float, why: str = ''):
        pct = round(max(self.f('fan_min_pct', 30), min(self.f('fan_max_pct', 100), pct)))
        if self.c('cmd_fan') and (self.fan_pct is None or abs(pct - self.fan_pct) >= 5):
            self.bus.exec(self.c('cmd_fan'), slider=pct)
            if why:
                log.info('VMI → ventilateur %d %% (%s)', pct, why)
            self.fan_pct = pct

    def set_heater(self, pct: float):
        pct = max(0.0, min(100.0, round(pct / 5) * 5))
        if self.c('cmd_heater') and abs(pct - self.heater_pct) >= 5:
            self.bus.exec(self.c('cmd_heater'), slider=int(pct))
            self.heater_pct = pct
            self._heater_ts = time.time()
        elif not self.c('cmd_heater'):
            self.heater_pct = 0.0

    def status(self):
        s = super().status()
        s.update({'source': self.SOURCE_FR.get(self.source, '?'), 'ventilateur_pct': self.fan_pct,
                  'resistance_pct': self.heater_pct, 'resistance_w': round(self.power()),
                  'temperatures': {k: self.t(k) for k in ('int', 'ext', 'attic', 'blow') if self.t(k) is not None},
                  'raison': self.reason})
        return s


# ═══════════════════════════════════════════════════════════════════
# VOITURE (borne de recharge)
# ═══════════════════════════════════════════════════════════════════

class EVCharger(BaseDevice):
    """Recharge de la voiture : sur le surplus solaire (intensité modulée si la borne le permet),
    et protection de la batterie maison quand la voiture charge hors surplus (décharge coupée)."""

    key = 'ev'
    name = 'Voiture'
    CHARGING_W = 500.0

    def __init__(self, bus: Bus):
        super().__init__(bus)
        self.priority = 5
        self.started_by_us = False
        self.current_a = 0
        self.batt_blocked = None        # None = inconnu, True / False = dernier ordre envoyé
        self.reason = ''
        self._last_cmd = 0.0

    # lectures
    def power(self): return self.bus.num(self.c('cmd_power'), 0.0) or 0.0
    def volts(self): return self.f('volts', 230) * (3 if str(self.cfg.get('phases', '1')) == '3' else 1)
    def min_w(self): return self.f('min_a', 6) * self.volts()
    def nominal_power(self): return self.f('max_a', 16) * self.volts() if self.c('cmd_current') else self.f('nominal_w', 3700)
    def min_surplus(self): return self.min_w() if self.c('cmd_current') else self.f('min_surplus', 3000)

    def plugged(self) -> bool:
        if self.c('cmd_plugged'):
            return self.bus.flag(self.c('cmd_plugged'))
        return True

    def is_running(self) -> bool:
        if self.c('cmd_charging'):
            return self.bus.flag(self.c('cmd_charging'))
        return self.power() >= self.CHARGING_W

    # actions
    def set_current(self, a: int):
        if not self.c('cmd_current'):
            return
        a = int(max(self.f('min_a', 6), min(self.f('max_a', 16), a)))
        if a != self.current_a:
            self.bus.exec(self.c('cmd_current'), slider=a)
            self.current_a = a

    def turn_on(self, reason: str = 'surplus solaire'):
        if self.circuit_open or not self.enabled or time.time() - self._last_cmd < 60:
            return False
        log.info('Voiture → CHARGE (%s)', reason)
        self._last_cmd = time.time()
        self.started_by_us = True
        self.reason = reason
        return self.bus.exec(self.c('cmd_on'), on_result=self._on_start_result)

    def turn_off(self, reason: str = ''):
        if not self.c('cmd_off') or time.time() - self._last_cmd < 60:
            return False
        log.info('Voiture → ARRÊT de la charge (%s)', reason)
        self._last_cmd = time.time()
        self.started_by_us = False
        self.reason = reason
        return self.bus.exec(self.c('cmd_off'))

    def block_battery(self, block: bool, why: str):
        """Coupe (ou rétablit) la décharge de la batterie maison pendant une charge hors surplus."""
        if self.batt_blocked is block:
            return
        cmd = self.c('cmd_batt_block' if block else 'cmd_batt_unblock')
        slider = self.c('cmd_batt_discharge_slider')
        if not cmd and not slider:
            return
        if cmd:
            self.bus.exec(cmd)
        if slider:
            self.bus.exec(slider, slider=0 if block else int(self.f('batt_discharge_normal_w', 5000)))
        self.batt_blocked = block
        log.info('Batterie maison → décharge %s (%s)', 'COUPÉE' if block else 'rétablie', why)

    def status(self):
        s = super().status()
        s.update({'plugged': self.plugged(), 'current_a': self.current_a, 'started_by_us': self.started_by_us,
                  'battery_discharge_blocked': bool(self.batt_blocked), 'reason': self.reason,
                  'mode': self.cfg.get('ev_mode', 'surplus')})
        return s


# ═══════════════════════════════════════════════════════════════════
# BATTERIE
# ═══════════════════════════════════════════════════════════════════

class Battery(BaseDevice):
    """Gestion de la charge sur le solaire : répartition sur la journée et charge prioritaire de fin de
    journée. Jamais de recharge depuis le réseau."""

    key = 'battery'
    name = 'Batterie'
    HYSTERESIS_W = 100.0

    def __init__(self, bus: Bus):
        super().__init__(bus)
        self.priority = 0
        self._last_slider = -1.0
        self.force_active = False
        self.soc_min_today = 100.0
        self.reason = ''

    def reset_daily(self):
        self.soc_min_today = 100.0

    def sync(self, snap: Snapshot):
        self.soc_min_today = min(self.soc_min_today, snap.battery_soc)

    def _slider(self, w: float):
        if not self.c('cmd_charge_slider'):
            return
        w = max(0.0, min(self.f('charge_max_w', 3500), round(w)))
        if abs(w - self._last_slider) < self.HYSTERESIS_W:
            return
        self.bus.exec(self.c('cmd_charge_slider'), slider=int(w))
        self._last_slider = w

    def _force(self, on: bool):
        if on == self.force_active:
            return
        cmd = self.c('cmd_force_charge_on' if on else 'cmd_force_charge_off')
        if cmd:
            self.bus.exec(cmd)
        self.force_active = on
        log.info('Batterie → charge forcée %s', 'ON' if on else 'OFF')

    def run(self, snap: Snapshot, loads_w: float, limits: Limits):
        if not self.enabled:
            return
        if not snap.in_solar_window:
            self._force(False)
            self._slider(0)
            self.reason = 'nuit'
            return

        # ── charge forcée de fin de journée sur solaire ──
        recharge = limits.recharge_wh(snap.battery_soc)
        force = (snap.battery_soc < 100 and
                 self.soc_min_today < self.f('force_charge_soc', 95) and
                 snap.forecast_remain <= recharge * 1.1)
        if self.directive.get('priority_charge'):
            force = snap.battery_soc < 100
        self._force(force)

        if force:
            target = max(0.0, snap.pv_power - loads_w)
            self.reason = f'charge prioritaire ({target:.0f} W)'
        elif recharge <= 0:
            target = 0.0
            self.reason = 'batterie pleine'
        else:
            # répartition de la recharge au prorata du solaire prévu
            now_h = snap.now.hour
            frac_hour = max(0.05, (60 - snap.now.minute) / 60.0)
            remain = {h: w for h, w in snap.forecast_by_hour.items() if h >= now_h}
            total = sum(remain.values())
            avail = max(0.0, snap.pv_power - loads_w)
            if total > 0:
                need = recharge * (remain.get(now_h, 0.0) / total) / frac_hour
            else:
                need = recharge / max(0.5, snap.solar_end_h - now_h)
            target = min(need, avail)
            self.reason = f'répartition Solcast ({target:.0f} W)'
        self._slider(target)

    def status(self):
        s = super().status()
        s.update({'force_active': self.force_active,
                  'slider_w': self._last_slider, 'soc_min_today': round(self.soc_min_today, 1),
                  'reason': self.reason})
        return s

    def is_running(self): return self.force_active
