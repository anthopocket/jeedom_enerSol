"""Prévision PV multi-jours, auto-calibrée sur la production réelle.

- Open-Meteo : irradiance sur le plan des panneaux (GTI) + température, J à J+3.
- Calibration : coefficient k[saison][heure] = Wh produits / GTI, appris en continu
  (moyenne exponentielle). Il intègre puissance crête, rendement ET ombrages
  propres à chaque heure (soleil bas d'hiver derrière un arbre, etc.).
- Solcast (commandes Jeedom) reste prioritaire pour aujourd'hui, et fixe le total
  de demain quand il est disponible.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

import aiohttp

log = logging.getLogger('enerSol.forecast')

OPEN_METEO = 'https://api.open-meteo.com/v1/forecast'
OPEN_METEO_ARCHIVE = 'https://archive-api.open-meteo.com/v1/archive'


def season_of(d) -> str:
    m = d.month
    return 'winter' if m in (12, 1, 2) else 'spring' if m in (3, 4, 5) else 'summer' if m in (6, 7, 8) else 'autumn'


class PVForecaster:

    ALPHA = 0.15          # vitesse d'apprentissage du coefficient horaire
    MIN_GTI = 15.0        # W/m² — en dessous, mesure non significative
    DEFAULT_K = 2.4       # Wh par (W/m²) ≈ 3 kWc × 0,8 — remplacé dès le premier apprentissage

    def __init__(self, datadir: str):
        self._file = os.path.join(datadir, 'pv_model.json')
        self.k: dict = {}                 # {saison: {heure: k}}
        self.weather: dict = {}           # {'YYYY-MM-DD HH': {'gti': , 't': , 'cloud': }}
        self.daily_real: dict = {}        # {'YYYY-MM-DD': Wh mesurés}
        self.hour_real: dict = {}         # {'YYYY-MM-DD HH': (Wh mesurés, bridé)} — 48 h
        self.daily_pred: dict = {}        # {'YYYY-MM-DD': Wh prévus le matin}
        self._cur_hour_key = ''
        self._cur_hour_wh = 0.0
        self._cur_hour_tainted = False
        self._last_ts: Optional[datetime] = None
        self._load()

    # ── persistance ───────────────────────────────────────────────
    def _load(self):
        try:
            with open(self._file) as fh:
                d = json.load(fh)
            self.k = {s: {int(h): float(v) for h, v in hours.items()} for s, hours in d.get('k', {}).items()}
            self.daily_real = d.get('daily_real', {})
            self.daily_pred = d.get('daily_pred', {})
        except Exception:  # noqa: BLE001
            pass

    def save(self):
        keep = (date.today() - timedelta(days=60)).isoformat()
        self.daily_real = {k: v for k, v in self.daily_real.items() if k >= keep}
        self.daily_pred = {k: v for k, v in self.daily_pred.items() if k >= keep}
        try:
            with open(self._file, 'w') as fh:
                json.dump({'k': self.k, 'daily_real': self.daily_real, 'daily_pred': self.daily_pred}, fh, indent=1)
        except OSError as e:
            log.warning('Sauvegarde modèle PV : %s', e)

    # ── météo ─────────────────────────────────────────────────────
    async def fetch_temp_history(self, lat: float, lon: float, days: int = 45) -> list:
        """Températures extérieures horaires des derniers jours (Open-Meteo), pour apprendre
        le chauffage quand aucune sonde extérieure n'est configurée. [['YYYY-MM-DD HH', °C], …]"""
        params = {'latitude': lat, 'longitude': lon, 'hourly': 'temperature_2m',
                  'past_days': min(92, days), 'forecast_days': 1, 'timezone': 'Europe/Paris'}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.get(OPEN_METEO, params=params) as r:
                    if r.status != 200:
                        log.warning('Open-Meteo (historique températures) HTTP %s', r.status)
                        return []
                    data = await r.json()
        except Exception as e:  # noqa: BLE001
            log.warning('Open-Meteo (historique températures) : %s', e)
            return []
        h = data.get('hourly', {})
        out = [[t.replace('T', ' ')[:13], float(v)] for t, v in zip(h.get('time', []), h.get('temperature_2m', []))
               if v is not None]
        log.info('Open-Meteo : %d heures de températures passées (pas de sonde extérieure)', len(out))
        return out

    async def fetch_temp_archive(self, lat: float, lon: float, start: str, end: str) -> list:
        """Températures extérieures horaires MESURÉES sur plusieurs années (archive Open-Meteo, ~5 jours de
        retard), pour l'apprentissage du chauffage quand la sonde extérieure est récente."""
        params = {'latitude': lat, 'longitude': lon, 'hourly': 'temperature_2m', 'start_date': start,
                  'end_date': end, 'timezone': 'Europe/Paris'}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                async with s.get(OPEN_METEO_ARCHIVE, params=params) as r:
                    if r.status != 200:
                        log.warning('Open-Meteo (archive températures) HTTP %s', r.status)
                        return []
                    data = await r.json()
        except Exception as e:  # noqa: BLE001
            log.warning('Open-Meteo (archive températures) : %s', e)
            return []
        h = data.get('hourly', {})
        return [[t.replace('T', ' ')[:13], float(v)] for t, v in zip(h.get('time', []), h.get('temperature_2m', []))
                if v is not None]

    async def fetch(self, lat: float, lon: float, tilt: float, azimuth: float) -> bool:
        params = {
            'latitude': lat, 'longitude': lon, 'tilt': tilt, 'azimuth': azimuth,
            'hourly': 'temperature_2m,global_tilted_irradiance,cloud_cover',
            'forecast_days': 4, 'past_days': 1, 'timezone': 'Europe/Paris',
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.get(OPEN_METEO, params=params) as r:
                    if r.status != 200:
                        log.warning('Open-Meteo HTTP %s', r.status)
                        return False
                    data = await r.json()
        except Exception as e:  # noqa: BLE001
            log.warning('Open-Meteo : %s', e)
            return False
        h = data.get('hourly', {})
        times = h.get('time', [])
        gti = h.get('global_tilted_irradiance', [])
        tmp = h.get('temperature_2m', [])
        cloud = h.get('cloud_cover', [])
        w = {}
        for i, t in enumerate(times):
            key = t.replace('T', ' ')[:13]
            w[key] = {
                'gti': float(gti[i] or 0) if i < len(gti) and gti[i] is not None else 0.0,
                't': float(tmp[i]) if i < len(tmp) and tmp[i] is not None else None,
                'cloud': float(cloud[i]) if i < len(cloud) and cloud[i] is not None else None,
            }
        self.weather = w
        log.info('Open-Meteo : %d heures reçues', len(w))
        return True

    def temp_at(self, dt: datetime) -> Optional[float]:
        return (self.weather.get(dt.strftime('%Y-%m-%d %H')) or {}).get('t')

    def gti_at(self, dt: datetime) -> float:
        return (self.weather.get(dt.strftime('%Y-%m-%d %H')) or {}).get('gti', 0.0) or 0.0

    def cloud_at(self, dt: datetime) -> Optional[float]:
        return (self.weather.get(dt.strftime('%Y-%m-%d %H')) or {}).get('cloud')

    # ── apprentissage ─────────────────────────────────────────────
    def coef(self, season: str, hour: int) -> float:
        s = self.k.get(season) or {}
        if hour in s:
            return s[hour]
        for other in (self.k.get(x) or {} for x in ('autumn', 'spring', 'winter', 'summer')):
            if hour in other:
                return other[hour]
        vals = [v for hours in self.k.values() for v in hours.values()]
        return sum(vals) / len(vals) if vals else self.DEFAULT_K

    def _learn(self, dt: datetime, wh: float, weight: float = 1.0):
        g = self.gti_at(dt)
        if g < self.MIN_GTI:
            return
        ratio = max(0.0, wh / g)
        s = season_of(dt)
        hours = self.k.setdefault(s, {})
        a = self.ALPHA * weight
        hours[dt.hour] = ratio if dt.hour not in hours else (1 - a) * hours[dt.hour] + a * ratio

    def seed_from_solcast(self, by_hour: dict, today: date):
        """Amorce la calibration avec Solcast tant qu'on manque de mesures."""
        s = season_of(today)
        if len(self.k.get(s, {})) >= 8:
            return
        for h, wh in by_hour.items():
            if wh > 0:
                self._learn(datetime(today.year, today.month, today.day, h), wh, weight=0.5)

    def record_pv(self, now: datetime, pv_w: float, curtailed: bool = False):
        """Intègre la puissance PV mesurée ; apprend à chaque changement d'heure.

        `curtailed` : batterie pleine sans export → l'onduleur bride le PV, la mesure
        sous-estime le potentiel : l'heure n'est alors pas utilisée pour apprendre."""
        key = now.strftime('%Y-%m-%d %H')
        if self._last_ts is not None:
            dt_h = min(0.1, (now - self._last_ts).total_seconds() / 3600.0)
            if key == self._cur_hour_key:
                self._cur_hour_wh += max(0.0, pv_w) * dt_h
                self._cur_hour_tainted = self._cur_hour_tainted or curtailed
        if key != self._cur_hour_key:
            if self._cur_hour_key:
                prev = datetime.strptime(self._cur_hour_key, '%Y-%m-%d %H')
                self.hour_real[self._cur_hour_key] = (self._cur_hour_wh, self._cur_hour_tainted)
                if len(self.hour_real) > 72:
                    for k in sorted(self.hour_real)[:-48]:
                        del self.hour_real[k]
                if not self._cur_hour_tainted:
                    self._learn(prev, self._cur_hour_wh)
                d = prev.date().isoformat()
                self.daily_real[d] = round(self.daily_real.get(d, 0.0) + self._cur_hour_wh, 0)
                if prev.hour == 23:
                    self.save()
            self._cur_hour_key = key
            self._cur_hour_wh = 0.0
            self._cur_hour_tainted = curtailed
        self._last_ts = now

    # ── correction immédiate (nuages) ─────────────────────────────
    def nowcast_ratio(self, now: datetime, forecast_by_hour: dict) -> Optional[tuple]:
        """Production réelle / prévue sur les 2 dernières heures + l'heure en cours.
        Renvoie (ratio borné 0,3 – 1,2, réel Wh, prévu Wh) ou None si pas assez de données.
        Les heures bridées (batterie pleine) sont ignorées : la mesure y sous-estime le soleil."""
        real = pred = 0.0
        for back in (2, 1):
            dt = now - timedelta(hours=back)
            if dt.date() != now.date():
                continue
            r = self.hour_real.get(dt.strftime('%Y-%m-%d %H'))
            f = forecast_by_hour.get(dt.hour)
            if r and f and not r[1]:
                real += r[0]
                pred += f
        frac = now.minute / 60.0
        f_now = forecast_by_hour.get(now.hour)
        if frac >= 0.33 and f_now and self._cur_hour_key == now.strftime('%Y-%m-%d %H') and not self._cur_hour_tainted:
            real += self._cur_hour_wh
            pred += f_now * frac
        if pred < 300:
            return None
        return max(0.3, min(1.2, real / pred)), round(real), round(pred)

    # ── prévision ─────────────────────────────────────────────────
    def model_wh(self, dt: datetime) -> float:
        return self.gti_at(dt) * self.coef(season_of(dt), dt.hour)

    def hourly(self, start: datetime, hours: int, solcast_today: dict, solcast_tomorrow_total: float) -> list:
        """Liste [(datetime, Wh)] sur l'horizon, en combinant Solcast et le modèle calibré."""
        start = start.replace(minute=0, second=0, microsecond=0)
        today = start.date()
        tomorrow = today + timedelta(days=1)
        # facteur d'échelle de demain pour respecter le total Solcast
        scale_tomorrow = 1.0
        if solcast_tomorrow_total > 0:
            tm = sum(self.model_wh(datetime(tomorrow.year, tomorrow.month, tomorrow.day, h)) for h in range(24))
            if tm > 100:
                scale_tomorrow = solcast_tomorrow_total / tm
        # sans météo pour demain : total Solcast réparti selon la forme d'aujourd'hui
        tomorrow_has_weather = any(self.weather.get(f'{tomorrow.isoformat()} {h:02d}') for h in range(24))
        shape_total = sum(solcast_today.values())
        out = []
        for i in range(hours):
            dt = start + timedelta(hours=i)
            if dt.date() == today and dt.hour in solcast_today:
                wh = solcast_today[dt.hour]
            elif dt.date() == tomorrow:
                if not tomorrow_has_weather and solcast_tomorrow_total > 0 and shape_total > 0:
                    wh = solcast_tomorrow_total * solcast_today.get(dt.hour, 0.0) / shape_total
                else:
                    wh = self.model_wh(dt) * scale_tomorrow
            else:
                wh = self.model_wh(dt)
            out.append((dt, max(0.0, wh)))
        return out

    def day_total(self, d: date, solcast_today: dict, solcast_tomorrow_total: float) -> Optional[float]:
        """Total prévu du jour (Wh) ; None si aucune donnée météo pour ce jour."""
        today = date.today()
        if d == today and solcast_today:
            return sum(solcast_today.values())
        if d == today + timedelta(days=1) and solcast_tomorrow_total > 0:
            return solcast_tomorrow_total
        if not any(self.weather.get(f'{d.isoformat()} {h:02d}') for h in range(24)):
            return None
        return sum(self.model_wh(datetime(d.year, d.month, d.day, h)) for h in range(24))

    def day_cloud(self, d: date) -> Optional[float]:
        vals = [self.cloud_at(datetime(d.year, d.month, d.day, h)) for h in range(9, 17)]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    def note_morning_prediction(self, d: date, wh: float):
        k = d.isoformat()
        if k not in self.daily_pred:
            self.daily_pred[k] = round(wh, 0)

    def accuracy_yesterday(self) -> Optional[float]:
        y = (date.today() - timedelta(days=1)).isoformat()
        real, pred = self.daily_real.get(y), self.daily_pred.get(y)
        if not real or not pred or real < 200:
            return None
        return round(max(0.0, 100.0 * (1 - abs(pred - real) / real)), 0)

    def calibrated_hours(self) -> int:
        return sum(len(v) for v in self.k.values())
