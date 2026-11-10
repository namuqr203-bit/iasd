"""Slotana PvP Flip Loop

Two bot wallets (A and B). Press Start and every 12 hours it runs a cycle:
  5 rounds of:  A creates a PvP flip  ->  B joins it  ->  payout  ->  auto split
Auto split sends half the difference from the richer wallet to the poorer one
so both wallets have the same balance again before the next round.

Local:    python app.py               (open http://localhost:8080)
Railway:  see Procfile / README.md    (ONE worker only)
"""

import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, send_from_directory

from solana_tx import Keypair, build_transfer_tx, sign_serialized_tx


# ------------------------------------------------------------------ config
def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def env_bool(name, default):
    return str(env(name, str(default))).lower() in ("1", "true", "yes", "on")


LAMPORTS = 1_000_000_000
SLOTANA_API_URL = env("SLOTANA_API_URL", "https://backend.slotana.io").rstrip("/")
CLUSTER = env("CLUSTER", "mainnet")
RPC_URL = env("RPC_URL", "https://api.mainnet-beta.solana.com")

BET_SOL = float(env("BET_SOL", "0.005"))            # Slotana minimum is 0.005
ROUNDS_PER_CYCLE = int(env("ROUNDS_PER_CYCLE", "5"))
CYCLE_HOURS = float(env("CYCLE_HOURS", "12"))
COOLDOWN_SEC = int(env("COOLDOWN_SEC", "20"))       # pause between steps
SPLIT_MIN_LAMPORTS = int(env("SPLIT_MIN_LAMPORTS", "20000"))  # skip tiny splits

ADMIN_PASSWORD = env("ADMIN_PASSWORD", "")          # optional
DRY_RUN = env_bool("DRY_RUN", False)                # simulate, no real SOL
# On Railway, attach a Volume: its mount path is picked up automatically so the
# wallets and history survive redeploys.
VOLUME = env("RAILWAY_VOLUME_MOUNT_PATH")
DB_PATH = env("DB_PATH", os.path.join(VOLUME, "flipbot.db") if VOLUME else "data/flipbot.db")
NO_VOLUME_ON_RAILWAY = bool(env("RAILWAY_ENVIRONMENT")) and not VOLUME and not env("DB_PATH")

TX_FEE = 5000              # lamports per signature
RENT_MIN = 890_880         # a wallet must keep this (or exactly 0) after sending
BET = int(round(BET_SOL * LAMPORTS))


# ------------------------------------------------------------------ storage
_db_lock = threading.RLock()


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql, args=(), one=False):
    with _db_lock, db() as c:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        return (dict(rows[0]) if rows else None) if one else [dict(r) for r in rows]


def x(sql, args=()):
    with _db_lock, db() as c:
        return c.execute(sql, args).lastrowid


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS wallets (label TEXT PRIMARY KEY, address TEXT, secret TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
            status TEXT, rounds_done INTEGER DEFAULT 0, note TEXT,
            start_total INTEGER, end_total INTEGER);
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, round_no INTEGER,
            ts TEXT, game_id INTEGER, bet INTEGER, face TEXT, winner TEXT,
            create_sig TEXT, join_sig TEXT, split_from TEXT, split_amount INTEGER,
            split_sig TEXT, bal_a INTEGER, bal_b INTEGER, status TEXT, note TEXT);
        CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT);
        """)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message, level="info"):
    print(f"[{level.upper()}] {message}", flush=True)
    x("INSERT INTO log (ts, level, message) VALUES (?,?,?)", (now_iso(), level, message))
    x("DELETE FROM log WHERE id NOT IN (SELECT id FROM log ORDER BY id DESC LIMIT 500)")


def get_setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else default


def set_setting(key, value):
    x("INSERT INTO settings (key,value) VALUES (?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, None if value is None else str(value)))


# ------------------------------------------------------------------ wallets
WALLETS = {}


def load_wallets():
    """Creates wallets A and B on first start and keeps them forever.
    WALLET_A_KEY / WALLET_B_KEY env vars override (to restore old wallets)."""
    for label in ("A", "B"):
        override = env(f"WALLET_{label}_KEY")
        if override:
            kp = Keypair.from_secret(override)
        else:
            row = q("SELECT secret FROM wallets WHERE label=?", (label,), one=True)
            kp = Keypair.from_secret(row["secret"]) if row else Keypair(os.urandom(32))
        x("INSERT INTO wallets (label,address,secret,created_at) VALUES (?,?,?,?) "
          "ON CONFLICT(label) DO UPDATE SET address=excluded.address, secret=excluded.secret",
          (label, kp.address, kp.secret_b58, now_iso()))
        WALLETS[label] = kp


# ------------------------------------------------------------------ RPC
def rpc(method, params):
    r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                                     "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        err = body["error"]
        detail = ""
        data = err.get("data") or {}
        if isinstance(data, dict):
            if data.get("err"):
                detail += f" | err: {data['err']}"
            logs = [l for l in (data.get("logs") or []) if l]
            if logs:
                detail += " | " + " / ".join(logs[-3:])
        raise RuntimeError(f"RPC {method}: {err.get('message')}{detail}")
    return body["result"]


def balance(label, commitment="confirmed"):
    if DRY_RUN:
        return int(get_setting(f"sim_{label}", str(int(0.01 * LAMPORTS))))
    return rpc("getBalance", [WALLETS[label].address, {"commitment": commitment}])["value"]


def balances(commitment="confirmed"):
    return balance("A", commitment), balance("B", commitment)


def wait_confirmed(sig, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        st = rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])["value"][0]
        if st:
            if st.get("err"):
                raise RuntimeError(f"Transaction failed on chain: {st['err']}")
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return
        time.sleep(2)
    raise RuntimeError(f"Transaction not confirmed in {timeout}s: {sig}")


def transfer(from_label, to_label, lamports):
    if DRY_RUN:
        set_setting(f"sim_{from_label}", balance(from_label) - lamports - TX_FEE)
        set_setting(f"sim_{to_label}", balance(to_label) + lamports)
        return "simulated"
    bh = rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    tx = build_transfer_tx(WALLETS[from_label], WALLETS[to_label].address, lamports, bh)
    sig = rpc("sendTransaction", [tx, {"encoding": "base64",
                                       "preflightCommitment": "finalized"}])
    wait_confirmed(sig)
    return sig


def rebalance(tag):
    """Auto split: make both wallets equal. Uses finalized balances (so the
    payout is definitely visible to every RPC node) and retries, because a
    payout that only just landed can make the first attempt fail simulation."""
    last_err = None
    if not DRY_RUN:  # wait until finalized balances caught up with confirmed ones
        end = time.time() + 60
        while time.time() < end and balances("finalized") != balances("confirmed"):
            time.sleep(4)
    for attempt in range(1, 7):
        a, b = balances("finalized")
        diff = abs(a - b)
        if diff < SPLIT_MIN_LAMPORTS:
            return None, None, None
        src, dst = ("A", "B") if a > b else ("B", "A")
        amount = (diff - TX_FEE) // 2
        try:
            step(f"{tag}: auto split, {src} sends {amount / LAMPORTS:.6f} SOL to {dst}"
                 + (f" (attempt {attempt})" if attempt > 1 else ""))
            return src, amount, transfer(src, dst, amount)
        except Exception as e:
            last_err = e
            log(f"{tag}: auto split attempt {attempt} failed: {e}", "warn")
            time.sleep(10 if not DRY_RUN else 0)
    raise RuntimeError(f"Auto split failed 6 times: {last_err}")


# ------------------------------------------------------------------ Slotana
class SlotanaError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Slotana HTTP {status}: {body}")
        self.status, self.body = status, body


def slotana(path, payload):
    url = f"{SLOTANA_API_URL}/{CLUSTER}/transaction/{path}"
    r = requests.post(url, json=payload, timeout=120)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:300]
    if r.status_code != 200 or (isinstance(body, dict) and body.get("success") is False):
        raise SlotanaError(r.status_code, body)
    return body


def build_and_sign_deposit(label):
    built = slotana("build/pvpFlipDeposit", {
        "betAmount": BET, "mint": [0, 0], "playerPublicKey": WALLETS[label].address})
    return sign_serialized_tx(built["serializedTransaction"], WALLETS[label])


def create_game(label, is_heads):
    if DRY_RUN:
        set_setting(f"sim_{label}", balance(label) - BET)
        gid = int(get_setting("sim_game_id", "5000")) + 1
        set_setting("sim_game_id", gid)
        return {"id": gid, "player1Signature": None}
    signed = build_and_sign_deposit(label)
    return slotana("createPvPFlip", {"isHeads": is_heads, "serializedTx": signed})["game"]


def join_game(label, game_id):
    if DRY_RUN:
        set_setting(f"sim_{label}", balance(label) - BET)
        winner = random.choice(["A", "B"])
        payout = int(BET * 2 * 0.97)
        set_setting(f"sim_{winner}", balance(winner) + payout)
        return {"id": game_id, "winner": WALLETS[winner].address, "player2Signature": None}
    signed = build_and_sign_deposit(label)
    return slotana("pvpFlip", {"gameId": game_id, "serializedTx": signed})["game"]


def cancel_game(label, game_id):
    if DRY_RUN:
        set_setting(f"sim_{label}", balance(label) + BET)
        return
    built = slotana("build/cancelPvPFlip", {"gameId": game_id,
                                            "playerPublicKey": WALLETS[label].address})
    signed = sign_serialized_tx(built["serializedTransaction"], WALLETS[label])
    slotana("cancelPvPFlip", {"gameId": game_id, "serializedTx": signed})


# ------------------------------------------------------------------ one round
STATUS = {"state": "idle", "round": 0, "step": ""}


def step(text):
    STATUS["step"] = text
    log(text)


def run_round(cycle_id, n):
    rid = x("INSERT INTO rounds (cycle_id, round_no, ts, bet, status) VALUES (?,?,?,?,?)",
            (cycle_id, n, now_iso(), BET, "RUNNING"))

    def upd(**kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        x(f"UPDATE rounds SET {cols} WHERE id=?", (*kw.values(), rid))

    # 1. even out the wallets first (fixes a split that failed last time),
    #    then check both have enough for a game
    rebalance(f"Round {n}")
    a, b = balances()
    need = BET + RENT_MIN + TX_FEE * 2
    if a < need or b < need:
        msg = (f"Not enough SOL: A {a / LAMPORTS:.4f}, B {b / LAMPORTS:.4f} "
               f"(each needs {need / LAMPORTS:.4f}). Fund the wallets.")
        upd(status="SKIPPED", note=msg, bal_a=a, bal_b=b)
        raise StopCycle(msg)

    # 2. A creates
    is_heads = random.random() < 0.5
    face = "heads" if is_heads else "tails"
    step(f"Round {n}: wallet A creating game ({BET_SOL} SOL, {face})")
    game = create_game("A", is_heads)
    gid = game["id"]
    upd(game_id=gid, face=face, create_sig=game.get("player1Signature"))
    step(f"Round {n}: game #{gid} created, waiting {COOLDOWN_SEC}s")
    time.sleep(COOLDOWN_SEC if not DRY_RUN else 1)

    # 3. B joins
    a_before, b_before = balances()
    step(f"Round {n}: wallet B joining game #{gid}")
    try:
        res = join_game("B", gid)
    except Exception as e:
        log(f"Round {n}: join failed ({e}); cancelling game #{gid} to refund A", "error")
        try:
            cancel_game("A", gid)
            upd(status="CANCELLED", note=f"Join failed, game refunded: {str(e)[:200]}")
        except Exception as e2:
            upd(status="ERROR", note=f"Join failed and cancel failed: {str(e2)[:200]}")
        raise
    upd(join_sig=res.get("player2Signature"))

    # 4. find the winner (from response, else from balance change)
    winner = None
    w = res.get("winner")
    for label, kp in WALLETS.items():
        if w == kp.address:
            winner = label
    end = time.time() + 90
    while True:
        a_after, b_after = balances()
        gain_a = a_after - a_before
        gain_b = b_after - (b_before - BET)
        if gain_a > BET or gain_b > BET:
            winner = winner or ("A" if gain_a > gain_b else "B")
            break
        if time.time() > end:
            break
        time.sleep(3)
    upd(winner=winner, status="PLAYED")
    step(f"Round {n}: game #{gid} won by wallet {winner or '?'}")
    time.sleep(COOLDOWN_SEC if not DRY_RUN else 1)

    # 5. auto split
    src, amount, sig = rebalance(f"Round {n}")
    if src:
        upd(split_from=src, split_amount=amount, split_sig=sig)
    a, b = balances()
    upd(bal_a=a, bal_b=b, status="DONE")
    step(f"Round {n}: done. A {a / LAMPORTS:.4f} SOL, B {b / LAMPORTS:.4f} SOL")


class StopCycle(Exception):
    pass


_cycle_lock = threading.Lock()


def run_cycle():
    if not _cycle_lock.acquire(blocking=False):
        return
    a, b = balances()
    cid = x("INSERT INTO cycles (started_at, status, start_total) VALUES (?,?,?)",
            (now_iso(), "RUNNING", a + b))
    STATUS.update(state="running", round=0)
    log(f"Cycle {cid} started ({ROUNDS_PER_CYCLE} rounds)")
    status, note, done = "DONE", None, 0
    try:
        for n in range(1, ROUNDS_PER_CYCLE + 1):
            if get_setting("running") != "1":
                status, note = "STOPPED", "Stopped from dashboard"
                break
            STATUS["round"] = n
            run_round(cid, n)
            done = n
            x("UPDATE cycles SET rounds_done=? WHERE id=?", (done, cid))
            if n < ROUNDS_PER_CYCLE:
                time.sleep(COOLDOWN_SEC if not DRY_RUN else 1)
    except StopCycle as e:
        status, note = "NEEDS FUNDING", str(e)
        log(str(e), "warn")
    except Exception as e:
        status, note = "ERROR", str(e)[:400]
        log(f"Cycle {cid} stopped by error: {e}", "error")
    finally:
        try:
            a, b = balances()
            end_total = a + b
        except Exception:
            end_total = None
        x("UPDATE cycles SET finished_at=?, status=?, note=?, rounds_done=?, end_total=? "
          "WHERE id=?", (now_iso(), status, note, done, end_total, cid))
        STATUS.update(state="idle", round=0, step="")
        log(f"Cycle {cid} finished: {status} ({done}/{ROUNDS_PER_CYCLE} rounds)")
        _cycle_lock.release()


# ------------------------------------------------------------------ scheduler
def next_cycle_at():
    if get_setting("running") != "1":
        return None
    anchor = float(get_setting("anchor"))
    period = CYCLE_HOURS * 3600
    last = float(get_setting("last_cycle_at", "0"))
    k = max(0, int((last - anchor) // period) + 1) if last >= anchor else 0
    return anchor + k * period


def scheduler_loop():
    while True:
        try:
            nxt = next_cycle_at()
            if nxt is not None and time.time() >= nxt and not _cycle_lock.locked():
                set_setting("last_cycle_at", time.time())
                threading.Thread(target=run_cycle, daemon=True).start()
        except Exception as e:
            log(f"Scheduler error: {e}", "error")
        time.sleep(10)


# ------------------------------------------------------------------ web
app = Flask(__name__, static_folder="static")


def authorized():
    return not ADMIN_PASSWORD or request.headers.get("X-Password") == ADMIN_PASSWORD


@app.get("/")
def index():
    with open(os.path.join(app.static_folder, "index.html"), encoding="utf-8") as f:
        return "<!doctype html>\n<html lang=\"en\"><head>" + f.read() + "</html>", 200, \
            {"Content-Type": "text/html; charset=utf-8"}


@app.get("/api/state")
def api_state():
    try:
        a, b = balances()
    except Exception:
        a = b = None
    cycles = q("SELECT * FROM cycles ORDER BY id DESC LIMIT 60")
    rounds = q("SELECT * FROM rounds WHERE cycle_id IN "
               "(SELECT id FROM cycles ORDER BY id DESC LIMIT 60) ORDER BY id DESC")
    for c in cycles:
        c["rounds"] = [r for r in rounds if r["cycle_id"] == c["id"]][::-1]
    return jsonify({
        "wallets": [{"label": "A", "role": "Creates games", "address": WALLETS["A"].address, "lamports": a},
                    {"label": "B", "role": "Joins games", "address": WALLETS["B"].address, "lamports": b}],
        "running": get_setting("running") == "1",
        "busy": _cycle_lock.locked(),
        "status": STATUS,
        "next_cycle_at": next_cycle_at(),
        "dry_run": DRY_RUN,
        "password_required": bool(ADMIN_PASSWORD),
        "no_volume": NO_VOLUME_ON_RAILWAY,
        "settings": {"bet_sol": BET_SOL, "rounds": ROUNDS_PER_CYCLE, "cycle_hours": CYCLE_HOURS,
                     "cooldown_sec": COOLDOWN_SEC, "cluster": CLUSTER,
                     "min_per_wallet_sol": (BET + RENT_MIN + TX_FEE * 2) / LAMPORTS},
        "cycles": cycles,
        "log": q("SELECT ts, level, message FROM log ORDER BY id DESC LIMIT 60"),
    })


@app.post("/api/start")
def api_start():
    if not authorized():
        return jsonify({"error": "Wrong password"}), 401
    if get_setting("running") == "1":
        return jsonify({"ok": True})
    set_setting("running", "1")
    set_setting("anchor", time.time())       # cycles repeat every 12h from this moment
    set_setting("last_cycle_at", "0")
    log(f"Started from dashboard: {ROUNDS_PER_CYCLE} rounds now, then every {CYCLE_HOURS:g}h")
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    if not authorized():
        return jsonify({"error": "Wrong password"}), 401
    set_setting("running", "0")
    log("Stopped from dashboard (current round finishes first)")
    return jsonify({"ok": True})


@app.post("/api/export")
def api_export():
    if not authorized():
        return jsonify({"error": "Wrong password"}), 401
    label = (request.json or {}).get("label")
    if label not in WALLETS:
        return jsonify({"error": "Unknown wallet"}), 400
    log(f"Private key of wallet {label} was exported")
    return jsonify({"label": label, "address": WALLETS[label].address,
                    "secret": WALLETS[label].secret_b58})


@app.get("/health")
def health():
    return "ok"


# ------------------------------------------------------------------ start
def start():
    init_db()
    load_wallets()
    mode = "DRY RUN (simulated, no real SOL)" if DRY_RUN else "LIVE"
    log(f"Server up in {mode} mode. Wallet A {WALLETS['A'].address}, "
        f"wallet B {WALLETS['B'].address}")
    # a cycle interrupted by a restart is marked so the history stays honest
    x("UPDATE cycles SET status='INTERRUPTED', finished_at=? WHERE status='RUNNING'",
      (now_iso(),))
    threading.Thread(target=scheduler_loop, daemon=True).start()


start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "8080")), debug=False, threaded=True)
