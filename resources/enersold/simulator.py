"""Simulation du SOC heure par heure."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

JOURS_COURTS = ['lun', 'mar', 'mer', 'jeu', 'ven', 'sam', 'dim']


def fr(dt: Optional[datetime]) -> Optional[str]:
    """'mar 08h' — indépendant de la locale système."""
    return f"{JOURS_COURTS[dt.weekday()]} {dt.hour:02d}h" if dt else None


@dataclass
class Hour:
    dt: datetime
    pv_wh: float
    load_wh: float
    extra_wh: float = 0.0        # charges planifiées en plus de la conso de base


@dataclass
class SimResult:
    soc: list = field(default_factory=list)       # SOC en fin de chaque heure
    imports: list = field(default_factory=list)
    exports: list = field(default_factory=list)
    import_wh: float = 0.0
    export_wh: float = 0.0
    soc_min: float = 100.0
    soc_min_at: Optional[datetime] = None
    soc_full_at: Optional[datetime] = None
    floor_hit_at: Optional[datetime] = None

    def summary(self, hours: list, n: Optional[int] = None) -> dict:
        n = len(hours) if n is None else min(n, len(hours))
        return {
            'soc_min': round(self.soc_min, 0),
            'soc_min_at': fr(self.soc_min_at),
            'soc_full_at': fr(self.soc_full_at),
            'floor_hit_at': fr(self.floor_hit_at),
            'import_kwh': round(self.import_wh / 1000, 2),
            'export_kwh': round(self.export_wh / 1000, 2),
            'soc_by_hour': {h.dt.strftime('%d %Hh'): round(s) for h, s in list(zip(hours, self.soc))[:n:2]},
        }


def simulate(start_soc: float, hours: list, capacity_wh: float, soc_min: float,
             charge_max_w: float, discharge_max_w: float, efficiency: float) -> SimResult:
    eff = max(0.5, min(1.0, efficiency)) ** 0.5     # rendement de charge = de décharge
    res = SimResult()
    soc = float(start_soc)
    cap = max(1.0, capacity_wh)
    for h in hours:
        e = soc / 100.0 * cap
        net = h.pv_wh - h.load_wh - h.extra_wh
        imp = exp = 0.0
        if net >= 0:
            room = cap - e
            charge = min(net, charge_max_w, room / eff)
            e += charge * eff
            exp = net - charge
        else:
            need = -net
            avail = max(0.0, e - soc_min / 100.0 * cap)
            dis = min(need, discharge_max_w, avail * eff)
            e -= dis / eff
            imp = need - dis
        soc = max(0.0, min(100.0, e / cap * 100.0))
        res.soc.append(soc)
        res.imports.append(imp)
        res.exports.append(exp)
        res.import_wh += imp
        res.export_wh += exp
        if soc < res.soc_min:
            res.soc_min, res.soc_min_at = soc, h.dt
        if res.soc_full_at is None and soc >= 99.0:
            res.soc_full_at = h.dt
        if res.floor_hit_at is None and soc <= soc_min + 0.5 and imp > 50:
            res.floor_hit_at = h.dt
    return res


def simulate_steps(start_soc: float, pv: list, load: list, extra: list, step_h: float,
                   capacity_wh: float, soc_min: float, charge_max_w: float,
                   discharge_max_w: float, efficiency: float, lost_until: Optional[int] = None) -> tuple:
    """Version rapide à pas fixe (ex. 5 min).
    Renvoie (énergie achetée au réseau, surplus solaire perdu avant l'indice `lost_until`) en Wh,
    et si la batterie atteint ~100 % avant cet indice.
    Surplus perdu = production qui ne peut ni alimenter la maison ni entrer dans la batterie
    (batterie pleine ou charge max atteinte) : exportée ou bridée."""
    eff = max(0.5, min(1.0, efficiency)) ** 0.5
    cap = max(1.0, capacity_wh)
    e = start_soc / 100.0 * cap
    e_min = soc_min / 100.0 * cap
    cmax, dmax = charge_max_w * step_h, discharge_max_w * step_h
    imp = lost = 0.0
    full = e >= 0.98 * cap
    end = len(pv) if lost_until is None else min(len(pv), lost_until)
    for i in range(len(pv)):
        net = pv[i] - load[i] - extra[i]
        if net >= 0:
            stored = min(net, cmax, (cap - e) / eff)
            e += stored * eff
            if i < end:
                lost += net - stored
                if e >= 0.98 * cap:
                    full = True
        else:
            dis = min(-net, dmax, max(0.0, e - e_min) * eff)
            e -= dis / eff
            imp += -net - dis
    return imp, lost, full
