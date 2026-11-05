"""Synthèse pour la tuile « enerSol » : trois réponses simples + les décisions du jour.

  1. Est-ce qu'on tient jusqu'au soleil de demain ?   (batterie au réveil, achat prévu)
  2. Va-t-on acheter au réseau ?                      (combien, quand)
  3. Le confort est-il respecté ?                     (température intérieure)
  + les décisions du jour : faites, en cours, prévues, imprévus.
"""

import base64
import json
from datetime import timedelta


def _txt(s: str) -> str:
    return (s or '').replace(' sur surplus perdu', ' avec le soleil en trop').replace(' au créneau prévu', ' au soleil')


def build(ctrl, planner) -> dict:
    snap = ctrl.last_snapshot
    if snap is None:
        return {}
    now = snap.now
    hours = getattr(planner, 'last_hours', None) or []
    base = getattr(planner, 'last_base', None)
    out = {'heure': now.strftime('%H:%M'), 'jour': ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi',
                                                    'dimanche'][now.weekday()] + now.strftime(' %d/%m')}

    # 1-2) nuit et achat : jusqu'au retour du soleil (demain, ou aujourd'hui si avant le lever)
    if base and hours:
        sun_h = max(7, int(snap.solar_start_h or 9)) + 1
        wake = (now if now.hour < sun_h else now + timedelta(days=1)).replace(hour=sun_h, minute=0, second=0, microsecond=0)
        soc_w, buy, first_buy = None, 0.0, None
        for h, soc, imp in zip(hours, base.soc, base.imports):
            if h.dt > wake:
                break
            soc_w = soc
            if imp > 50:
                buy += imp
                first_buy = first_buy or h.dt
        out['nuit'] = {'ok': buy < 300, 'batterie_reveil': None if soc_w is None else round(soc_w),
                       'reveil': f'{sun_h}h'}
        out['achat'] = {'kwh': round(buy / 1000, 1), 'quand': f'{first_buy.hour}h' if first_buy else None}

    # 3) confort
    tint = None
    if ctrl.pac_home.configured:
        cf = ctrl.pac_home.comfort(snap)
        tint = cf.get('tint')
    if tint is None and ctrl.vmi.configured:
        tint = ctrl.vmi.t('int')
    tmin = ctrl.limits.comfort_min_temp
    out['confort'] = {'tint': None if tint is None else round(tint, 1), 'min': tmin,
                      'ok': tint is None or tint >= tmin - 0.3}

    # décisions du jour
    items = []
    j = ctrl._journal_today()
    active = set()
    if ctrl.vmi.configured and ctrl.vmi.heater_pct:
        active.add('vmi')
    if ctrl.water_paused_for_vmi():
        active.add('chauffe_eau')
    if any(ctrl.calm(k) for k in (getattr(ctrl, '_calm', None) or {})):
        active.add('allers_retours')
    if ctrl.boost_state().get('decalage_c'):
        active.add('surchauffe')
    if getattr(ctrl, 'peak', None) and ctrl.peak.get('logged'):
        active.add('pics')
    for kind, lst in (j.get('decisions') or {}).items():
        if kind in active:
            lst = lst[:-1]                 # la dernière est « en cours », affichée plus bas
        for t in lst:
            h, _, rest = t.partition(' ')
            items.append({'h': h, 'etat': 'imprevu' if kind in ('imprevus', 'allers_retours') else 'fait', 'txt': _txt(rest)})
    # en cours
    unp = getattr(ctrl, '_unp', None) or {}
    if unp.get('active'):
        items.append({'h': unp.get('started', now.strftime('%H:%M')), 'etat': 'en_cours',
                      'txt': f"Imprévu ~{unp.get('w', 0) / 1000:.1f} kW (four, plaques…) : j'ai libéré mes appareils",
                      'sous': ', '.join(unp.get('shed') or []) or 'rien ne tournait'})
    pk = getattr(ctrl, 'peak', None)
    if pk:
        items.append({'h': pk['started'], 'etat': 'en_cours',
                      'txt': f"{pk['who']} chauffe l'eau : je fais de la place",
                      'sous': ', '.join(x for x in [pk.get('pac')] + (pk.get('shed') or []) if x) or 'rien à décaler'})
    b = ctrl.boost_state()
    if b.get('decalage_c'):
        items.append({'h': now.strftime('%H:%M'), 'etat': 'en_cours',
                      'txt': f"Je chauffe la maison +{b['decalage_c']:.1f} °C avec le soleil en trop",
                      'sous': 'chaleur gardée pour la soirée'})
    if ctrl.vmi.configured and ctrl.vmi.heater_pct:
        items.append({'h': now.strftime('%H:%M'), 'etat': 'en_cours',
                      'txt': 'VMI : air chauffé avec le soleil en trop', 'sous': f'résistance {ctrl.vmi.power():.0f} W'})
    for key in (getattr(ctrl, '_calm', None) or {}):
        d = ctrl.devices.get(key)
        if d is not None and ctrl.calm(key):
            items.append({'h': now.strftime('%H:%M'), 'etat': 'imprevu',
                          'txt': f'{d.name} : trop d\'allers-retours, je ralentis ses réglages',
                          'sous': 'pendant 30 min ; c\'est noté dans le journal pour corriger'})
    if ctrl.water_paused_for_vmi():
        items.append({'h': now.strftime('%H:%M'), 'etat': 'en_cours',
                      'txt': 'Chauffe-eau en pause : le soleil chauffe la maison par la VMI',
                      'sous': "mes compteurs montrent que c'est plus rentable maintenant ; l'eau chauffera plus tard au soleil"})
    if ctrl.ev.configured and ctrl.ev.is_running():
        items.append({'h': now.strftime('%H:%M'), 'etat': 'en_cours',
                      'txt': 'Voiture en charge ' + ('avec le soleil' if ctrl.ev.started_by_us and not ctrl.ev.batt_blocked
                                                     else 'sur le réseau (batterie maison protégée)')})
    for d in (ctrl.washer, ctrl.dryer):
        if d.configured and d.is_running() and not (pk and pk.get('who') == d.name):
            items.append({'h': now.strftime('%H:%M'), 'etat': 'en_cours', 'txt': f'{d.name} en cours'})
    # prévues
    plan = planner.plan or {}
    for key, w in (plan.get('windows') or {}).items():
        if key in ('washer', 'dryer') and w.get('decision') == 'later_today' and w.get('start_at'):
            name = 'Lave-linge' if key == 'washer' else 'Sèche-linge'
            items.append({'h': w['start_at'], 'etat': 'prevu', 'txt': f"{name} lancé au soleil ({w.get('program')})"})
        elif key in ('washer', 'dryer') and w.get('decision') == 'tomorrow':
            name = 'Lave-linge' if key == 'washer' else 'Sèche-linge'
            items.append({'h': 'demain', 'etat': 'prevu', 'txt': f"{name} : {w.get('verdict')}"})
    wh = ctrl.water
    if wh.configured and wh.enabled and wh.is_thermo():
        stop = str(wh.cfg.get('night_stop', '21:00') or '21:00')
        if now.strftime('%H:%M') < stop:
            items.append({'h': stop, 'etat': 'prevu', 'txt': "Chauffe-eau en pause jusqu'au soleil"})
    offs = plan.get('pac_home_offsets') or {}
    neg = sorted(int(h) for h, v in offs.items() if float(v) < 0 and int(h) >= now.hour)
    if neg and ctrl.pac_home.configured:
        items.append({'h': f'{neg[0]:02d}:00', 'etat': 'prevu',
                      'txt': f'Nuit un peu plus fraîche, jamais sous {tmin:.1f} °C'.replace('.', ',')})

    def key(it):
        h = it['h']
        return (1, '') if h == 'demain' else (0, h)
    items.sort(key=key)
    out['decisions'] = items[-8:]
    return out


def encoded(ctrl, planner) -> str:
    """JSON en base64 : passe sans souci dans le gabarit HTML de Jeedom."""
    return base64.b64encode(json.dumps(build(ctrl, planner), ensure_ascii=False).encode()).decode()
