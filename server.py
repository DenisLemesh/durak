"""
server.py — FastAPI WebSocket сервер Дурак
Запуск: python server.py
"""
import json, os, random, string, uuid
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from database import upsert_user, increment_stats, update_coins, get_all_users

app = FastAPI()

SUITS = ['♠', '♥', '♦', '♣']
RANKS = ['6', '7', '8', '9', '10', 'В', 'Д', 'К', 'Т']
RV = {r: i for i, r in enumerate(RANKS)}
INITIAL_COINS = 1000
DATA_FILE = 'data/players.json'

os.makedirs('data', exist_ok=True)

def _load():
    try:
        with open(DATA_FILE, encoding='utf-8') as f: return json.load(f)
    except FileNotFoundError: return {}

def _save(d):
    with open(DATA_FILE, 'w', encoding='utf-8') as f: json.dump(d, f, ensure_ascii=False)

pdb: Dict[str, dict] = _load()
conns: Dict[str, WebSocket] = {}
lobbies: Dict[str, 'Lobby'] = {}

# ── утилиты ───────────────────────────────────────────────────────────────
def can_beat(a, d, tr):
    at, dt = a['s'] == tr, d['s'] == tr
    if dt and not at: return True
    if dt and at:     return RV[d['r']] > RV[a['r']]
    if at and not dt: return False
    return d['s'] == a['s'] and RV[d['r']] > RV[a['r']]

def make_deck():
    deck = [{'r': r, 's': s, 'id': r+s} for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

def find_card(hand, cid): return next((c for c in hand if c['id'] == cid), None)

def get_friends_view(pid: str) -> list:
    friends = pdb.get(pid, {}).get('friends', [])
    return [{'id': f, 'name': pdb.get(f, {}).get('name', '?'),
             'photo': pdb.get(f, {}).get('photo_url'), 'online': f in conns}
            for f in friends if f in pdb]

def gen_game_id() -> str:
    chars = string.ascii_uppercase + string.digits
    existing = {pdb[p].get('game_id') for p in pdb}
    while True:
        gid = ''.join(random.choices(chars, k=8))
        if gid not in existing:
            return gid

def find_pid_by_short(short_id: str) -> Optional[str]:
    short = short_id.upper()
    for p in pdb:
        if pdb[p].get('game_id', '').upper() == short:
            return p
    return None

async def send(pid, msg):
    ws = conns.get(pid)
    if ws:
        try: await ws.send_text(json.dumps(msg, ensure_ascii=False))
        except: pass

async def broadcast_lobby_list():
    msg = json.dumps({'type': 'lobby_list',
        'lobbies': [l.info() for l in lobbies.values() if l.status == 'waiting']
    }, ensure_ascii=False)
    for ws in list(conns.values()):
        try: await ws.send_text(msg)
        except: pass

# ── Лобби ─────────────────────────────────────────────────────────────────
class Lobby:
    def __init__(self, owner, max_p, bet, mode, currency='coins'):
        self.id = uuid.uuid4().hex[:8]
        self.owner = owner
        self.max_p = max_p
        self.bet = bet
        self.mode = mode  # 'podkidnoy' | 'perevodoy'
        self.currency = currency  # 'coins' | 'usdt'
        self.players: List[str] = [owner]
        self.game: Optional['Game'] = None
        self.status = 'waiting'

    def info(self):
        return {'id': self.id, 'owner': self.owner, 'max_p': self.max_p,
                'bet': self.bet, 'mode': self.mode, 'currency': self.currency, 'status': self.status,
                'players': [{'id': p, 'name': pdb.get(p, {}).get('name', '?'),
                              'photo': pdb.get(p, {}).get('photo_url')} for p in self.players]}

    async def notify(self, msg):
        for pid in self.players: await send(pid, msg)

# ── Игра ──────────────────────────────────────────────────────────────────
class Game:
    def __init__(self, lobby: Lobby):
        n = len(lobby.players)
        deck = make_deck()
        self.lobby_id = lobby.id
        self.trump_card = deck[-1]
        self.trump = self.trump_card['s']
        self.hands: Dict[str, list] = {p: deck[i*6:(i+1)*6] for i, p in enumerate(lobby.players)}
        self.deck: list = deck[n*6:]
        self.all_pids: List[str] = lobby.players[:]
        self.pids: List[str] = lobby.players[:]
        self.ai = 0       # attacker index
        self.di = 1 % n   # defender index
        self.mode = lobby.mode
        self.state = 'attack'   # 'attack' | 'defense' | 'finished'
        self.table: list = []   # [{atk, def}]
        self.beaten_out: List[str] = []
        self.durak: Optional[str] = None

    @property
    def atk(self): return self.pids[self.ai]
    @property
    def dfr(self): return self.pids[self.di]

    def refill(self):
        for pid in self.pids[self.ai:] + self.pids[:self.ai]:
            while len(self.hands[pid]) < 6 and self.deck:
                self.hands[pid].append(self.deck.pop(0))

    def check_end(self):
        if self.deck: return
        gone = [p for p in self.pids if not self.hands[p]]
        for p in gone:
            idx = self.pids.index(p)
            self.pids.remove(p); self.beaten_out.append(p)
            if self.ai > idx: self.ai -= 1
            if self.di > idx: self.di -= 1
            n = len(self.pids)
            if n:
                self.ai %= n; self.di %= n
                if n > 1 and self.ai == self.di:
                    self.di = (self.di + 1) % n
        if len(self.pids) <= 1:
            self.durak = self.pids[0] if self.pids else None
            self.state = 'finished'

    def next_round(self, took: bool):
        n = len(self.pids)
        new_ai = (self.di + 1) % n if took else self.di
        self.ai = new_ai
        self.di = (new_ai + 1) % n
        self.table = []
        self.state = 'attack'

    def view(self, for_pid):
        return {
            'trump': self.trump, 'trump_card': self.trump_card,
            'deck_count': len(self.deck), 'table': self.table,
            'atk': self.atk if self.pids else None,
            'dfr': self.dfr if len(self.pids) > 1 else None,
            'state': self.state, 'mode': self.mode,
            'beaten_out': self.beaten_out, 'durak': self.durak,
            'lobby_id': self.lobby_id,
            'players': [{'id': p, 'name': pdb.get(p, {}).get('name', '?'),
                          'photo': pdb.get(p, {}).get('photo_url'),
                          'coins': pdb.get(p, {}).get('coins', 0),
                          'n': len(self.hands.get(p, [])),
                          'hand': self.hands.get(p, []) if p == for_pid else None}
                         for p in (self.pids + self.beaten_out)]
        }

    async def push(self):
        for pid in self.all_pids:
            ws = conns.get(pid)
            if ws:
                try: await ws.send_text(json.dumps({'type': 'game', 'g': self.view(pid)}, ensure_ascii=False))
                except: pass

# ── WebSocket ──────────────────────────────────────────────────────────────
def player_lobby(pid) -> Optional[Lobby]:
    for lb in lobbies.values():
        if pid in lb.players and lb.status != 'finished': return lb
    return None

async def remove_from_lobby(pid, refund=False):
    lb = player_lobby(pid)
    if not lb or lb.status != 'waiting': return
    lb.players.remove(pid)
    if refund and lb.currency == 'coins': pdb[pid]['coins'] += lb.bet; _save(pdb)
    if not lb.players: del lobbies[lb.id]
    else:
        if lb.owner == pid: lb.owner = lb.players[0]
        await lb.notify({'type': 'lobby_update', 'lobby': lb.info()})

@app.websocket('/ws')
async def ws_ep(ws: WebSocket):
    await ws.accept()
    pid = None
    try:
        raw = json.loads(await ws.receive_text())
        assert raw.get('type') == 'init'
        pid = raw.get('pid') or uuid.uuid4().hex
        conns[pid] = ws
        if pid not in pdb:
            pdb[pid] = {'name': raw.get('name', 'Игрок')[:20], 'coins': INITIAL_COINS,
                        'photo_url': raw.get('photo'), 'games': 0, 'wins': 0,
                        'game_id': gen_game_id()}
        else:
            if 'game_id' not in pdb[pid]:
                pdb[pid]['game_id'] = gen_game_id()
            if raw.get('name'): pdb[pid]['name'] = raw['name'][:20]
            if raw.get('photo'): pdb[pid]['photo_url'] = raw['photo']
        _save(pdb)
        if pid.startswith('tg_'):
            try:
                tg_id = int(pid[3:])
                upsert_user(tg_id=tg_id, first_name=raw.get('name'))
                update_coins(tg_id, pdb[pid].get('coins', INITIAL_COINS))
            except ValueError:
                pass
        await ws.send_text(json.dumps({'type': 'init_ok', 'pid': pid,
            'me': {'id': pid, **pdb[pid]},
            'friends': get_friends_view(pid),
            'lobbies': [l.info() for l in lobbies.values() if l.status == 'waiting']
        }, ensure_ascii=False))
        while True: await on_msg(pid, json.loads(await ws.receive_text()))
    except WebSocketDisconnect: pass
    finally:
        conns.pop(pid, None)

async def on_msg(pid, d):
    t = d.get('type')

    if t == 'create_lobby':
        currency = d.get('currency', 'coins')
        max_p = max(2, min(6, int(d.get('max_p', 2))))
        mode = d.get('mode', 'podkidnoy')
        if currency == 'usdt':
            try:
                bet = float(d.get('bet', 0.5))
            except (ValueError, TypeError):
                bet = 0.5
            valid = [0.5, 1, 2, 5, 10]
            if bet not in valid:
                bet = min(valid, key=lambda x: abs(x - bet))
        else:
            currency = 'coins'
            bet = max(500, min(5000, (int(d.get('bet', 500)) // 500) * 500))
            if pdb[pid]['coins'] < bet:
                return await send(pid, {'type': 'err', 'msg': 'Недостаточно монет'})
        await remove_from_lobby(pid)
        lb = Lobby(pid, max_p, bet, mode, currency)
        lobbies[lb.id] = lb
        if currency == 'coins':
            pdb[pid]['coins'] -= bet; _save(pdb)
        await send(pid, {'type': 'lobby_joined', 'lobby': lb.info(), 'me': {'id': pid, **pdb[pid]}})
        await broadcast_lobby_list()

    elif t == 'join_lobby':
        lb = lobbies.get(d.get('lid'))
        if not lb or lb.status != 'waiting':
            return await send(pid, {'type': 'err', 'msg': 'Лобби недоступно'})
        if pid in lb.players: return
        if len(lb.players) >= lb.max_p:
            return await send(pid, {'type': 'err', 'msg': 'Лобби полное'})
        if pdb[pid]['coins'] < lb.bet:
            return await send(pid, {'type': 'err', 'msg': 'Недостаточно монет'})
        await remove_from_lobby(pid)
        lb.players.append(pid)
        if lb.currency == 'coins':
            pdb[pid]['coins'] -= lb.bet; _save(pdb)
        await send(pid, {'type': 'lobby_joined', 'lobby': lb.info(), 'me': {'id': pid, **pdb[pid]}})
        await lb.notify({'type': 'lobby_update', 'lobby': lb.info()})
        await broadcast_lobby_list()

    elif t == 'leave_lobby':
        await remove_from_lobby(pid, refund=True)
        await send(pid, {'type': 'lobby_left', 'me': {'id': pid, **pdb[pid]}})
        await broadcast_lobby_list()

    elif t == 'start':
        lb = player_lobby(pid)
        if not lb or lb.owner != pid or len(lb.players) < 2:
            return await send(pid, {'type': 'err', 'msg': 'Нельзя начать'})
        lb.status = 'playing'
        lb.game = Game(lb)
        await lb.game.push()
        await broadcast_lobby_list()

    elif t == 'act':
        lb = player_lobby(pid)
        if lb and lb.game: await do_act(pid, lb.game, d)

    elif t == 'update_name':
        name = str(d.get('name', ''))[:20].strip()
        if name:
            pdb[pid]['name'] = name
            _save(pdb)

    elif t == 'add_friend':
        short_id = str(d.get('short_id', '')).strip().upper()
        target = find_pid_by_short(short_id)
        if not target:
            return await send(pid, {'type': 'err', 'msg': 'Игрок не найден'})
        if target == pid:
            return await send(pid, {'type': 'err', 'msg': 'Нельзя добавить себя'})
        pdb[pid].setdefault('friends', [])
        pdb[target].setdefault('friends', [])
        if target not in pdb[pid]['friends']:
            pdb[pid]['friends'].append(target)
        if pid not in pdb[target]['friends']:
            pdb[target]['friends'].append(pid)
        _save(pdb)
        await send(pid, {'type': 'friends_update', 'friends': get_friends_view(pid)})
        await send(target, {'type': 'friends_update', 'friends': get_friends_view(target)})

    elif t == 'remove_friend':
        target = str(d.get('pid', '')).strip()
        if target in pdb:
            pdb[pid].setdefault('friends', [])
            pdb[target].setdefault('friends', [])
            if target in pdb[pid]['friends']:
                pdb[pid]['friends'].remove(target)
            if pid in pdb[target]['friends']:
                pdb[target]['friends'].remove(pid)
            _save(pdb)
        await send(pid, {'type': 'friends_update', 'friends': get_friends_view(pid)})

    elif t == 'chat':
        text = str(d.get('msg', ''))[:200].strip()
        if not text: return
        name = pdb.get(pid, {}).get('name', 'Игрок')
        msg = json.dumps({'type': 'chat_msg', 'pid': pid, 'name': name, 'text': text}, ensure_ascii=False)
        for ws2 in list(conns.values()):
            try: await ws2.send_text(msg)
            except: pass

async def do_act(pid, g: Game, d):
    if g.state == 'finished': return
    act = d.get('act')

    if act == 'atk':
        if pid == g.dfr: return
        # Allow throwing in 'defense' state too (подкидной — другие игроки добавляют)
        if g.state not in ('attack', 'defense'): return
        if g.state == 'defense' and g.mode != 'podkidnoy': return
        card = find_card(g.hands[pid], d.get('cid'))
        if not card: return
        if g.table:
            ranks = {p['atk']['r'] for p in g.table} | {p['def']['r'] for p in g.table if p.get('def')}
            if card['r'] not in ranks:
                return await send(pid, {'type': 'err', 'msg': 'Ранг не совпадает'})
            # FIX: limit = defender's cards at round start = current hand + already defended
            defended = sum(1 for p in g.table if p.get('def'))
            def_had = len(g.hands[g.dfr]) + defended
            if len(g.table) >= min(6, def_had):
                return await send(pid, {'type': 'err', 'msg': 'У защитника мало карт'})
        elif g.state != 'attack':
            return
        g.table.append({'atk': card, 'def': None})
        g.hands[pid].remove(card)
        g.state = 'defense'
        await g.push()

    elif act == 'atk_all':
        if pid == g.dfr: return
        if g.state not in ('attack', 'defense'): return
        if g.state == 'defense' and g.mode != 'podkidnoy': return
        rank = d.get('rank')
        if not rank or rank not in {c['r'] for c in g.hands.get(pid, [])}: return
        if g.table:
            table_ranks = {p['atk']['r'] for p in g.table} | {p['def']['r'] for p in g.table if p.get('def')}
            if rank not in table_ranks:
                return await send(pid, {'type': 'err', 'msg': 'Ранг не совпадает'})
        elif g.state != 'attack':
            return
        defended = sum(1 for p in g.table if p.get('def'))
        def_had = len(g.hands[g.dfr]) + defended
        max_add = min(6, def_had) - len(g.table)
        if max_add <= 0:
            return await send(pid, {'type': 'err', 'msg': 'У защитника мало карт'})
        cards = [c for c in g.hands[pid] if c['r'] == rank][:max_add]
        for card in cards:
            g.table.append({'atk': card, 'def': None})
            g.hands[pid].remove(card)
        g.state = 'defense'
        await g.push()

    elif act == 'def':
        if pid != g.dfr or g.state != 'defense': return
        pi = d.get('pi', -1)
        if pi < 0 or pi >= len(g.table): return
        pair = g.table[pi]
        if pair.get('def'): return
        card = find_card(g.hands[pid], d.get('cid'))
        if not card: return
        if not can_beat(pair['atk'], card, g.trump):
            return await send(pid, {'type': 'err', 'msg': 'Карта не бьёт'})
        pair['def'] = card
        g.hands[pid].remove(card)
        if all(p.get('def') for p in g.table):
            g.state = 'attack'  # все отбиты — атакующий может подкинуть или завершить
        await g.push()

    elif act == 'transfer':
        if g.mode != 'perevodoy' or pid != g.dfr or g.state != 'defense': return
        if any(p.get('def') for p in g.table): return
        card = find_card(g.hands[pid], d.get('cid'))
        if not card: return
        if card['r'] != g.table[0]['atk']['r']:
            return await send(pid, {'type': 'err', 'msg': 'Ранг не совпадает'})
        next_di = (g.di + 1) % len(g.pids)
        if len(g.hands[g.pids[next_di]]) < len(g.table) + 1:
            return await send(pid, {'type': 'err', 'msg': 'У следующего мало карт'})
        g.table.append({'atk': card, 'def': None})
        g.hands[pid].remove(card)
        g.ai = g.di; g.di = next_di
        await g.push()

    elif act == 'take':
        if pid != g.dfr or g.state != 'defense': return
        for p in g.table:
            g.hands[pid].append(p['atk'])
            if p.get('def'): g.hands[pid].append(p['def'])
        g.next_round(took=True)
        g.refill(); g.check_end()
        await g.push()
        if g.state == 'finished': await end_game(g)

    elif act == 'done':
        if pid == g.dfr: return
        if not g.table or g.state != 'attack': return
        g.next_round(took=False)
        g.refill(); g.check_end()
        await g.push()
        if g.state == 'finished': await end_game(g)

    elif act == 'surrender':
        if pid not in g.pids: return
        g.durak = pid
        g.state = 'finished'
        await end_game(g)

async def end_game(g: Game):
    lb = lobbies.get(g.lobby_id)
    if not lb: return
    is_usdt = lb.currency == 'usdt'
    pot = lb.bet * len(g.all_pids)
    if g.durak:
        winners = [p for p in g.all_pids if p != g.durak]
        for w in winners:
            if not is_usdt:
                pdb[w]['coins'] += int(pot // len(winners))
            pdb[w].setdefault('games', 0); pdb[w]['games'] += 1
            pdb[w].setdefault('wins', 0);  pdb[w]['wins']  += 1
            if w.startswith('tg_'):
                try:
                    tg_id = int(w[3:])
                    increment_stats(tg_id, won=True)
                    if not is_usdt: update_coins(tg_id, pdb[w]['coins'])
                except ValueError: pass
        pdb[g.durak].setdefault('games', 0); pdb[g.durak]['games'] += 1
        if g.durak.startswith('tg_'):
            try:
                tg_id = int(g.durak[3:])
                increment_stats(tg_id, won=False)
                if not is_usdt: update_coins(tg_id, pdb[g.durak]['coins'])
            except ValueError: pass
    else:
        for p in g.all_pids:
            if not is_usdt:
                pdb[p]['coins'] += lb.bet
            pdb[p].setdefault('games', 0); pdb[p]['games'] += 1
            if p.startswith('tg_'):
                try:
                    tg_id = int(p[3:])
                    increment_stats(tg_id, won=False)
                    if not is_usdt: update_coins(tg_id, pdb[p]['coins'])
                except ValueError: pass
    _save(pdb)
    lb.status = 'finished'
    await g.push()
    lobbies.pop(g.lobby_id, None)

ADMIN_KEY = os.getenv('ADMIN_KEY', '')

@app.get('/admin/users', response_class=HTMLResponse)
async def admin_users(key: str = Query(default='')):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail='Forbidden')
    users = get_all_users()
    online_pids = set(conns.keys())
    online_count = sum(1 for u in users if f'tg_{u["tg_id"]}' in online_pids)

    def row(u):
        is_online = f'tg_{u["tg_id"]}' in online_pids
        dot = '<span style="color:#4caf50;font-size:16px" title="Онлайн">●</span>' if is_online else '<span style="color:#555;font-size:16px" title="Оффлайн">●</span>'
        bg = 'background:#0d2010;' if is_online else ''
        name = f'{u["first_name"] or "—"} {u["last_name"] or ""}'.strip()
        game_id = pdb.get(f'tg_{u["tg_id"]}', {}).get('game_id', '—')
        return (
            f'<tr style="{bg}">'
            f'<td>{dot}</td>'
            f'<td style="font-family:monospace;color:#f0b429;font-weight:700">{game_id}</td>'
            f'<td>{u["tg_id"]}</td>'
            f'<td>{u["username"] or "—"}</td>'
            f'<td>{name}</td>'
            f'<td>{u["games"]}</td>'
            f'<td>{u["wins"]}</td>'
            f'<td style="color:#f0b429;font-weight:600">{u.get("coins", 1000)}</td>'
            f'<td>{u["joined_at"][:16].replace("T"," ")}</td>'
            f'<td>{u["last_seen"][:16].replace("T"," ")}</td>'
            f'</tr>'
        )

    rows = ''.join(row(u) for u in users)
    html = f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="30">
<title>Пользователи — Дурак</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0d0d0d;color:#e0e0e0;padding:24px}}
  h2{{font-size:20px;margin-bottom:6px}}
  .meta{{font-size:12px;color:#666;margin-bottom:20px}}
  .badges{{display:flex;gap:16px;margin-bottom:20px}}
  .badge{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:10px 20px;text-align:center}}
  .badge-val{{font-size:26px;font-weight:700;color:#f0b429}}
  .badge-lbl{{font-size:11px;color:#777;margin-top:2px}}
  .badge.online .badge-val{{color:#4caf50}}
  table{{border-collapse:collapse;width:100%;font-size:13px}}
  th{{background:#181818;color:#999;font-weight:600;padding:10px 12px;text-align:left;border-bottom:1px solid #222;position:sticky;top:0}}
  td{{padding:9px 12px;border-bottom:1px solid #1a1a1a;vertical-align:middle}}
  tr:hover td{{background:#141414}}
  .tag{{background:#1e2d1e;color:#4caf50;border-radius:4px;padding:1px 6px;font-size:11px;font-weight:600}}
</style></head>
<body>
<h2>♠ Дурак — Панель администратора</h2>
<div class="meta">Обновляется каждые 30 сек · {len(users)} пользователей в базе</div>
<div class="badges">
  <div class="badge online"><div class="badge-val">{online_count}</div><div class="badge-lbl">Онлайн сейчас</div></div>
  <div class="badge"><div class="badge-val">{len(users)}</div><div class="badge-lbl">Всего игроков</div></div>
</div>
<table>
  <tr><th>●</th><th>Game ID</th><th>TG ID</th><th>Username</th><th>Имя</th><th>Игр</th><th>Побед</th><th>Монеты</th><th>Зашёл</th><th>Был</th></tr>
  {rows}
</table>
</body></html>'''
    return HTMLResponse(html)

app.mount('/', StaticFiles(directory='frontend', html=True), name='static')

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8000))
    uvicorn.run('server:app', host='0.0.0.0', port=port, reload=False)
