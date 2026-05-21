"""
cryptopay.py — CryptoBot Crypto Pay API integration
Testnet: @CryptoTestnetBot, https://testnet-pay.crypt.bot/api
Mainnet: @CryptoBot,        https://pay.crypt.bot/api
"""
import os
import hashlib
import hmac as _hmac
import httpx

CRYPTOPAY_TOKEN   = os.getenv('CRYPTOPAY_TOKEN', '')
CRYPTOPAY_TESTNET = os.getenv('CRYPTOPAY_TESTNET', '1') == '1'
BASE_URL = (
    'https://testnet-pay.crypt.bot/api'
    if CRYPTOPAY_TESTNET else
    'https://pay.crypt.bot/api'
)

COMMISSION_TG_ID  = int(os.getenv('COMMISSION_TG_ID', '0'))
COMMISSION_RATE   = 0.10


async def create_invoice(amount: float, payload: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f'{BASE_URL}/createInvoice',
            headers={'Crypto-Pay-API-Token': CRYPTOPAY_TOKEN},
            json={
                'asset': 'USDT',
                'amount': str(round(amount, 2)),
                'description': 'Ставка в Дурак 🃏',
                'payload': payload,
                'allow_comments': False,
                'allow_anonymous': False,
                'expires_in': 600,
            },
        )
    data = r.json()
    if not data.get('ok'):
        raise RuntimeError(f'createInvoice error: {data}')
    return data['result']


async def transfer(tg_id: int, amount: float, spend_id: str, comment: str = '') -> dict:
    body = {
        'user_id': tg_id,
        'asset': 'USDT',
        'amount': str(round(amount, 2)),
        'spend_id': spend_id,
        'disable_send_notification': False,
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f'{BASE_URL}/transfer',
            headers={'Crypto-Pay-API-Token': CRYPTOPAY_TOKEN},
            json=body,
        )
    data = r.json()
    if not data.get('ok'):
        raise RuntimeError(f'transfer error: {data}')
    return data['result']


def verify_webhook(body: bytes, signature: str) -> bool:
    if not CRYPTOPAY_TOKEN:
        return True  # dev/local mode
    secret = hashlib.sha256(CRYPTOPAY_TOKEN.encode()).digest()
    expected = _hmac.new(secret, body, hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, signature)
