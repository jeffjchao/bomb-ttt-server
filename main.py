import asyncio
import json
import os
import random
import string
import uuid
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------- Database --------

# Set this as an environment variable on Render:
# DATABASE_URL=postgresql://user:pass@ep-something.neon.tech/dbname
DATABASE_URL = os.environ.get("DATABASE_URL")

db_pool = None


async def init_db():
    """Create the connection pool and ensure tables exist."""
    global db_pool
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL not set — game logging disabled")
        return

    try:
        import asyncpg
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS games (
                    game_id         UUID PRIMARY KEY,
                    mode            VARCHAR(12) NOT NULL,
                    difficulty      VARCHAR(10),
                    first_mover_mark CHAR(1) NOT NULL,
                    winner_mark     CHAR(1),
                    outcome         VARCHAR(12) NOT NULL,
                    total_turns     INT NOT NULL,
                    started_at      TIMESTAMPTZ,
                    ended_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    duration_seconds INT
                );

                CREATE TABLE IF NOT EXISTS turns (
                    turn_id     SERIAL PRIMARY KEY,
                    game_id     UUID NOT NULL REFERENCES games(game_id),
                    turn_number INT NOT NULL,
                    mover_mark  CHAR(1) NOT NULL,
                    move_cell   INT NOT NULL,
                    bomb_cell   INT NOT NULL,
                    result      VARCHAR(10) NOT NULL
                );
            """)
        print("Database connected and tables ready")
    except Exception as e:
        print(f"Database init failed: {e} — game logging disabled")
        db_pool = None


async def log_game(
    mode: str,
    difficulty: Optional[str],
    first_mover_mark: str,
    winner_mark: Optional[str],
    outcome: str,
    total_turns: int,
    started_at: Optional[datetime],
    history: list,
):
    """Log a completed game and its turns to the database."""
    if db_pool is None:
        return

    game_id = uuid.uuid4()
    ended_at = datetime.now(timezone.utc)
    duration = None
    if started_at:
        duration = int((ended_at - started_at).total_seconds())

    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO games (game_id, mode, difficulty, first_mover_mark,
                                   winner_mark, outcome, total_turns,
                                   started_at, ended_at, duration_seconds)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """, game_id, mode, difficulty, first_mover_mark,
                winner_mark, outcome, total_turns,
                started_at, ended_at, duration)

            for turn in history:
                await conn.execute("""
                    INSERT INTO turns (game_id, turn_number, mover_mark,
                                      move_cell, bomb_cell, result)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """, game_id, turn["turn"], turn["player"],
                    turn["move"], turn["bomb"], turn["result"])
    except Exception as e:
        print(f"Failed to log game: {e}")


async def log_disconnect(
    first_mover_mark: str,
    total_turns: int,
    started_at: Optional[datetime],
    history: list,
):
    """Log a disconnected multiplayer game for diagnostic purposes."""
    if db_pool is None:
        return

    game_id = uuid.uuid4()
    ended_at = datetime.now(timezone.utc)
    duration = None
    if started_at:
        duration = int((ended_at - started_at).total_seconds())

    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO games (game_id, mode, difficulty, first_mover_mark,
                                   winner_mark, outcome, total_turns,
                                   started_at, ended_at, duration_seconds)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """, game_id, "multiplayer", None, first_mover_mark,
                None, "disconnect", total_turns,
                started_at, ended_at, duration)

            for turn in history:
                await conn.execute("""
                    INSERT INTO turns (game_id, turn_number, mover_mark,
                                      move_cell, bomb_cell, result)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """, game_id, turn["turn"], turn["player"],
                    turn["move"], turn["bomb"], turn["result"])
    except Exception as e:
        print(f"Failed to log disconnect: {e}")


@app.on_event("startup")
async def startup():
    await init_db()


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()


# -------- Game Logic --------

WIN_LINES = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6],
]


def check_winner(board: list) -> Optional[dict]:
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return {"winner": board[a], "line": [a, b, c]}
    return None


def is_board_full(board: list) -> bool:
    return all(cell is not None for cell in board)


# -------- Game Room --------

class Phase(str, Enum):
    WAITING = "waiting"
    PLAYING = "playing"
    GAME_OVER = "game_over"


@dataclass
class Player:
    ws: WebSocket
    mark: str
    name: str = ""


@dataclass
class GameRoom:
    code: str
    board: list = field(default_factory=lambda: [None] * 9)
    players: dict = field(default_factory=dict)
    phase: Phase = Phase.WAITING
    mover_id: Optional[str] = None
    bomber_id: Optional[str] = None
    pending_bomb: Optional[int] = None
    pending_move: Optional[int] = None
    bomber_ready: bool = False
    mover_ready: bool = False
    turn_number: int = 1
    history: list = field(default_factory=list)
    scores: dict = field(default_factory=lambda: {"X": 0, "O": 0})
    resolving: bool = False
    # Tracking fields for logging
    first_mover_mark: str = "X"
    started_at: Optional[datetime] = None


# -------- Room Management --------

rooms: dict[str, GameRoom] = {}


def generate_room_code() -> str:
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=4))
        if code not in rooms:
            return code


async def send_to_player(player: Player, message: dict):
    try:
        await player.ws.send_json(message)
    except Exception:
        pass


async def broadcast(room: GameRoom, message: dict):
    for player in room.players.values():
        await send_to_player(player, message)


def get_opponent_id(room: GameRoom, player_id: str) -> Optional[str]:
    for pid in room.players:
        if pid != player_id:
            return pid
    return None


def get_public_state(room: GameRoom) -> dict:
    return {
        "board": room.board,
        "turn_number": room.turn_number,
        "scores": room.scores,
        "history": room.history,
    }


async def start_turn(room: GameRoom):
    """Begin a new turn: tell both players to act simultaneously."""
    room.phase = Phase.PLAYING
    room.pending_bomb = None
    room.pending_move = None
    room.bomber_ready = False
    room.mover_ready = False
    room.resolving = False

    # Record start time on first turn
    if room.turn_number == 1 and room.started_at is None:
        room.started_at = datetime.now(timezone.utc)

    bomber = room.players[room.bomber_id]
    mover = room.players[room.mover_id]

    await send_to_player(bomber, {
        "type": "your_action",
        "role": "bomber",
        "message": "Secretly place a bomb on an empty cell 💣",
        **get_public_state(room),
    })

    await send_to_player(mover, {
        "type": "your_action",
        "role": "mover",
        "mark": mover.mark,
        "message": f"Place your {mover.mark}",
        **get_public_state(room),
    })


async def try_resolve(room: GameRoom):
    """If both players have submitted, resolve the turn."""
    if not room.bomber_ready or not room.mover_ready:
        return
    if room.resolving:
        return

    room.resolving = True

    bomb_cell = room.pending_bomb
    move_cell = room.pending_move
    mover = room.players[room.mover_id]
    bomber = room.players[room.bomber_id]

    room.board[move_cell] = mover.mark

    # Did they hit the bomb?
    if move_cell == bomb_cell:
        room.scores[bomber.mark] += 1
        room.history.append({
            "turn": room.turn_number,
            "player": mover.mark,
            "move": move_cell,
            "bomb": bomb_cell,
            "result": "BOOM",
        })
        room.phase = Phase.GAME_OVER

        # Log to database
        await log_game(
            mode="multiplayer",
            difficulty=None,
            first_mover_mark=room.first_mover_mark,
            winner_mark=bomber.mark,
            outcome="bomb",
            total_turns=room.turn_number,
            started_at=room.started_at,
            history=room.history,
        )

        await broadcast(room, {
            "type": "turn_result",
            "outcome": "bomb_hit",
            "loser_mark": mover.mark,
            "winner_mark": bomber.mark,
            "move_cell": move_cell,
            "bomb_cell": bomb_cell,
            **get_public_state(room),
        })
        return

    # Check for win
    win_result = check_winner(room.board)
    if win_result:
        room.scores[mover.mark] += 1
        room.history.append({
            "turn": room.turn_number,
            "player": mover.mark,
            "move": move_cell,
            "bomb": bomb_cell,
            "result": "WIN",
        })
        room.phase = Phase.GAME_OVER

        # Log to database
        await log_game(
            mode="multiplayer",
            difficulty=None,
            first_mover_mark=room.first_mover_mark,
            winner_mark=win_result["winner"],
            outcome="win",
            total_turns=room.turn_number,
            started_at=room.started_at,
            history=room.history,
        )

        await broadcast(room, {
            "type": "turn_result",
            "outcome": "win",
            "winner_mark": win_result["winner"],
            "win_line": win_result["line"],
            "move_cell": move_cell,
            "bomb_cell": bomb_cell,
            **get_public_state(room),
        })
        return

    # Check for draw
    if is_board_full(room.board):
        room.history.append({
            "turn": room.turn_number,
            "player": mover.mark,
            "move": move_cell,
            "bomb": bomb_cell,
            "result": "DRAW",
        })
        room.phase = Phase.GAME_OVER

        # Log to database
        await log_game(
            mode="multiplayer",
            difficulty=None,
            first_mover_mark=room.first_mover_mark,
            winner_mark=None,
            outcome="draw",
            total_turns=room.turn_number,
            started_at=room.started_at,
            history=room.history,
        )

        await broadcast(room, {
            "type": "turn_result",
            "outcome": "draw",
            "move_cell": move_cell,
            "bomb_cell": bomb_cell,
            **get_public_state(room),
        })
        return

    # Game continues
    room.history.append({
        "turn": room.turn_number,
        "player": mover.mark,
        "move": move_cell,
        "bomb": bomb_cell,
        "result": "safe",
    })

    await broadcast(room, {
        "type": "turn_result",
        "outcome": "safe",
        "mover_mark": mover.mark,
        "move_cell": move_cell,
        "bomb_cell": bomb_cell,
        **get_public_state(room),
    })

    room.mover_id, room.bomber_id = room.bomber_id, room.mover_id
    room.turn_number += 1

    await asyncio.sleep(2.0)
    await start_turn(room)


async def reset_room(room: GameRoom):
    """Reset the board for a new game."""
    room.board = [None] * 9
    room.phase = Phase.PLAYING
    room.pending_bomb = None
    room.pending_move = None
    room.bomber_ready = False
    room.mover_ready = False
    room.resolving = False
    room.turn_number = 1
    room.history = []
    room.started_at = None

    # Swap who goes first each game
    room.mover_id, room.bomber_id = room.bomber_id, room.mover_id
    # Track the new first mover
    room.first_mover_mark = room.players[room.mover_id].mark

    await broadcast(room, {
        "type": "new_game",
        "message": "New game starting!",
        **get_public_state(room),
    })

    await asyncio.sleep(0.5)
    await start_turn(room)


# -------- REST Endpoint for AI Game Logging --------

@app.post("/log-game")
async def log_ai_game(request: Request):
    """Receive completed AI game data from the frontend."""
    try:
        data = await request.json()

        # Validate required fields
        required = ["mode", "first_mover_mark", "outcome", "total_turns", "history"]
        for field_name in required:
            if field_name not in data:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Missing field: {field_name}"},
                )

        started_at = None
        if data.get("started_at"):
            try:
                started_at = datetime.fromisoformat(data["started_at"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        await log_game(
            mode=data["mode"],
            difficulty=data.get("difficulty"),
            first_mover_mark=data["first_mover_mark"],
            winner_mark=data.get("winner_mark"),
            outcome=data["outcome"],
            total_turns=data["total_turns"],
            started_at=started_at,
            history=data["history"],
        )

        return {"status": "logged"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# -------- Stats Endpoint --------

@app.get("/stats")
async def get_stats():
    """Return aggregate game stats."""
    if db_pool is None:
        return {"error": "Database not connected"}

    try:
        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM games WHERE outcome != 'disconnect'")
            by_outcome = await conn.fetch(
                "SELECT outcome, COUNT(*) as count FROM games WHERE outcome != 'disconnect' GROUP BY outcome"
            )
            by_mode = await conn.fetch(
                "SELECT mode, COUNT(*) as count FROM games WHERE outcome != 'disconnect' GROUP BY mode"
            )
            avg_turns = await conn.fetchval(
                "SELECT ROUND(AVG(total_turns)::numeric, 1) FROM games WHERE outcome != 'disconnect'"
            )
            first_mover_wins = await conn.fetchval(
                "SELECT COUNT(*) FROM games WHERE winner_mark = first_mover_mark AND outcome != 'disconnect'"
            )

        return {
            "total_games": total,
            "by_outcome": {r["outcome"]: r["count"] for r in by_outcome},
            "by_mode": {r["mode"]: r["count"] for r in by_mode},
            "avg_turns": float(avg_turns) if avg_turns else None,
            "first_mover_win_rate": round(first_mover_wins / total * 100, 1) if total > 0 else None,
        }
    except Exception as e:
        return {"error": str(e)}


# -------- WebSocket Endpoint --------

@app.websocket("/ws/{room_code}")
async def websocket_endpoint(websocket: WebSocket, room_code: str):
    await websocket.accept()

    room_code = room_code.upper()
    player_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))

    if room_code == "NEW":
        code = generate_room_code()
        room = GameRoom(code=code)
        rooms[code] = room

        player = Player(ws=websocket, mark="X", name="Player 1")
        room.players[player_id] = player

        await send_to_player(player, {
            "type": "room_created",
            "room_code": code,
            "your_mark": "X",
            "message": f"Room {code} created. Waiting for opponent...",
        })

    elif room_code in rooms:
        room = rooms[room_code]

        if len(room.players) >= 2:
            await websocket.send_json({"type": "error", "message": "Room is full"})
            await websocket.close()
            return

        player = Player(ws=websocket, mark="O", name="Player 2")
        room.players[player_id] = player

        # X moves first, O bombs first
        player_ids = list(room.players.keys())
        room.mover_id = player_ids[0]
        room.bomber_id = player_ids[1]
        room.first_mover_mark = "X"

        await send_to_player(player, {
            "type": "room_joined",
            "room_code": room_code,
            "your_mark": "O",
            "message": "Joined! Game starting...",
        })

        opponent = room.players[player_ids[0]]
        await send_to_player(opponent, {
            "type": "opponent_joined",
            "message": "Opponent joined! Game starting...",
        })

        await asyncio.sleep(1)
        await start_turn(room)

    else:
        await websocket.send_json({"type": "error", "message": f"Room {room_code} not found"})
        await websocket.close()
        return

    # Find the actual room code
    actual_code = room_code if room_code != "NEW" else list(
        c for c, r in rooms.items() if player_id in r.players
    )[0]
    room = rooms[actual_code]

    # Main message loop
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "bomb" and room.phase == Phase.PLAYING:
                if player_id != room.bomber_id:
                    await send_to_player(room.players[player_id], {
                        "type": "error", "message": "You're not the bomber this turn",
                    })
                    continue
                if room.bomber_ready:
                    continue

                cell = data.get("cell")
                if not isinstance(cell, int) or cell < 0 or cell > 8:
                    continue
                if room.board[cell] is not None:
                    await send_to_player(room.players[player_id], {
                        "type": "error", "message": "That cell is already taken",
                    })
                    continue

                room.pending_bomb = cell
                room.bomber_ready = True

                await send_to_player(room.players[player_id], {
                    "type": "action_confirmed",
                    "role": "bomber",
                    "cell": cell,
                    "message": "Bomb planted! Waiting for opponent...",
                })

                await try_resolve(room)

            elif action == "move" and room.phase == Phase.PLAYING:
                if player_id != room.mover_id:
                    await send_to_player(room.players[player_id], {
                        "type": "error", "message": "You're not the mover this turn",
                    })
                    continue
                if room.mover_ready:
                    continue

                cell = data.get("cell")
                if not isinstance(cell, int) or cell < 0 or cell > 8:
                    continue
                if room.board[cell] is not None:
                    await send_to_player(room.players[player_id], {
                        "type": "error", "message": "That cell is already taken",
                    })
                    continue

                room.pending_move = cell
                room.mover_ready = True

                await send_to_player(room.players[player_id], {
                    "type": "action_confirmed",
                    "role": "mover",
                    "cell": cell,
                    "message": "Move locked in! Waiting for opponent...",
                })

                await try_resolve(room)

            elif action == "play_again" and room.phase == Phase.GAME_OVER:
                await reset_room(room)

            elif action == "ping":
                await send_to_player(room.players[player_id], {"type": "pong"})

    except WebSocketDisconnect:
        if player_id in room.players:
            del room.players[player_id]

        if len(room.players) == 0:
            # Log disconnect if game was in progress
            if room.started_at and room.history:
                await log_disconnect(
                    first_mover_mark=room.first_mover_mark,
                    total_turns=room.turn_number,
                    started_at=room.started_at,
                    history=room.history,
                )
            if actual_code in rooms:
                del rooms[actual_code]
        else:
            # Log disconnect if game was in progress
            if room.started_at and room.history:
                await log_disconnect(
                    first_mover_mark=room.first_mover_mark,
                    total_turns=room.turn_number,
                    started_at=room.started_at,
                    history=room.history,
                )
            for p in room.players.values():
                await send_to_player(p, {
                    "type": "opponent_disconnected",
                    "message": "Your opponent disconnected",
                })
            room.phase = Phase.GAME_OVER


@app.get("/")
async def health():
    return {"status": "ok", "rooms": len(rooms), "db": "connected" if db_pool else "disabled"}
