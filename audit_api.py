import os
import time
import json
import requests
from eth_abi.abi import encode
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

# ==========================================
# CONFIGURATION
# ==========================================
BASE_URL = "https://fapi.asterdex.com" 

API_KEY = os.getenv('ASTER_API_KEY')
API_SECRET = os.getenv('ASTER_API_SECRET')
USER_ADDRESS = os.getenv('ASTER_USER_ADDRESS')
SIGNER_ADDRESS = os.getenv('ASTER_SIGNER_ADDRESS', USER_ADDRESS)

if not API_KEY or not API_SECRET:
    print("Missing API Keys")
    exit(1)

def _trim_dict(data):
    for key in data:
        value = data[key]
        if isinstance(value, list):
            new_value = []
            for item in value:
                if isinstance(item, dict):
                    new_value.append(json.dumps(_trim_dict(item)))
                else:
                    new_value.append(str(item))
            data[key] = json.dumps(new_value)
            continue
        if isinstance(value, dict):
            data[key] = json.dumps(_trim_dict(value))
            continue
        data[key] = str(value)
    return data

def _sign_request(params):
    params = {k: v for k, v in params.items() if v is not None}
    params['recvWindow'] = 50000
    params['timestamp'] = int(time.time() * 1000)
    
    # Exact bot signing logic
    _trim_dict(params)
    nonce = int(time.time() * 1000000)
    json_str = json.dumps(params, sort_keys=True).replace(' ', '').replace("'", '"')
    
    user = USER_ADDRESS
    signer = SIGNER_ADDRESS if SIGNER_ADDRESS else USER_ADDRESS
    
    encoded = encode(['string', 'address', 'address', 'uint256'], 
                     [json_str, user, signer, nonce])
    keccak_hex = Web3.keccak(encoded).hex()
    
    signable_msg = encode_defunct(hexstr=keccak_hex)
    signed_message = Account.sign_message(signable_message=signable_msg, private_key=API_SECRET)
    
    params['nonce'] = str(nonce)
    params['user'] = user
    params['signer'] = signer
    params['signature'] = '0x' + signed_message.signature.hex()
    
    return params

def log_status(endpoint, resp):
    if resp.status_code == 200:
        print(f"[SUCCESS] {endpoint}")
    else:
        print(f"[FAILED] {endpoint} ({resp.status_code}): {resp.text[:200]}")

def test_v3_upgrades():
    print(f"--- Auditing V3 Upgrades on {BASE_URL} ---\n")
    
    headers = {'User-Agent': 'AuditBot/1.0', 'X-MBX-APIKEY': API_KEY}

    # 1. Commission Rate (v3 vs v1)
    params = {'symbol': 'BTCUSDT'}
    q_v3 = _sign_request(params.copy())
    resp = requests.get(f"{BASE_URL}/fapi/v3/commissionRate", params=q_v3, headers=headers)
    log_status("GET /fapi/v3/commissionRate", resp)

    # 2. Position Side Mode (v3 vs v1)
    params = {}
    q_v3 = _sign_request(params.copy())
    resp = requests.get(f"{BASE_URL}/fapi/v3/positionSide/dual", params=q_v3, headers=headers)
    log_status("GET /fapi/v3/positionSide/dual", resp)

    # 3. Double check v1 if v3 fails (sanity check on signer)
    if resp.status_code != 200:
        print("\n--- Verifying Signer with v1/positionSide/dual ---")
        q_v1 = _sign_request(params.copy())
        resp = requests.get(f"{BASE_URL}/fapi/v1/positionSide/dual", params=q_v1, headers=headers)
        log_status("GET /fapi/v1/positionSide/dual", resp)

if __name__ == "__main__":
    test_v3_upgrades()