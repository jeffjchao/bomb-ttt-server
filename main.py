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

# Allow your Vercel frontend to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # You can restrict this to your Vercel URL later
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
    WAITING = "waiting"        # Waiting for second player
    BOMB = "bomb"              # Bomber places a bomb
    MOVE = "move"              # Active player places their mark
    GAME_OVER = "game_over"


@dataclass
class Player:
    ws: WebSocket
    mark: str                  # "X" or "O"
    name: str = ""


@dataclass
class GameRoom:
    code: str
    board: list = field(default_factory=lambda: [None] * 9)
    players: dict = field(default_factory=dict)  # player_id -> Player
    phase: Phase = Phase.WAITING
    active_player_id: Optional[str] = None    # Who places a mark this turn
    bomber_id: Optional[str] = None           # Who places a bomb this turn
    bomb_cell: Optional[int] = None
    turn_number: int = 1
    history: list = field(default_factory=list)
    scores: dict = field(default_factory=lambda: {"X": 0, "O": 0})


# -------- Room Management --------

rooms: dict[str, GameRoom] = {}


def generate_room_code() -> str:
    """Generate a unique 4-character room code."""
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=4))
        if code not in rooms:
            return code


async def send_to_player(player: Player, message: dict):
    """Send a JSON message to a player, ignoring errors if disconnected."""
    try:
        await player.ws.send_json(message)
    except Exception:
        pass


async def broadcast(room: GameRoom, message: dict):
    """Send a message to all players in a room."""
    for player in room.players.values():
        await send_to_player(player, message)


def get_opponent_id(room: GameRoom, player_id: str) -> Optional[str]:
    """Get the other player's ID."""
    for pid in room.players:
        if pid != player_id:
            return pid
    return None


def get_public_state(room: GameRoom) -> dict:
    """Return the game state that's safe to send to both players (no bomb info)."""
    return {
        "board": room.board,
        "turn_number": room.turn_number,
        "scores": room.scores,
        "history": room.history,
    }


async def start_turn(room: GameRoom):
    """Begin a new turn: tell the bomber to place a bomb."""
    room.phase = Phase.BOMB
    room.bomb_cell = None

    bomber = room.players[room.bomber_id]
    active = room.players[room.active_player_id]

    # Tell the bomber to place a bomb
    await send_to_player(bomber, {
        "type": "your_turn_bomb",
        "message": "Secretly place a bomb on an empty cell",
        **get_public_state(room),
    })

    # Tell the active player to wait
    await send_to_player(active, {
        "type": "wait",
        "message": "Opponent is planning... hold tight",
        **get_public_state(room),
    })


async def prompt_move(room: GameRoom):
    """After bomb is placed, tell the active player to move."""
    room.phase = Phase.MOVE

    active = room.players[room.active_player_id]
    bomber = room.players[room.bomber_id]

    await send_to_player(active, {
        "type": "your_turn_move",
        "message": f"Place your {active.mark}",
        "mark": active.mark,
        **get_public_state(room),
    })

    await send_to_player(bomber, {
        "type": "wait",
        "message": "Opponent is choosing where to play...",
        **get_public_state(room),
    })


async def resolve_move(room: GameRoom, move_cell: int):
    """Resolve the active player's move: check bomb, win, draw."""
    active = room.players[room.active_player_id]
    bomber = room.players[room.bomber_id]

    # Place the mark on the board
    room.board[move_cell] = active.mark

    # Did they hit the bomb?
    if move_cell == room.bomb_cell:
        room.scores[bomber.mark] += 1
        history_entry = {
            "turn": room.turn_number,
            "player": active.mark,
            "move": move_cell,
            "bomb": room.bomb_cell,
            "result": "BOOM",
        }
        room.history.append(history_entry)
        room.phase = Phase.GAME_OVER

        await broadcast(room, {
            "type": "bomb_hit",
            "loser_mark": active.mark,
            "winner_mark": bomber.mark,
            "move_cell": move_cell,
            "bomb_cell": room.bomb_cell,
            **get_public_state(room),
        })
        return

    # Check for win
    win_result = check_winner(room.board)
    if win_result:
        room.scores[active.mark] += 1
        history_entry = {
            "turn": room.turn_number,
            "player": active.mark,
            "move": move_cell,
            "bomb": room.bomb_cell,
            "result": "WIN",
        }
        room.history.append(history_entry)
        room.phase = Phase.GAME_OVER

        await broadcast(room, {
            "type": "win",
            "winner_mark": win_result["winner"],
            "win_line": win_result["line"],
            "move_cell": move_cell,
            "bomb_cell": room.bomb_cell,
            **get_public_state(room),
        })
        return

    # Check for draw
    if is_board_full(room.board):
        history_entry = {
            "turn": room.turn_number,
            "player": active.mark,
            "move": move_cell,
            "bomb": room.bomb_cell,
            "result": "DRAW",
        }
        room.history.append(history_entry)
        room.phase = Phase.GAME_OVER

        await broadcast(room, {
            "type": "draw",
            "move_cell": move_cell,
            "bomb_cell": room.bomb_cell,
            **get_public_state(room),
        })
        return

    # Game continues — log the safe move and swap roles
    history_entry = {
        "turn": room.turn_number,
        "player": active.mark,
        "move": move_cell,
        "bomb": room.bomb_cell,
        "result": "safe",
    }
    room.history.append(history_entry)

    # Reveal the bomb location to both players (it was a miss)
    await broadcast(room, {
        "type": "move_safe",
        "move_cell": move_cell,
        "bomb_cell": room.bomb_cell,
        "mover_mark": active.mark,
        **get_public_state(room),
    })

    # Swap roles
    room.active_player_id, room.bomber_id = room.bomber_id, room.active_player_id
    room.turn_number += 1
    room.bomb_cell = None

    # Small delay so clients can show the bomb reveal, then start next turn
    await asyncio.sleep(1.5)
    await start_turn(room)


async def reset_room(room: GameRoom):
    """Reset the board for a new game, keep scores and players."""
    room.board = [None] * 9
    room.phase = Phase.BOMB
    room.bomb_cell = None
    room.turn_number = 1
    room.history = []

    # Swap who goes first each game
    room.active_player_id, room.bomber_id = room.bomber_id, room.active_player_id

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

    # Determine if creating or joining
    room_code = room_code.upper()
    player_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))

    if room_code == "NEW":
        # Create a new room
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
            await websocket.send_json({
                "type": "error",
                "message": "Room is full",
            })
            await websocket.close()
            return

        player = Player(ws=websocket, mark="O", name="Player 2")
        room.players[player_id] = player

        # Set up initial roles: X moves first, O bombs first
        player_ids = list(room.players.keys())
        room.active_player_id = player_ids[0]   # X goes first
        room.bomber_id = player_ids[1]           # O bombs first

        await send_to_player(player, {
            "type": "room_joined",
            "room_code": room_code,
            "your_mark": "O",
            "message": "Joined! Game starting...",
        })

        # Notify player 1
        opponent = room.players[player_ids[0]]
        await send_to_player(opponent, {
            "type": "opponent_joined",
            "message": "Opponent joined! Game starting...",
        })

        # Start the game
        await asyncio.sleep(1)
        await start_turn(room)

    else:
        await websocket.send_json({
            "type": "error",
            "message": f"Room {room_code} not found",
        })
        await websocket.close()
        return

    # Find which room this player is in
    actual_code = room_code if room_code != "NEW" else list(
        c for c, r in rooms.items() if player_id in r.players
    )[0]
    room = rooms[actual_code]

    # Main message loop
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "bomb" and room.phase == Phase.BOMB:
                # Only the bomber can place a bomb
                if player_id != room.bomber_id:
                    await send_to_player(room.players[player_id], {
                        "type": "error",
                        "message": "It's not your turn to bomb",
                    })
                    continue

                cell = data.get("cell")
                if not isinstance(cell, int) or cell < 0 or cell > 8:
                    continue
                if room.board[cell] is not None:
                    await send_to_player(room.players[player_id], {
                        "type": "error",
                        "message": "That cell is already taken",
                    })
                    continue

                room.bomb_cell = cell
                # Confirm to bomber (don't reveal to opponent)
                await send_to_player(room.players[player_id], {
                    "type": "bomb_placed",
                    "message": "Bomb planted! Waiting for opponent's move...",
                })

                await prompt_move(room)

            elif action == "move" and room.phase == Phase.MOVE:
                # Only the active player can move
                if player_id != room.active_player_id:
                    await send_to_player(room.players[player_id], {
                        "type": "error",
                        "message": "It's not your turn to move",
                    })
                    continue

                cell = data.get("cell")
                if not isinstance(cell, int) or cell < 0 or cell > 8:
                    continue
                if room.board[cell] is not None:
                    await send_to_player(room.players[player_id], {
                        "type": "error",
                        "message": "That cell is already taken",
                    })
                    continue

                await resolve_move(room, cell)

            elif action == "play_again" and room.phase == Phase.GAME_OVER:
                # Simple: first player to ask triggers reset
                # (A more robust version would require both players to agree)
                await reset_room(room)

            elif action == "ping":
                await send_to_player(room.players[player_id], {
                    "type": "pong",
                })

    except WebSocketDisconnect:
        # Player disconnected
        if player_id in room.players:
            del room.players[player_id]

        if len(room.players) == 0:
            # Both gone — clean up
            if actual_code in rooms:
                del rooms[actual_code]
        else:
            # Notify remaining player
            for p in room.players.values():
                await send_to_player(p, {
                    "type": "opponent_disconnected",
                    "message": "Your opponent disconnected",
                })
            room.phase = Phase.GAME_OVER


# -------- Health Check --------

@app.get("/")
async def health():
    return {"status": "ok", "rooms": len(rooms)}
