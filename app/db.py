import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", "/data/powerball.sqlite"))

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS draws (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  draw_no INTEGER UNIQUE NOT NULL,
  draw_date TEXT NOT NULL,
  n1 INTEGER NOT NULL,
  n2 INTEGER NOT NULL,
  n3 INTEGER NOT NULL,
  n4 INTEGER NOT NULL,
  n5 INTEGER NOT NULL,
  n6 INTEGER NOT NULL,
  n7 INTEGER NOT NULL,
  powerball INTEGER NOT NULL,
  source_url TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_draws_date ON draws(draw_date);
CREATE INDEX IF NOT EXISTS idx_draws_no   ON draws(draw_no);
"""

@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()

def upsert_draw(draw):
    with connect() as conn:
        conn.execute("""
        INSERT INTO draws (draw_no, draw_date, n1,n2,n3,n4,n5,n6,n7, powerball, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(draw_no) DO UPDATE SET
          draw_date=excluded.draw_date,
          n1=excluded.n1, n2=excluded.n2, n3=excluded.n3, n4=excluded.n4,
          n5=excluded.n5, n6=excluded.n6, n7=excluded.n7,
          powerball=excluded.powerball,
          source_url=excluded.source_url
        """, (
            draw["draw_no"], draw["draw_date"],
            draw["nums"][0], draw["nums"][1], draw["nums"][2],
            draw["nums"][3], draw["nums"][4], draw["nums"][5], draw["nums"][6],
            draw["pb"], draw["source_url"]
        ))
        conn.commit()

def get_draws(limit=None):
    sql = "SELECT draw_no, draw_date, n1,n2,n3,n4,n5,n6,n7, powerball FROM draws ORDER BY date(draw_date) DESC, draw_no DESC"
    params = (limit,) if limit else ()
    if limit:
        sql += " LIMIT ?"
    with connect() as conn:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
    to_dict = lambda r: {"draw_no": r[0], "draw_date": r[1], "nums": [r[2],r[3],r[4],r[5],r[6],r[7],r[8]], "powerball": r[9]}
    return [to_dict(r) for r in rows]

def get_frequencies(window=None):
    with connect() as conn:
        if window:
            ids = [d["draw_no"] for d in get_draws(limit=window)]
            where = f"WHERE draw_no IN ({','.join('?'*len(ids))})"
            params = ids
        else:
            where, params = "", []

        main = {i:0 for i in range(1,36)}
        for row in conn.execute(f"SELECT n1,n2,n3,n4,n5,n6,n7 FROM draws {where}", params):
            for n in row:
                if 1 <= n <= 35:
                    main[n] += 1

        pb = {i:0 for i in range(1,21)}
        for (p,) in conn.execute(f"SELECT powerball FROM draws {where}", params):
            if 1 <= p <= 20:
                pb[p] += 1

        (sample_size,) = conn.execute(f"SELECT COUNT(*) FROM draws {where}", params).fetchone()

    return {"main": main, "powerball": pb, "sample_size": sample_size}
