"""Client HTTP vers Jeedom (core/php/jeeEnerSol.php) — requêtes groupées.

Une seule requête HTTP lit
toutes les valeurs d'un cycle, une seule exécute toutes les actions.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Callable, Optional

import aiohttp

log = logging.getLogger('enerSol.jeedom')


class JeedomClient:

    def __init__(self, url: str, apikey: str, timeout: float = 15.0):
        self._url = url
        self._apikey = apikey
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _post(self, payload: dict, timeout: Optional[float] = None) -> Optional[dict]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        payload = {**payload, 'apikey': self._apikey}
        try:
            kw = {'timeout': aiohttp.ClientTimeout(total=timeout)} if timeout else {}
            async with self._session.post(self._url, json=payload, **kw) as r:
                if r.status != 200:
                    log.warning('Jeedom %s → HTTP %s', payload.get('action'), r.status)
                    return None
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning('Jeedom %s → %s', payload.get('action'), e)
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def ping(self) -> bool:
        r = await self._post({'action': 'ping'})
        return bool(r and r.get('ok'))

    async def read(self, ids) -> dict:
        ids = sorted({int(i) for i in ids if str(i).strip().isdigit() and int(i) > 0})
        if not ids:
            return {}
        r = await self._post({'action': 'read', 'cmds': ids})
        if not r:
            return {}
        return {int(k): v for k, v in (r.get('values') or {}).items()}

    async def exec(self, items: list) -> dict:
        if not items:
            return {}
        r = await self._post({'action': 'exec', 'cmds': items})
        if not r:
            return {int(i['id']): False for i in items}
        return {int(k): bool(v) for k, v in (r.get('results') or {}).items()}

    async def history_weighted(self, ids, from_days: int, to_days: int, split: Optional[dict] = None) -> dict:
        """Moyennes horaires pondérées par le temps sur [from_days ; to_days] jours en arrière."""
        ids = [int(i) for i in ids if str(i).strip().isdigit() and int(i) > 0]
        if not ids:
            return {}
        r = await self._post({'action': 'history_weighted', 'cmds': ids, 'from_days': from_days,
                              'to_days': to_days, 'split': split or {}}, timeout=300)
        if not r:
            return {}
        # clés : « 123 » (moyenne), « 123:maison » (part qui chauffe toute la maison)
        return {(k if ':' in str(k) else int(k)): v for k, v in (r.get('history') or {}).items()}

    async def history(self, ids, days: int = 30) -> dict:
        ids = [int(i) for i in ids if str(i).strip().isdigit() and int(i) > 0]
        if not ids:
            return {}
        r = await self._post({'action': 'history', 'cmds': ids, 'days': days})
        if not r:
            return {}
        return {int(k): v for k, v in (r.get('history') or {}).items()}

    async def event(self, values: dict) -> bool:
        r = await self._post({'action': 'event', 'values': values})
        return bool(r and r.get('ok'))

    async def notify(self, title: str, message: str) -> bool:
        r = await self._post({'action': 'notify', 'title': title, 'message': message})
        return bool(r and r.get('ok'))


class Bus:
    """Accès aux valeurs du cycle courant + file d'actions à exécuter.

    Les appareils lisent via val()/num()/flag() et écrivent via exec().
    Les actions sont envoyées en une requête à la fin du cycle (flush).
    """

    def __init__(self):
        self.values: dict = {}
        self._queue: list = []
        self._callbacks: list = []
        self.sent: deque = deque(maxlen=5000)      # (instant, id) des ordres envoyés : surveillance des allers-retours

    def set_values(self, values: dict):
        self.values = values

    def val(self, cmd_id, default=None):
        if not cmd_id:
            return default
        try:
            v = self.values.get(int(cmd_id))
        except (TypeError, ValueError):
            return default
        return default if v is None or v == '' else v

    def num(self, cmd_id, default: Optional[float] = 0.0) -> Optional[float]:
        v = self.val(cmd_id, None)
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def text(self, cmd_id, default: str = '') -> str:
        v = self.val(cmd_id, None)
        return default if v is None else str(v).strip().lower()

    def flag(self, cmd_id) -> bool:
        return (self.num(cmd_id, 0.0) or 0.0) > 0

    def exec(self, cmd_id, slider=None, options: Optional[dict] = None,
             on_result: Optional[Callable[[bool], None]] = None) -> bool:
        if not cmd_id or not str(cmd_id).strip().isdigit():
            return False
        opts = dict(options or {})
        if slider is not None:
            opts['slider'] = slider
        self._queue.append({'id': int(cmd_id), 'options': opts})
        self.sent.append((time.monotonic(), int(cmd_id)))
        self._callbacks.append((int(cmd_id), on_result))
        return True

    async def flush(self, client: JeedomClient):
        if not self._queue:
            return
        queue, callbacks = self._queue, self._callbacks
        self._queue, self._callbacks = [], []
        results = await client.exec(queue)
        for cmd_id, cb in callbacks:
            ok = results.get(cmd_id, False)
            if not ok:
                log.warning('Commande %s : échec d\'exécution', cmd_id)
            if cb:
                try:
                    cb(ok)
                except Exception:  # noqa: BLE001
                    log.exception('callback exec')
