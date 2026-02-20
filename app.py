import os
import re
from typing import Optional, List, Dict, Any
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import psycopg
import redis
import pymysql

load_dotenv()

BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.getenv("BIND_PORT", "8000"))
API_TOKEN = os.environ.get("API_TOKEN", "")

PG_DSN = os.environ.get("PG_DSN", "")
REDIS_URL = os.environ.get("REDIS_URL", "")
REDIS_DB = int(os.getenv("REDIS_DB", "9"))

MDB_HOST = os.environ.get("MDB_HOST", "")
MDB_PORT = int(os.getenv("MDB_PORT", "3306"))
MDB_USER = os.environ.get("MDB_USER", "")
MDB_PASS = os.environ.get("MDB_PASS", "")
MDB_DB = os.environ.get("MDB_DB", "dispatcher-api2")

if not API_TOKEN:
    raise RuntimeError("API_TOKEN is required")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


class FixRequest(BaseModel):
    input: str
    dry_run: bool = False
    enp_target: Literal["NXP1", "NXP2"] = "NXP1"


def require_token(req: Request):
    token = req.headers.get("x-api-token") or ""
    if token != API_TOKEN:
        raise HTTPException(401, "Unauthorized")


def normalize_to_digits(s: str) -> str:
    return re.sub(r"\D+", "", s)


def to_dn_and_target(number: str) -> tuple[str, str]:
    raw = normalize_to_digits(number)

    if len(raw) == 10 and raw.startswith("0"):
        dn = "41" + raw[1:]
        return dn, raw

    if len(raw) == 11 and raw.startswith("41"):
        target = "0" + raw[2:]
        return raw, target

    raise HTTPException(400, f"Unsupported number format: {number}")


def expand_numbers(expr: str, max_span: int = 100) -> List[str]:
    s = re.sub(r"\s+", "", expr)

    if "-" not in s:
        return [s]

    start_s, end_s = s.split("-", 1)
    start_digits = normalize_to_digits(start_s)
    end_digits = normalize_to_digits(end_s)

    if not start_digits or not end_digits:
        raise HTTPException(400, "Bad range format")

    if len(end_digits) < len(start_digits):
        end_full = start_digits[: len(start_digits) - len(end_digits)] + end_digits
    else:
        end_full = end_digits

    start_i = int(start_digits)
    end_i = int(end_full)
    if end_i < start_i:
        raise HTTPException(400, "Range end < start")

    span = end_i - start_i + 1
    if span > max_span:
        raise HTTPException(400, f"Range too large (>{max_span})")

    width = len(start_digits)
    return [str(n).zfill(width) for n in range(start_i, end_i + 1)]


def expand_preview(expr: str) -> Dict[str, Any]:
    targets = expand_numbers(expr, max_span=100)
    dns = []
    ttargets = []
    for t in targets:
        dn, target = to_dn_and_target(t)
        dns.append(dn)
        ttargets.append(target)
    redis_keys = [f"nprn:routing:{dn}" for dn in dns]
    return {
        "count": len(ttargets),
        "expanded_targets": ttargets,
        "expanded_dns": dns,
        "expanded_redis_keys": redis_keys,
    }


def pg_conn():
    if not PG_DSN:
        raise RuntimeError("PG_DSN missing")
    return psycopg.connect(PG_DSN)


def redis_conn():
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL missing")
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def mariadb_conn():
    if not (MDB_HOST and MDB_USER and MDB_DB):
        raise RuntimeError("MariaDB env missing")
    return pymysql.connect(
        host=MDB_HOST,
        port=MDB_PORT,
        user=MDB_USER,
        password=MDB_PASS,
        database=MDB_DB,
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/fix/enp")
def fix_enp(req: Request, body: FixRequest):
    require_token(req)
    prev = expand_preview(body.input)

    ENP_MAP = {
        "NXP1": {"system_id": 500, "nprn": 98067},
        "NXP2": {"system_id": 510, "nprn": 98019},
    }
    cfg = ENP_MAP[body.enp_target]

    if body.dry_run:
        return {"dry_run": True, "enp_target": body.enp_target, **prev, **cfg}

    dns = prev["expanded_dns"]
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE numbers
            SET reservation_tstamp = '2050-01-01 00:00:00',
                product_id = 1,
                system_id = %s,
                nprn = %s,
                outporting_tstamp = NULL,
                lastupdated_tstamp = NOW()
            WHERE dn = ANY(%s)
            RETURNING dn;
            """,
            (cfg["system_id"], cfg["nprn"], dns),
        )
        updated_dns = [r[0] for r in cur.fetchall()]

    return {"dry_run": False, "enp_target": body.enp_target, **prev, **cfg, "updated_dns": updated_dns}


@app.post("/fix/nprn")
def fix_nprn(req: Request, body: FixRequest):
    require_token(req)
    prev = expand_preview(body.input)
    if body.dry_run:
        return {"dry_run": True, **prev, "redis_db": REDIS_DB}

    r = redis_conn()
    r.execute_command("SELECT", REDIS_DB)
    pipe = r.pipeline()
    for k in prev["expanded_redis_keys"]:
        pipe.delete(k)
    deleted_counts = pipe.execute()
    return {"dry_run": False, **prev, "redis_db": REDIS_DB, "deleted_counts": deleted_counts}


@app.post("/fix/disp")
def fix_disp(req: Request, body: FixRequest):
    require_token(req)
    prev = expand_preview(body.input)
    targets = prev["expanded_targets"]

    conn = mariadb_conn()
    try:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(targets))

            cur.execute(
                f"SELECT id, target_number, target_system, tenant, nprn, insert_date "
                f"FROM cli_provisioning WHERE target_number IN ({placeholders})",
                tuple(targets),
            )
            rows = cur.fetchall()

            if body.dry_run:
                return {
                    "dry_run": True,
                    **prev,
                    "would_delete_count": len(rows),
                    "would_delete_rows": rows,
                }

            cur.execute(
                f"DELETE FROM cli_provisioning WHERE target_number IN ({placeholders})",
                tuple(targets),
            )
            conn.commit()
            return {"dry_run": False, **prev, "deleted_count": cur.rowcount, "deleted_rows": rows}
    except:
        conn.rollback()
        raise
    finally:
        conn.close()

