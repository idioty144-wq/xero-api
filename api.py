from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
import sqlite3, secrets, string
import uvicorn

# ============================================================
#  CONFIG
# ============================================================
API_HOST = "0.0.0.0"
API_PORT  = 8000
API_BASE  = "https://api-xero.onrender.com"  # เปลี่ยนเป็น URL จริงของ Railway

DB_PATH   = "xero.db"  # Railway ใช้ path นี้

# ============================================================
#  DATABASE
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL,
            name TEXT NOT NULL,
            code TEXT NOT NULL,
            api_token TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS pending_keys (
            key TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_code TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    db.commit()
    db.close()

# ============================================================
#  HELPERS
# ============================================================
def gen_token(length=48):
    return secrets.token_urlsafe(length)

# ============================================================
#  FASTAPI ROUTES
# ============================================================
app = FastAPI()

@app.get("/")
def root():
    # หน้าเปล่า — ไม่เปิดเผยอะไรเลย
    return {}

@app.get("/script/{token}", response_class=PlainTextResponse)
def get_script(token: str):
    """endpoint ที่ loadstring เรียก — ส่งโค้ด Lua กลับ"""
    db = get_db()
    row = db.execute(
        "SELECT code FROM scripts WHERE api_token=?", (token,)
    ).fetchone()
    db.close()
    if not row:
        raise HTTPException(status_code=404, detail="")
    return row["code"]

@app.get("/getkey/{key}")
def verify_key(key: str):
    """Linksite redirect มาที่นี่หลังผ่านโฆษณา"""
    db = get_db()
    row = db.execute(
        "SELECT * FROM pending_keys WHERE key=? AND used=0", (key,)
    ).fetchone()
    if not row:
        db.close()
        return {"status": "invalid", "message": "Key ไม่ถูกต้องหรือใช้ไปแล้ว"}

    token = gen_token()
    db.execute(
        "INSERT INTO scripts (owner_id, name, code, api_token) VALUES (?,?,?,?)",
        (row["user_id"], row["file_name"], row["file_code"], token)
    )
    db.execute("UPDATE pending_keys SET used=1 WHERE key=?", (key,))
    db.commit()
    db.close()

    loadstring_url = f"{API_BASE}/script/{token}"
    return {
        "status": "ok",
        "loadstring": f"loadstring(game:HttpGet('{loadstring_url}'))()"
    }

@app.post("/internal/lock")
def lock_script(data: dict):
    """บอท Discord เรียก endpoint นี้เพื่อล็อคโค้ดโดยตรง (admin bypass)"""
    secret = data.get("secret")
    if secret != INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    owner_id = data.get("owner_id")
    name     = data.get("name")
    code     = data.get("code")

    if not all([owner_id, name, code]):
        raise HTTPException(status_code=400, detail="Missing fields")

    token = gen_token()
    db = get_db()
    db.execute(
        "INSERT INTO scripts (owner_id, name, code, api_token) VALUES (?,?,?,?)",
        (str(owner_id), name, code, token)
    )
    db.commit()
    db.close()

    return {
        "status": "ok",
        "token": token,
        "loadstring": f"loadstring(game:HttpGet('{API_BASE}/script/{token}'))()"
    }

@app.get("/internal/keycheck/{key}")
def check_key_status(key: str, secret: str = ""):
    """บอทเช็คว่า key ถูก redeem แล้วหรือยัง"""
    if secret != INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    db = get_db()
    row = db.execute("SELECT used FROM pending_keys WHERE key=?", (key,)).fetchone()
    db.close()
    if not row:
        return {"used": -1, "message": "ไม่พบ key"}
    return {"used": row["used"]}

@app.post("/internal/addkey")
def add_pending_key(data: dict):
    """บอทเพิ่ม pending key ลง DB ของ API"""
    secret = data.get("secret")
    if secret != INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    key       = data.get("key")
    user_id   = data.get("user_id")
    file_name = data.get("file_name")
    file_code = data.get("file_code")

    db = get_db()
    db.execute(
        "INSERT INTO pending_keys (key, user_id, file_name, file_code) VALUES (?,?,?,?)",
        (key, str(user_id), file_name, file_code)
    )
    db.commit()
    db.close()
    return {"status": "ok"}

@app.get("/internal/scripts/{owner_id}")
def list_scripts(owner_id: str, secret: str = ""):
    """บอทดึงรายชื่อ script ของ user"""
    if secret != INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    db = get_db()
    rows = db.execute(
        "SELECT id, name, api_token, created_at FROM scripts WHERE owner_id=? ORDER BY id ASC",
        (owner_id,)
    ).fetchall()
    db.close()
    return {"scripts": [dict(r) for r in rows]}

@app.delete("/internal/scripts/{script_id}")
def delete_script(script_id: int, data: dict):
    """บอทลบ script"""
    if data.get("secret") != INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    db = get_db()
    db.execute("DELETE FROM scripts WHERE id=?", (script_id,))
    db.commit()
    db.close()
    return {"status": "deleted"}

# ============================================================
#  INTERNAL SECRET (บอทใช้คุยกับ API)
# ============================================================
INTERNAL_SECRET = "xero999"  # เปลี่ยนเป็นรหัสลับยาวๆ

# ============================================================
#  KEEP ALIVE (ป้องกัน Render sleep)
# ============================================================
import asyncio
import aiohttp

async def keep_alive():
    """ping ตัวเองทุก 10 นาที ป้องกัน Render sleep"""
    await asyncio.sleep(60)  # รอให้ server ขึ้นก่อน
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(f"{API_BASE}/")
        except Exception:
            pass
        await asyncio.sleep(600)  # 10 นาที

@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(keep_alive())

# ============================================================
#  RUN
# ============================================================
if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)
