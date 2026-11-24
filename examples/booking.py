"""Planted split commit. Set SPLIT_COMMIT to False for the atomic control."""
from contextlib import closing
import sqlite3

from crashcheck import CheckFailure

SPLIT_COMMIT = True


def setup(ctx):
    with closing(sqlite3.connect(ctx.db)) as db:
        db.executescript("""
            CREATE TABLE seats (remaining INTEGER NOT NULL);
            INSERT INTO seats VALUES (1);
            CREATE TABLE bookings (operation INTEGER PRIMARY KEY);
        """)


def run(ctx):
    ctx.begin(0)
    with closing(sqlite3.connect(ctx.db)) as db:
        db.execute("UPDATE seats SET remaining = remaining - 1")
        if SPLIT_COMMIT:
            db.commit()
        ctx.marker("after-seat-update")
        db.execute("INSERT INTO bookings VALUES (0)")
        ctx.marker("before-booking-commit")
        db.commit()
        ctx.acknowledge(0)
        ctx.marker("after-acknowledgement")


def observe(ctx, history):
    with closing(sqlite3.connect(ctx.db)) as db:
        remaining, = db.execute("SELECT remaining FROM seats").fetchone()
        booked = {row[0] for row in db.execute("SELECT operation FROM bookings")}
    if remaining + len(booked) != 1:
        raise CheckFailure("booking.capacity", "invariant", "seat and booking totals disagree")
    if not set(history.acknowledged).issubset(booked):
        raise CheckFailure("booking.receipt", "durability", "an acknowledged booking is missing")
