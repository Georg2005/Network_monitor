"""Local network monitoring dashboard (educational project)."""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from network import probe, discover, validate_network

from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("NETWORK_MONITOR_DB", ROOT / "monitor.db"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))
MAX_HOSTS = 30
HOST_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?$")

app = Flask(__name__)
app.json.ensure_ascii = False
scan_lock = threading.Lock()
discovery_lock = threading.Lock()


def connection():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connection() as con:
        con.execute("CREATE TABLE IF NOT EXISTS hosts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL UNIQUE)")
        con.execute("""CREATE TABLE IF NOT EXISTS checks (
          id INTEGER PRIMARY KEY, host_id INTEGER NOT NULL,
          checked_at TEXT NOT NULL, online INTEGER NOT NULL, latency_ms REAL,
          FOREIGN KEY(host_id) REFERENCES hosts(id))""")
        columns = {r[1] for r in con.execute("PRAGMA table_info(checks)")}
        for column, kind in [("resolved_ip", "TEXT"), ("rtt_less", "INTEGER DEFAULT 0"), ("is_rtt", "INTEGER DEFAULT 0")]:
            if column not in columns:
                con.execute(f"ALTER TABLE checks ADD COLUMN {column} {kind}")
        con.execute("CREATE INDEX IF NOT EXISTS idx_checks_host ON checks(host_id, id)")
        con.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, host_id INTEGER NOT NULL,
            name TEXT NOT NULL, address TEXT NOT NULL, resolved_ip TEXT,
            occurred_at TEXT NOT NULL, online INTEGER NOT NULL)""")


def normalize_host(raw):
    if not isinstance(raw, str):
        raise ValueError("Адреса має бути текстом")
    addr = raw.strip().lower()
    if not addr or len(addr) > 253 or not HOST_RE.fullmatch(addr) or ".." in addr:
        raise ValueError("Вкажіть IPv4-адресу або домен без протоколу та порту")
    # Reject shell flags even though subprocess never invokes a shell.
    if addr.startswith("-"):
        raise ValueError("Некоректна адреса")
    return addr


def check_all():
    if not scan_lock.acquire(blocking=False):
        return False
    try:
        with connection() as con:
            hosts = con.execute("SELECT id, address FROM hosts ORDER BY id").fetchall()
        for host in hosts:
            ip, online, ms, less = probe(host["address"])
            with connection() as con:
                con.execute("BEGIN IMMEDIATE")
                current = con.execute("SELECT name, address FROM hosts WHERE id=?", (host["id"],)).fetchone()
                if current is None or current["address"] != host["address"]:
                    continue
                previous = con.execute("SELECT online FROM checks WHERE host_id=? ORDER BY id DESC LIMIT 1", (host["id"],)).fetchone()
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                con.execute("INSERT INTO checks(host_id, checked_at, online, latency_ms, resolved_ip, rtt_less, is_rtt) VALUES (?, ?, ?, ?, ?, ?, 1)",
                            (host["id"], now, int(online), ms, ip, int(less)))
                if previous is not None and previous["online"] != int(online):
                    con.execute("INSERT INTO events(host_id, name, address, resolved_ip, occurred_at, online) VALUES (?, ?, ?, ?, ?, ?)",
                                (host["id"], current["name"], current["address"], ip, now, int(online)))
        return True
    finally:
        scan_lock.release()


def monitor_loop():
    while True:
        check_all()
        time.sleep(max(5, CHECK_INTERVAL))


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/hosts")
def hosts():
    with connection() as con:
        rows = con.execute("""SELECT h.id, h.name, h.address,
          (SELECT online FROM checks WHERE host_id=h.id ORDER BY id DESC LIMIT 1) AS online,
          (SELECT CASE WHEN is_rtt=1 THEN latency_ms END FROM checks WHERE host_id=h.id ORDER BY id DESC LIMIT 1) AS latency_ms,
          (SELECT resolved_ip FROM checks WHERE host_id=h.id ORDER BY id DESC LIMIT 1) AS resolved_ip,
          (SELECT rtt_less FROM checks WHERE host_id=h.id ORDER BY id DESC LIMIT 1) AS rtt_less,
          (SELECT checked_at FROM checks WHERE host_id=h.id ORDER BY id DESC LIMIT 1) AS checked_at,
          (SELECT COUNT(*) FROM checks WHERE host_id=h.id) AS total,
          (SELECT COUNT(*) FROM checks WHERE host_id=h.id AND online=1) AS successful
          FROM hosts h ORDER BY h.id""").fetchall()
    return jsonify([{**dict(row), "availability": round(100 * row["successful"] / row["total"], 1) if row["total"] else None} for row in rows])


@app.post("/api/hosts")
def add_host():
    payload = request.get_json(silent=True) or {}
    try:
        address = normalize_host(payload.get("address"))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    name = str(payload.get("name", "")).strip()[:60] or address
    with connection() as con:
        if con.execute("SELECT COUNT(*) FROM hosts").fetchone()[0] >= MAX_HOSTS:
            return jsonify(error=f"Ліміт: {MAX_HOSTS} вузлів"), 400
        try:
            con.execute("INSERT INTO hosts(name, address) VALUES (?, ?)", (name, address))
        except sqlite3.IntegrityError:
            return jsonify(error="Цю адресу вже додано"), 409
    return jsonify(ok=True), 201


@app.delete("/api/hosts/<int:host_id>")
def delete_host(host_id):
    with connection() as con:
        con.execute("DELETE FROM checks WHERE host_id=?", (host_id,))
        result = con.execute("DELETE FROM hosts WHERE id=?", (host_id,))
    return ("", 204) if result.rowcount else (jsonify(error="Вузол не знайдено"), 404)


@app.post("/api/check")
def manual_check():
    if not check_all():
        return jsonify(error="Перевірка вже виконується"), 409
    return jsonify(ok=True)


@app.get("/api/history/<int:host_id>")
def history(host_id):
    with connection() as con:
        rows = con.execute("SELECT checked_at, online, CASE WHEN is_rtt=1 THEN latency_ms END AS latency_ms, rtt_less FROM checks WHERE host_id=? ORDER BY id DESC LIMIT 30", (host_id,)).fetchall()
    return jsonify([dict(row) for row in reversed(rows)])


@app.get("/api/events")
def events():
    with connection() as con:
        rows = con.execute("SELECT id, name, address, resolved_ip, occurred_at, online FROM events ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/discover")
def discover_lan():
    payload = request.get_json(silent=True) or {}
    try:
        net = validate_network(payload.get("network", ""))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    if not discovery_lock.acquire(blocking=False):
        return jsonify(error="Сканування вже триває"), 409
    try:
        return jsonify(network=str(net), hosts=discover(str(net)))
    finally:
        discovery_lock.release()


if __name__ == "__main__":
    init_db()
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
