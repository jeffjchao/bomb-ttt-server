import asyncio
import json
import random
import string
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    PLAYING = "playing"       # Both players act simultaneously
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
    players: dict = field(default_factory=dict)  # player_id -> Player
    phase: Phase = Phase.WAITING
    mover_id: Optional[str] = None     # Who places a mark this turn
    bomber_id: Optional[str] = None    # Who places a bomb this turn
    # Pending actions for the current turn (synchronous resolution)
    pending_bomb: Optional[int] = None
    pending_move: Optional[int] = None
    bomber_ready: bool = False
    mover_ready: bool = False
    turn_number: int = 1
    history: list = field(default_factory=list)
    scores: dict = field(default_factory=lambda: {"X": 0, "O": 0})
    resolving: bool = False  # Prevent double-resolution


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

    bomber = room.players[room.bomber_id]
    mover = room.players[room.mover_id]

    # Tell the bomber to place a bomb
    await send_to_player(bomber, {
        "type": "your_action",
        "role": "bomber",
        "message": "Secretly place a bomb on an empty cell 💣",
        **get_public_state(room),
    })

    # Tell the mover to place their mark
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

    # Place the mark on the board
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

    # Swap roles
    room.mover_id, room.bomber_id = room.bomber_id, room.mover_id
    room.turn_number += 1

    # Brief pause for clients to show the result, then start next turn
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

    # Swap who goes first each game
    room.mover_id, room.bomber_id = room.bomber_id, room.mover_id

    await broadcast(room, {
        "type": "new_game",
        "message": "New game starting!",
        **get_public_state(room),
    })

    await asyncio.sleep(0.5)
    await start_turn(room)


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
                    continue  # Already submitted

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
                    continue  # Already submitted

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
            if actual_code in rooms:
                del rooms[actual_code]
        else:
            for p in room.players.values():
                await send_to_player(p, {
                    "type": "opponent_disconnected",
                    "message": "Your opponent disconnected",
                })
            room.phase = Phase.GAME_OVER


@app.get("/")
async def health():
    return {"status": "ok", "rooms": len(rooms)}
