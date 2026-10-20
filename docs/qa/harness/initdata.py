# initData для синтетического бэкенда (фейковый токен из seed.py)
import hashlib, hmac, json, time
from urllib.parse import urlencode
FAKE_TOKEN = "123456:QA_FAKE_TOKEN_xyz"
USER = {"id": 777000777, "first_name": "QA", "username": "qa_operator"}

def make() -> str:
    f = {"user": json.dumps(USER, separators=(",", ":")), "auth_date": str(int(time.time())), "query_id": "QA"}
    dc = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    sk = hmac.new(b"WebAppData", FAKE_TOKEN.encode(), hashlib.sha256).digest()
    f["hash"] = hmac.new(sk, dc.encode(), hashlib.sha256).hexdigest()
    return urlencode(f)

if __name__ == "__main__":
    print(make())
