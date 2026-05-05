"""
server.py — FastAPI WebSocket сервер Дурак
Запуск: python server.py
"""
import json, os, random, uuid
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

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
    def __init__(self, owner, max_p, bet, mode):
        self.id = uuid.uuid4().hex[:8]
        self.owner = owner
        self.max_p = max_p
        self.bet = bet
        self.mode = mode  # 'podkidnoy' | 'perevodoy'
        self.players: List[str] = [owner]
        self.game: Optional['Game'] = None
        self.status = 'waiting'

    def info(self):
        return {'id': self.id, 'owner': self.owner, 'max_p': self.max_p,
                'bet': self.bet, 'mode': self.mode, 'status': self.status,
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
                self.hands[pid].append(self.deck.pop())

    def check_end(self):
        if self.deck: return
        gone = [p for p in self.pids if not self.hands[p]]
        for p in gone:
            idx = self.pids.index(p)
            self.pids.remove(p); self.beaten_out.append(p)
            if self.ai > idx: self.ai -= 1
            if self.di > idx: self.di -= 1
            n = len(self.pids)
            if n: self.ai %= n; self.di %= n
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
        if pid in lb.players: return lb
    return None

async def remove_from_lobby(pid, refund=False):
    lb = player_lobby(pid)
    if not lb or lb.status != 'waiting': return
    lb.players.remove(pid)
    if refund: pdb[pid]['coins'] += lb.bet; _save(pdb)
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
                        'photo_url': raw.get('photo'), 'games': 0, 'wins': 0}
        else:
            if raw.get('name'): pdb[pid]['name'] = raw['name'][:20]
            if raw.get('photo'): pdb[pid]['photo_url'] = raw['photo']
        _save(pdb)
        await ws.send_text(json.dumps({'type': 'init_ok', 'pid': pid,
            'me': {'id': pid, **pdb[pid]},
            'lobbies': [l.info() for l in lobbies.values() if l.status == 'waiting']
        }, ensure_ascii=False))
        while True: await on_msg(pid, json.loads(await ws.receive_text()))
    except WebSocketDisconnect: pass
    finally:
        conns.pop(pid, None)

async def on_msg(pid, d):
    t = d.get('type')

    if t == 'create_lobby':
        bet = max(500, min(5000, (int(d.get('bet', 500)) // 500) * 500))
        max_p = max(2, min(6, int(d.get('max_p', 2))))
        mode = d.get('mode', 'podkidnoy')
        if pdb[pid]['coins'] < bet:
            return await send(pid, {'type': 'err', 'msg': 'Недостаточно монет'})
        await remove_from_lobby(pid)
        lb = Lobby(pid, max_p, bet, mode)
        lobbies[lb.id] = lb
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
        if len(g.pids) < 3:
            return await send(pid, {'type': 'err', 'msg': 'Перевод — только 3+ игроков'})
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

async def end_game(g: Game):
    lb = lobbies.get(g.lobby_id)
    if not lb: return
    pot = lb.bet * len(g.all_pids)
    if g.durak:
        winners = [p for p in g.all_pids if p != g.durak]
        for w in winners:
            pdb[w]['coins'] += pot // len(winners)
            pdb[w].setdefault('games', 0); pdb[w]['games'] += 1
            pdb[w].setdefault('wins', 0);  pdb[w]['wins']  += 1
        pdb[g.durak].setdefault('games', 0); pdb[g.durak]['games'] += 1
    else:
        for p in g.all_pids:
            pdb[p]['coins'] += lb.bet
            pdb[p].setdefault('games', 0); pdb[p]['games'] += 1
    _save(pdb)
    lb.status = 'finished'
    await g.push()

app.mount('/', StaticFiles(directory='frontend', html=True), name='static')

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8000))
    uvicorn.run('server:app', host='0.0.0.0', port=port, reload=False)
