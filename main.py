from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field


API_DIR = Path(__file__).resolve().parent
PROJECT_ROOT_CANDIDATE = API_DIR.parent.parent
PROJECT_ROOT = PROJECT_ROOT_CANDIDATE if (PROJECT_ROOT_CANDIDATE / "App").exists() else API_DIR
ENV_PATHS = [PROJECT_ROOT / ".env.local", PROJECT_ROOT / ".env.firebase.local"]


def load_local_env() -> None:
    for env_path in ENV_PATHS:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() and key.strip() not in os.environ:
                os.environ[key.strip()] = value.strip()


load_local_env()

DATA_DIR = Path(os.getenv("APP_DATA_DIR", str(API_DIR / "data"))).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "science_collab.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ROLES = ["이끄미", "기록이", "나누미", "지키미"]
ALLOWED_FILE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}
MAX_FILE_BYTES = 5 * 1024 * 1024

app = FastAPI(title="과학 모둠 탐구실 API", version="0.2.0")

configured_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if origin.strip()
]

def health_only_mode() -> bool:
    return os.getenv("DEPLOYMENT_MODE", "full").strip().lower() == "health-only"


@app.middleware("http")
async def deployment_mode_guard(request: Request, call_next):
    if health_only_mode() and request.url.path not in {"/", "/api/health"}:
        return JSONResponse(status_code=503, content={"detail": "배포 상태 확인 중입니다."})
    return await call_next(request)


# Register CORS after the deployment guard so it wraps guard-generated 503 responses too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_origins,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1|10\.[0-9.]+|192\.168\.[0-9.]+):[0-9]+$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Teacher-Code"],
)


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                class_name TEXT NOT NULL,
                topic TEXT NOT NULL,
                objective TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                started_at TEXT,
                ends_at TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                group_no INTEGER NOT NULL CHECK(group_no BETWEEN 1 AND 6),
                role TEXT NOT NULL,
                session_token TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(activity_id, name, group_no),
                FOREIGN KEY(activity_id) REFERENCES activities(id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id INTEGER NOT NULL,
                group_no INTEGER NOT NULL,
                sender_name TEXT NOT NULL,
                sender_role TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'text',
                content TEXT NOT NULL,
                attachment_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(activity_id) REFERENCES activities(id)
            );
            CREATE TABLE IF NOT EXISTS work_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id INTEGER NOT NULL,
                group_no INTEGER NOT NULL,
                student_name TEXT NOT NULL,
                role TEXT NOT NULL,
                task TEXT NOT NULL,
                process TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '확인 대기',
                evidence_message_id INTEGER,
                confirmed_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(activity_id) REFERENCES activities(id)
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id INTEGER NOT NULL,
                group_no INTEGER NOT NULL,
                content TEXT NOT NULL,
                published_by TEXT NOT NULL DEFAULT '교사',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(activity_id, group_no),
                FOREIGN KEY(activity_id) REFERENCES activities(id)
            );
            """
        )
        exists = connection.execute("SELECT 1 FROM activities WHERE code = ?", ("SCIENCE6",)).fetchone()
        if not exists:
            start = datetime.now(timezone.utc)
            connection.execute(
                """INSERT INTO activities
                (code, class_name, topic, objective, steps_json, started_at, ends_at, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    "SCIENCE6",
                    "5학년 1반",
                    "우리 주변에서 물이 증발하는 조건은 무엇일까?",
                    "근거를 나누고 역할에 따라 탐구 계획을 세운다.",
                    json.dumps(["궁금한 점 모으기", "역할과 할 일 정하기", "근거 자료 모으기", "탐구 계획 공유하기"], ensure_ascii=False),
                    start.isoformat(),
                    (start + timedelta(minutes=60)).isoformat(),
                ),
            )


init_db()


def activity_by_code(code: str) -> sqlite3.Row:
    with db() as connection:
        row = connection.execute("SELECT * FROM activities WHERE code = ?", (code.strip().upper(),)).fetchone()
    if not row:
        raise HTTPException(404, "활동 코드를 확인해 주세요.")
    return row


def student_by_token(token: str | None) -> tuple[sqlite3.Row, sqlite3.Row]:
    if not token:
        raise HTTPException(401, "로그인이 필요합니다.")
    with db() as connection:
        row = connection.execute(
            """SELECT s.*, a.code, a.active, a.ends_at
            FROM students s JOIN activities a ON a.id = s.activity_id
            WHERE s.session_token = ?""",
            (token,),
        ).fetchone()
        if not row:
            raise HTTPException(401, "로그인 정보를 다시 확인해 주세요.")
        activity = connection.execute("SELECT * FROM activities WHERE id = ?", (row["activity_id"],)).fetchone()
    return row, activity


def activity_open(activity: sqlite3.Row) -> bool:
    if not activity["active"]:
        return False
    if not activity["ends_at"]:
        return True
    return datetime.now(timezone.utc) < datetime.fromisoformat(activity["ends_at"])


def activity_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "code": row["code"],
        "className": row["class_name"],
        "topic": row["topic"],
        "objective": row["objective"],
        "steps": json.loads(row["steps_json"]),
        "startedAt": row["started_at"],
        "endsAt": row["ends_at"],
        "isOpen": activity_open(row),
    }


class LoginBody(BaseModel):
    activity_code: str = Field(min_length=4, max_length=20)
    class_name: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=20)
    group_no: int = Field(ge=1, le=6)


class WorkConfirmBody(BaseModel):
    task: str = Field(min_length=1, max_length=300)
    process: str = Field(default="", max_length=500)


class ActivityUpdateBody(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    objective: str = Field(min_length=2, max_length=300)
    steps: list[str] = Field(min_length=1, max_length=8)
    minutes: int = Field(ge=1, le=180)


class FeedbackPublishBody(BaseModel):
    content: str = Field(min_length=10, max_length=1500)


class StudentGroupUpdateBody(BaseModel):
    group_no: int = Field(ge=1, le=6)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def service_info() -> dict[str, str]:
    return {"service": "science-collab-api", "status": "ok", "docs": "/docs"}


@app.get("/api/activity/{code}")
def get_activity(code: str) -> dict[str, Any]:
    return activity_payload(activity_by_code(code))


@app.post("/api/login")
def login(body: LoginBody) -> dict[str, Any]:
    activity = activity_by_code(body.activity_code)
    if body.class_name.strip() != activity["class_name"]:
        raise HTTPException(400, "학급 정보를 확인해 주세요.")
    clean_name = " ".join(body.name.split())
    with db() as connection:
        existing = connection.execute(
            "SELECT * FROM students WHERE activity_id = ? AND name = ? AND group_no = ?",
            (activity["id"], clean_name, body.group_no),
        ).fetchone()
        if existing:
            token = secrets.token_urlsafe(24)
            connection.execute("UPDATE students SET session_token = ? WHERE id = ?", (token, existing["id"]))
            role = existing["role"]
        else:
            same_name = connection.execute(
                "SELECT group_no FROM students WHERE activity_id = ? AND name = ? ORDER BY group_no",
                (activity["id"], clean_name),
            ).fetchall()
            if len(same_name) == 1:
                raise HTTPException(400, f"교사가 배정한 {same_name[0]['group_no']}모둠으로 로그인해 주세요.")
            count = connection.execute(
                "SELECT COUNT(*) FROM students WHERE activity_id = ? AND group_no = ?",
                (activity["id"], body.group_no),
            ).fetchone()[0]
            role = ROLES[count % len(ROLES)]
            token = secrets.token_urlsafe(24)
            connection.execute(
                """INSERT INTO students
                (activity_id, name, group_no, role, session_token, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (activity["id"], clean_name, body.group_no, role, token, now_iso()),
            )
    return {
        "token": token,
        "student": {"name": clean_name, "groupNo": body.group_no, "role": role},
        "activity": activity_payload(activity),
    }


@app.get("/api/bootstrap")
def bootstrap(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    student, activity = student_by_token(token)
    with db() as connection:
        messages = connection.execute(
            """SELECT * FROM messages WHERE activity_id = ? AND group_no = ?
            ORDER BY id DESC LIMIT 80""",
            (student["activity_id"], student["group_no"]),
        ).fetchall()[::-1]
        items = connection.execute(
            """SELECT * FROM work_items WHERE activity_id = ? AND group_no = ?
            ORDER BY id DESC LIMIT 40""",
            (student["activity_id"], student["group_no"]),
        ).fetchall()
        members = connection.execute(
            """SELECT name, role FROM students WHERE activity_id = ? AND group_no = ? ORDER BY id""",
            (student["activity_id"], student["group_no"]),
        ).fetchall()
        feedback = connection.execute(
            "SELECT * FROM feedback WHERE activity_id = ? AND group_no = ?",
            (student["activity_id"], student["group_no"]),
        ).fetchone()
    return {
        "student": {"name": student["name"], "groupNo": student["group_no"], "role": student["role"]},
        "activity": activity_payload(activity),
        "members": [dict(row) for row in members],
        "messages": [serialize_message(row) for row in messages],
        "workItems": [serialize_work_item(row) for row in items],
        "feedback": serialize_feedback(feedback) if feedback else None,
    }


def serialize_message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "sender": row["sender_name"],
        "role": row["sender_role"],
        "kind": row["kind"],
        "content": row["content"],
        "attachment": json.loads(row["attachment_json"]) if row["attachment_json"] else None,
        "createdAt": row["created_at"],
    }


def serialize_work_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "studentName": row["student_name"],
        "role": row["role"],
        "task": row["task"],
        "process": row["process"],
        "status": row["status"],
        "confirmedBy": row["confirmed_by"],
        "updatedAt": row["updated_at"],
    }


def serialize_feedback(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "content": row["content"],
        "publishedBy": row["published_by"],
        "updatedAt": row["updated_at"],
    }


class RoomHub:
    def __init__(self) -> None:
        self.rooms: dict[str, set[WebSocket]] = {}

    def key(self, activity_id: int, group_no: int) -> str:
        return f"{activity_id}:{group_no}"

    async def connect(self, key: str, socket: WebSocket) -> None:
        await socket.accept()
        self.rooms.setdefault(key, set()).add(socket)

    def disconnect(self, key: str, socket: WebSocket) -> None:
        if key in self.rooms:
            self.rooms[key].discard(socket)
            if not self.rooms[key]:
                self.rooms.pop(key, None)

    async def broadcast(self, key: str, event: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for socket in self.rooms.get(key, set()):
            try:
                await socket.send_json(event)
            except Exception:
                dead.append(socket)
        for socket in dead:
            self.disconnect(key, socket)


hub = RoomHub()


@app.websocket("/ws")
async def websocket_room(websocket: WebSocket, token: str = Query(...)) -> None:
    if health_only_mode():
        await websocket.close(code=1013, reason="배포 상태 확인 중입니다.")
        return
    try:
        student, activity = student_by_token(token)
    except HTTPException:
        await websocket.close(code=4401)
        return
    room_key = hub.key(student["activity_id"], student["group_no"])
    await hub.connect(room_key, websocket)
    await hub.broadcast(room_key, {"type": "presence", "message": f"{student['name']} 학생이 들어왔습니다."})
    try:
        while True:
            payload = await websocket.receive_json()
            if payload.get("type") != "message":
                continue
            fresh_activity = activity_by_code(activity["code"])
            if not activity_open(fresh_activity):
                await websocket.send_json({"type": "error", "message": "활동 시간이 끝나 채팅이 멈췄습니다."})
                continue
            content = str(payload.get("content", "")).strip()[:1000]
            attachment = payload.get("attachment")
            if not content and not attachment:
                continue
            with db() as connection:
                cursor = connection.execute(
                    """INSERT INTO messages
                    (activity_id, group_no, sender_name, sender_role, kind, content, attachment_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        student["activity_id"], student["group_no"], student["name"], student["role"],
                        "file" if attachment else "text", content,
                        json.dumps(attachment, ensure_ascii=False) if attachment else None, now_iso(),
                    ),
                )
                row = connection.execute("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
            await hub.broadcast(room_key, {"type": "message", "message": serialize_message(row)})
    except WebSocketDisconnect:
        hub.disconnect(room_key, websocket)


@app.post("/api/files")
async def upload_file(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=120),
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    student, activity = student_by_token(token)
    if not activity_open(activity):
        raise HTTPException(403, "활동 시간이 끝나 파일을 올릴 수 없습니다.")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in ALLOWED_FILE_TYPES:
        raise HTTPException(415, "이미지 또는 PDF 파일만 올릴 수 있습니다.")
    data = await request.body()
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "파일은 5MB 이하만 올릴 수 있습니다.")
    safe_id = secrets.token_urlsafe(18)
    suffix = ALLOWED_FILE_TYPES[content_type]
    stored_name = f"{student['activity_id']}_{student['group_no']}_{safe_id}{suffix}"
    (UPLOAD_DIR / stored_name).write_bytes(data)
    return {"name": Path(filename).name, "url": f"/uploads/{stored_name}", "type": content_type}


@app.post("/api/work-items/{item_id}/confirm")
async def confirm_work_item(
    item_id: int,
    body: WorkConfirmBody,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    student, _ = student_by_token(token)
    with db() as connection:
        item = connection.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
        if not item or item["activity_id"] != student["activity_id"] or item["group_no"] != student["group_no"]:
            raise HTTPException(404, "작업 기록을 찾을 수 없습니다.")
        connection.execute(
            """UPDATE work_items SET task = ?, process = ?, status = '확정', confirmed_by = ?, updated_at = ?
            WHERE id = ?""",
            (body.task, body.process, student["name"], now_iso(), item_id),
        )
        updated = connection.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
    event = {"type": "work_item", "workItem": serialize_work_item(updated)}
    await hub.broadcast(hub.key(student["activity_id"], student["group_no"]), event)
    return event["workItem"]


@app.post("/api/ai/suggest")
async def ai_suggest(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    student, activity = student_by_token(token)
    if not activity_open(activity):
        raise HTTPException(403, "활동 시간이 끝나 AI 도움을 받을 수 없습니다.")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "AI 설정을 확인해 주세요.")
    with db() as connection:
        messages = connection.execute(
            """SELECT sender_name, sender_role, content FROM messages
            WHERE activity_id = ? AND group_no = ? ORDER BY id DESC LIMIT 8""",
            (student["activity_id"], student["group_no"]),
        ).fetchall()[::-1]
        confirmed = connection.execute(
            """SELECT student_name, role, task, process FROM work_items
            WHERE activity_id = ? AND group_no = ? AND status = '확정' ORDER BY id DESC LIMIT 8""",
            (student["activity_id"], student["group_no"]),
        ).fetchall()
        members = connection.execute(
            "SELECT name, role FROM students WHERE activity_id = ? AND group_no = ? ORDER BY id",
            (student["activity_id"], student["group_no"]),
        ).fetchall()
    compact_context = {
        "topic": activity["topic"],
        "objective": activity["objective"],
        "members": [dict(row) for row in members],
        "confirmedWork": [dict(row) for row in confirmed],
        "recentChat": [dict(row) for row in messages],
    }
    instruction = """너는 초등학교 5~6학년 과학 탐구 모둠의 보조교사다.
정답이나 결론을 대신 만들지 말고, 최근 대화를 근거로 가장 필요한 다음 행동 하나만 제안한다.
반드시 JSON 한 개만 반환한다. 형식:
{"message":"학생들에게 보여줄 짧은 도움말", "studentName":"담당 학생 이름", "role":"이끄미|기록이|나누미|지키미", "task":"할 일", "process":"지금까지 확인된 과정"}
정보가 부족하면 message에 학생들이 먼저 답할 질문을 쓰고 task에는 그 질문에 답하기를 쓴다."""
    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            instructions=instruction,
            input=json.dumps(compact_context, ensure_ascii=False),
            max_output_tokens=260,
        )
        text = response.output_text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        suggestion = json.loads(text)
    except Exception as exc:
        raise HTTPException(502, "AI 응답을 만들지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    role = suggestion.get("role") if suggestion.get("role") in ROLES else student["role"]
    member_names = {row["name"] for row in members}
    assignee = suggestion.get("studentName") if suggestion.get("studentName") in member_names else student["name"]
    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO work_items
            (activity_id, group_no, student_name, role, task, process, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '확인 대기', ?, ?)""",
            (
                student["activity_id"], student["group_no"], assignee, role,
                str(suggestion.get("task", "다음 할 일을 함께 정하기"))[:300],
                str(suggestion.get("process", ""))[:500], now_iso(), now_iso(),
            ),
        )
        item = connection.execute("SELECT * FROM work_items WHERE id = ?", (cursor.lastrowid,)).fetchone()
        message_cursor = connection.execute(
            """INSERT INTO messages
            (activity_id, group_no, sender_name, sender_role, kind, content, created_at)
            VALUES (?, ?, 'AI 보조교사', '도움', 'ai', ?, ?)""",
            (student["activity_id"], student["group_no"], str(suggestion.get("message", "다음 할 일을 확인해 보세요."))[:600], now_iso()),
        )
        ai_message = connection.execute("SELECT * FROM messages WHERE id = ?", (message_cursor.lastrowid,)).fetchone()
    room_key = hub.key(student["activity_id"], student["group_no"])
    await hub.broadcast(room_key, {"type": "message", "message": serialize_message(ai_message)})
    await hub.broadcast(room_key, {"type": "work_item", "workItem": serialize_work_item(item)})
    return {"message": serialize_message(ai_message), "workItem": serialize_work_item(item)}


def teacher_authorized(code: str | None) -> None:
    expected = os.getenv("TEACHER_CODE", "teacher-demo")
    if not code or not secrets.compare_digest(code, expected):
        raise HTTPException(403, "교사 코드를 확인해 주세요.")


def rebalance_roles(connection: sqlite3.Connection, activity_id: int, group_no: int) -> None:
    students = connection.execute(
        "SELECT id FROM students WHERE activity_id = ? AND group_no = ? ORDER BY id",
        (activity_id, group_no),
    ).fetchall()
    for index, student in enumerate(students):
        connection.execute("UPDATE students SET role = ? WHERE id = ?", (ROLES[index % len(ROLES)], student["id"]))


@app.get("/api/teacher/overview/{activity_code}")
def teacher_overview(activity_code: str, x_teacher_code: str | None = Header(default=None)) -> dict[str, Any]:
    teacher_authorized(x_teacher_code)
    activity = activity_by_code(activity_code)
    groups: list[dict[str, Any]] = []
    with db() as connection:
        for group_no in range(1, 7):
            members = connection.execute(
                "SELECT id, name, role, group_no FROM students WHERE activity_id = ? AND group_no = ? ORDER BY id",
                (activity["id"], group_no),
            ).fetchall()
            messages = connection.execute(
                """SELECT * FROM messages WHERE activity_id = ? AND group_no = ?
                ORDER BY id DESC LIMIT 30""",
                (activity["id"], group_no),
            ).fetchall()[::-1]
            items = connection.execute(
                """SELECT * FROM work_items WHERE activity_id = ? AND group_no = ?
                ORDER BY id DESC LIMIT 20""",
                (activity["id"], group_no),
            ).fetchall()
            feedback = connection.execute(
                "SELECT * FROM feedback WHERE activity_id = ? AND group_no = ?",
                (activity["id"], group_no),
            ).fetchone()
            groups.append({
                "groupNo": group_no,
                "members": [dict(row) for row in members],
                "messages": [serialize_message(row) for row in messages],
                "workItems": [serialize_work_item(row) for row in items],
                "feedback": serialize_feedback(feedback) if feedback else None,
            })
    return {"activity": activity_payload(activity), "groups": groups}


@app.put("/api/teacher/students/{student_id}")
async def update_student_group(
    student_id: int,
    body: StudentGroupUpdateBody,
    x_teacher_code: str | None = Header(default=None),
) -> dict[str, Any]:
    teacher_authorized(x_teacher_code)
    with db() as connection:
        student = connection.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        if not student:
            raise HTTPException(404, "등록된 학생을 찾을 수 없습니다.")
        old_group = student["group_no"]
        if old_group == body.group_no:
            return {"id": student["id"], "name": student["name"], "groupNo": old_group, "role": student["role"]}
        collision = connection.execute(
            "SELECT 1 FROM students WHERE activity_id = ? AND name = ? AND group_no = ?",
            (student["activity_id"], student["name"], body.group_no),
        ).fetchone()
        if collision:
            raise HTTPException(409, "이름이 같은 학생이 대상 모둠에 이미 등록되어 있습니다.")
        connection.execute("UPDATE students SET group_no = ? WHERE id = ?", (body.group_no, student_id))
        rebalance_roles(connection, student["activity_id"], old_group)
        rebalance_roles(connection, student["activity_id"], body.group_no)
        updated = connection.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    await hub.broadcast(
        hub.key(student["activity_id"], old_group),
        {"type": "student_moved", "studentName": student["name"], "groupNo": body.group_no},
    )
    await hub.broadcast(hub.key(student["activity_id"], body.group_no), {"type": "members_changed"})
    return {"id": updated["id"], "name": updated["name"], "groupNo": updated["group_no"], "role": updated["role"]}


@app.delete("/api/teacher/students/{student_id}")
async def delete_student_registration(
    student_id: int,
    x_teacher_code: str | None = Header(default=None),
) -> dict[str, bool]:
    teacher_authorized(x_teacher_code)
    with db() as connection:
        student = connection.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        if not student:
            raise HTTPException(404, "등록된 학생을 찾을 수 없습니다.")
        connection.execute("DELETE FROM students WHERE id = ?", (student_id,))
        rebalance_roles(connection, student["activity_id"], student["group_no"])
    await hub.broadcast(
        hub.key(student["activity_id"], student["group_no"]),
        {"type": "student_removed", "studentName": student["name"]},
    )
    return {"deleted": True}


@app.delete("/api/teacher/activities/{activity_code}/students")
async def reset_activity_students(
    activity_code: str,
    x_teacher_code: str | None = Header(default=None),
) -> dict[str, int]:
    teacher_authorized(x_teacher_code)
    activity = activity_by_code(activity_code)
    with db() as connection:
        count = connection.execute("SELECT COUNT(*) FROM students WHERE activity_id = ?", (activity["id"],)).fetchone()[0]
        connection.execute("DELETE FROM students WHERE activity_id = ?", (activity["id"],))
    for group_no in range(1, 7):
        await hub.broadcast(hub.key(activity["id"], group_no), {"type": "registration_reset"})
    return {"deletedStudents": count}


@app.delete("/api/teacher/groups/{activity_code}/{group_no}/conversation")
async def reset_group_conversation(
    activity_code: str,
    group_no: int,
    x_teacher_code: str | None = Header(default=None),
) -> dict[str, int]:
    teacher_authorized(x_teacher_code)
    if group_no < 1 or group_no > 6:
        raise HTTPException(400, "모둠 번호를 확인해 주세요.")
    activity = activity_by_code(activity_code)
    with db() as connection:
        message_count = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE activity_id = ? AND group_no = ?",
            (activity["id"], group_no),
        ).fetchone()[0]
        connection.execute("DELETE FROM messages WHERE activity_id = ? AND group_no = ?", (activity["id"], group_no))
        connection.execute("DELETE FROM work_items WHERE activity_id = ? AND group_no = ?", (activity["id"], group_no))
        connection.execute("DELETE FROM feedback WHERE activity_id = ? AND group_no = ?", (activity["id"], group_no))
    removed_files = 0
    for file_path in UPLOAD_DIR.glob(f"{activity['id']}_{group_no}_*"):
        if file_path.is_file():
            file_path.unlink()
            removed_files += 1
    await hub.broadcast(hub.key(activity["id"], group_no), {"type": "conversation_reset"})
    return {"deletedMessages": message_count, "deletedFiles": removed_files}


@app.put("/api/teacher/activity/{activity_code}")
def update_activity(
    activity_code: str,
    body: ActivityUpdateBody,
    x_teacher_code: str | None = Header(default=None),
) -> dict[str, Any]:
    teacher_authorized(x_teacher_code)
    activity = activity_by_code(activity_code)
    start = datetime.now(timezone.utc)
    with db() as connection:
        connection.execute(
            """UPDATE activities SET topic = ?, objective = ?, steps_json = ?, started_at = ?, ends_at = ?, active = 1
            WHERE id = ?""",
            (
                body.topic.strip(), body.objective.strip(), json.dumps(body.steps, ensure_ascii=False),
                start.isoformat(), (start + timedelta(minutes=body.minutes)).isoformat(), activity["id"],
            ),
        )
        updated = connection.execute("SELECT * FROM activities WHERE id = ?", (activity["id"],)).fetchone()
    return activity_payload(updated)


@app.post("/api/teacher/feedback/{activity_code}/{group_no}/draft")
def draft_feedback(
    activity_code: str,
    group_no: int,
    x_teacher_code: str | None = Header(default=None),
) -> dict[str, str]:
    teacher_authorized(x_teacher_code)
    if group_no < 1 or group_no > 6:
        raise HTTPException(400, "모둠 번호를 확인해 주세요.")
    activity = activity_by_code(activity_code)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "AI 설정을 확인해 주세요.")
    with db() as connection:
        messages = connection.execute(
            """SELECT sender_name, sender_role, content FROM messages
            WHERE activity_id = ? AND group_no = ? ORDER BY id DESC LIMIT 20""",
            (activity["id"], group_no),
        ).fetchall()[::-1]
        items = connection.execute(
            """SELECT student_name, role, task, process, status FROM work_items
            WHERE activity_id = ? AND group_no = ? ORDER BY id DESC LIMIT 12""",
            (activity["id"], group_no),
        ).fetchall()
    if not messages and not items:
        raise HTTPException(400, "피드백을 만들 활동 기록이 아직 없습니다.")
    context = {
        "topic": activity["topic"],
        "objective": activity["objective"],
        "recentChat": [dict(row) for row in messages],
        "workItems": [dict(row) for row in items],
    }
    instruction = """너는 초등학교 5~6학년 과학 모둠 활동의 과정 피드백 초안을 만드는 보조교사다.
학생을 비교하거나 성격·능력을 단정하지 말고, 메시지 수로 참여도를 판단하지 않는다.
제공된 기록에 있는 행동만 근거로 한국어 피드백을 작성한다.
형식은 반드시 아래 세 항목이며 전체 450자 이내다.
잘한 과정: 구체적인 협력 행동 1가지
함께 해결한 점: 의견이나 역할을 연결한 과정 1가지
다음에 시도할 행동: 다음 활동에서 할 수 있는 작은 행동 1가지
교사가 검토할 초안이므로 점수나 등급은 쓰지 않는다."""
    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            instructions=instruction,
            input=json.dumps(context, ensure_ascii=False),
            max_output_tokens=300,
        )
        draft = response.output_text.strip()
    except Exception as exc:
        raise HTTPException(502, "피드백 초안을 만들지 못했습니다.") from exc
    return {"draft": draft}


@app.post("/api/teacher/feedback/{activity_code}/{group_no}/publish")
async def publish_feedback(
    activity_code: str,
    group_no: int,
    body: FeedbackPublishBody,
    x_teacher_code: str | None = Header(default=None),
) -> dict[str, Any]:
    teacher_authorized(x_teacher_code)
    if group_no < 1 or group_no > 6:
        raise HTTPException(400, "모둠 번호를 확인해 주세요.")
    activity = activity_by_code(activity_code)
    timestamp = now_iso()
    with db() as connection:
        connection.execute(
            """INSERT INTO feedback (activity_id, group_no, content, published_by, created_at, updated_at)
            VALUES (?, ?, ?, '교사', ?, ?)
            ON CONFLICT(activity_id, group_no) DO UPDATE SET
            content = excluded.content, published_by = excluded.published_by, updated_at = excluded.updated_at""",
            (activity["id"], group_no, body.content.strip(), timestamp, timestamp),
        )
        feedback = connection.execute(
            "SELECT * FROM feedback WHERE activity_id = ? AND group_no = ?",
            (activity["id"], group_no),
        ).fetchone()
    payload = serialize_feedback(feedback)
    await hub.broadcast(hub.key(activity["id"], group_no), {"type": "feedback", "feedback": payload})
    return payload


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
