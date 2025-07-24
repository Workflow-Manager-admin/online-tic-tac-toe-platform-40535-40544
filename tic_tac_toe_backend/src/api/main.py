from fastapi import FastAPI, Depends, HTTPException, status, Body
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
from pydantic import BaseModel, Field
from datetime import datetime
from passlib.context import CryptContext
import uuid

# In-memory data stores for demo; replace with persistent database in production
users_db = {}
games_db = {}
moves_db = {}  # {game_id: [Move, ...]}
scores_db = {}  # {username: {'wins': int, 'losses': int, 'draws': int}}

# Authentication settings
SECRET_KEY = "SET_THIS_IN_PRODUCTION"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# ----- Models -----

class UserCreate(BaseModel):
    username: str = Field(..., description="Unique username")
    password: str = Field(..., description="Password (will be hashed)")

class Token(BaseModel):
    access_token: str
    token_type: str

class UserInfo(BaseModel):
    username: str

class GameCreateRequest(BaseModel):
    opponent: Optional[str] = Field(None, description="The opponent's username")
    as_player: Optional[str] = Field(
        None, description="Which player the creator will play as: 'X' or 'O' (default: X)"
    )

class GameState(BaseModel):
    id: str
    player_x: str
    player_o: str
    board: List[List[str]]
    turn: str
    status: str
    winner: Optional[str]
    history: List["Move"]

class Move(BaseModel):
    x: int = Field(..., ge=0, le=2, description="Row (0-2)")
    y: int = Field(..., ge=0, le=2, description="Column (0-2)")
    player: str = Field(..., description="'X' or 'O'")
    move_number: int = Field(..., description="Move number in sequence")

class MoveRequest(BaseModel):
    x: int
    y: int

class GameSummary(BaseModel):
    id: str
    player_x: str
    player_o: str
    created_at: datetime
    status: str
    winner: Optional[str]

class ScoreEntry(BaseModel):
    username: str
    wins: int
    losses: int
    draws: int

GameState.update_forward_refs()

# ----- Password Hashing -----
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain, hashed):
    return pwd_context.verify(plain, hashed)

def hash_password(password):
    return pwd_context.hash(password)

# ----- Fake JWT for Demo (replace with PyJWT for true JWT support) -----
import base64

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

def create_access_token(data: dict):
    # For demo, encode username only (do NOT use in prod)
    username = data.get("sub")
    payload = f"{username}:{datetime.utcnow()}"
    return base64.b64encode(payload.encode()).decode()

def get_current_user(token: str = Depends(oauth2_scheme)) -> UserInfo:
    try:
        payload = base64.b64decode(token).decode()
        username = payload.split(":")[0]
        if username in users_db:
            return UserInfo(username=username)
    except Exception:
        pass
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )

# ----- FastAPI Initialization -----
app = FastAPI(
    title="Tic Tac Toe Backend API",
    description="REST API for user authentication, game management, moves, state/score/history tracking",
    version="1.0.0",
    openapi_tags=[
        {"name": "auth", "description": "User authentication"},
        {"name": "game", "description": "Game creation, board and moves"},
        {"name": "history", "description": "Game history, scoreboard"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

@app.get("/", tags=["health"])
def health_check():
    """Health Check endpoint."""
    return {"message": "Healthy"}

# -------- AUTH ENDPOINTS --------

# PUBLIC_INTERFACE
@app.post("/register", response_model=UserInfo, tags=["auth"], summary="Register new user")
def register(user: UserCreate):
    """
    Register a new user. Username must be unique.
    """
    if user.username in users_db:
        raise HTTPException(status_code=400, detail="Username already exists")
    users_db[user.username] = {"username": user.username, "password": hash_password(user.password)}
    scores_db[user.username] = {'wins': 0, 'losses': 0, 'draws': 0}
    return UserInfo(username=user.username)

# PUBLIC_INTERFACE
@app.post("/login", response_model=Token, tags=["auth"], summary="User login")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Login and get access token.
    """
    user_data = users_db.get(form_data.username)
    if not user_data or not verify_password(form_data.password, user_data["password"]):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    token = create_access_token({"sub": form_data.username})
    return {"access_token": token, "token_type": "bearer"}

# -------- GAME ENDPOINTS --------

def create_empty_board():
    return [["" for _ in range(3)] for _ in range(3)]

def who_is_next(board, player_x, player_o, moves_list):
    if not moves_list or moves_list[-1].player == "O":
        return "X"
    else:
        return "O"

def game_status(board):
    for player in ("X", "O"):
        for i in range(3):
            if all([cell == player for cell in board[i]]):
                return "won", player
            if all([board[j][i] == player for j in range(3)]):
                return "won", player
        if all([board[i][i] == player for i in range(3)]) or all([board[i][2 - i] == player for i in range(3)]):
            return "won", player
    if all([cell != "" for row in board for cell in row]):
        return "draw", None
    return "running", None

def current_game_state(game_obj):
    board = create_empty_board()
    history = moves_db.get(game_obj["id"], [])
    for move in history:
        if 0 <= move.x <= 2 and 0 <= move.y <= 2:
            board[move.x][move.y] = move.player
    status, winner = game_status(board)
    return GameState(
        id=game_obj["id"],
        player_x=game_obj["player_x"],
        player_o=game_obj["player_o"],
        board=board,
        turn=who_is_next(board, game_obj["player_x"], game_obj["player_o"], history),
        status=status,
        winner=winner,
        history=history,
    )

# PUBLIC_INTERFACE
@app.post("/game", response_model=GameState, tags=["game"], summary="Start a new game")
def start_game(
    req: GameCreateRequest = Body(...),
    user: UserInfo = Depends(get_current_user)
):
    """
    Start a new Tic Tac Toe game.
    If opponent is provided, start a two-player game. If not, play as both sides (hotseat).
    """
    as_player = req.as_player.upper() if req.as_player in ('X', 'O') else 'X'
    if req.opponent and req.opponent not in users_db:
        raise HTTPException(status_code=404, detail="Opponent not found")
    game_id = str(uuid.uuid4())
    player_x = user.username if as_player == 'X' else req.opponent or user.username
    player_o = req.opponent if as_player == 'X' else user.username
    game_obj = {
        "id": game_id,
        "player_x": player_x,
        "player_o": player_o,
        "created_at": datetime.utcnow(),
        "status": "running",
        "winner": None
    }
    games_db[game_id] = game_obj
    moves_db[game_id] = []
    return current_game_state(game_obj)

# PUBLIC_INTERFACE
@app.get("/game/{game_id}", response_model=GameState, tags=["game"], summary="Get game state")
def get_game_state(game_id: str, user: UserInfo = Depends(get_current_user)):
    """
    Get the current state of a specific game.
    """
    game = games_db.get(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    return current_game_state(game)

# PUBLIC_INTERFACE
@app.post("/game/{game_id}/move", response_model=GameState, tags=["game"], summary="Make a move")
def make_move(
    game_id: str,
    req: MoveRequest,
    user: UserInfo = Depends(get_current_user)
):
    """
    Make a move in the specified game. Player must have turn.
    """
    game = games_db.get(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    state = current_game_state(game)
    if state.status != "running":
        raise HTTPException(status_code=400, detail=f"Game already finished. Winner: {state.winner}")
    if game["player_x"] != user.username and game["player_o"] != user.username:
        raise HTTPException(status_code=403, detail="You are not a participant of this game")

    player = "X" if user.username == game["player_x"] else "O"
    if state.turn != player:
        raise HTTPException(status_code=400, detail=f"It is not your turn. Turn: {state.turn}")
    if not (0 <= req.x <= 2 and 0 <= req.y <= 2):
        raise HTTPException(status_code=400, detail="Move out of bounds")
    if state.board[req.x][req.y]:
        raise HTTPException(status_code=400, detail="Cell already occupied")
    move_index = len(moves_db[game_id])
    move = Move(
        x=req.x,
        y=req.y,
        player=player,
        move_number=move_index + 1
    )
    moves_db[game_id].append(move)
    # Update game status and scoreboard if ended
    updated_state = current_game_state(game)
    if updated_state.status != "running" and not game["winner"]:
        game["status"] = updated_state.status
        game["winner"] = updated_state.winner
        if updated_state.status == "won":
            winner_name = game["player_x"] if updated_state.winner == "X" else game["player_o"]
            loser_name = game["player_o"] if updated_state.winner == "X" else game["player_x"]
            scores_db[winner_name]["wins"] += 1
            scores_db[loser_name]["losses"] += 1
        elif updated_state.status == "draw":
            scores_db[game["player_x"]]["draws"] += 1
            scores_db[game["player_o"]]["draws"] += 1
    return updated_state

# PUBLIC_INTERFACE
@app.get("/games", response_model=List[GameSummary], tags=["history"], summary="List my games")
def list_games(user: UserInfo = Depends(get_current_user)):
    """
    List all games in which the current user has participated.
    """
    games = [
        GameSummary(
            id=game["id"],
            player_x=game["player_x"],
            player_o=game["player_o"],
            created_at=game["created_at"],
            status=game.get("status", "unknown"),
            winner=game.get("winner"),
        )
        for game in games_db.values()
        if user.username in (game["player_x"], game["player_o"])
    ]
    # sort by newest first
    games.sort(key=lambda g: g.created_at, reverse=True)
    return games

# PUBLIC_INTERFACE
@app.get("/scoreboard", response_model=List[ScoreEntry], tags=["history"], summary="Get scoreboard")
def get_scoreboard():
    """
    Get aggregate win/loss/draw scores for all users.
    """
    scoreboard = [
        ScoreEntry(username=username, **scores)
        for username, scores in scores_db.items()
    ]
    scoreboard.sort(key=lambda s: s.wins, reverse=True)
    return scoreboard

# PUBLIC_INTERFACE
@app.get("/game/{game_id}/history", response_model=List[Move], tags=["history"], summary="Get game move history")
def get_game_history(game_id: str, user: UserInfo = Depends(get_current_user)):
    """
    Get the chronological history of all moves for a given game.
    """
    if game_id not in games_db:
        raise HTTPException(status_code=404, detail="Game not found")
    game = games_db[game_id]
    if user.username not in (game["player_x"], game["player_o"]):
        raise HTTPException(status_code=403, detail="You are not a participant in this game")
    return moves_db.get(game_id, [])

# ----- End of File -----
