
# -*- coding: utf-8 -*-
"""
Rubika Music Userbot - Owner / Admin / Special Edition
Target: rubpy 7.3.5

Private:
  - Only invite links are processed.
  - No reply is sent to private messages.

Group:
  - First user sending فعال becomes group owner.
  - Owner can add/remove admins and special users.
  - Owner, admins and special users can use music commands.
  - Only owner can manage access.
  - ویسکال : create voice chat
  - کال    : show RTC notice
  - Reply to audio + پخش : play music
"""

import asyncio
import inspect
import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from rubpy import Client

try:
    from rubpy import voice_chat_player as exported_voice_chat_player
except Exception:
    exported_voice_chat_player = None


# ------------------------- Paths / session -------------------------

APP_NAME = "Rubika_Music"
BASE_DIR = Path(__file__).resolve().parent

RAILWAY_DATA = Path("/app/data")

if RAILWAY_DATA.exists() and os.access(str(RAILWAY_DATA), os.W_OK):
    DATA_DIR = RAILWAY_DATA
else:
    DATA_DIR = BASE_DIR / "rubika_data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "music_userbot.sqlite3"

DOWNLOAD_DIR = DATA_DIR / "music_files"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_AUDIO_SECONDS = 60 * 60 * 3

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s"
)

log = logging.getLogger("rubika-music")

NEW_SESSION_FILE = BASE_DIR / f"{APP_NAME}.rp"

OLD_SESSION_CANDIDATES = (
    BASE_DIR / "Rubika_Music_Userbot.rp",
    BASE_DIR / "rubika_account.rp",
    DATA_DIR / "Rubika_Music_Userbot.rp",
    DATA_DIR / "rubika_account.rp",
)

if not NEW_SESSION_FILE.exists():
    for old_file in OLD_SESSION_CANDIDATES:
        if old_file.exists():
            try:
                import shutil
                shutil.copy2(old_file, NEW_SESSION_FILE)
                log.info(
                    "Copied existing session file: %s",
                    old_file.name
                )
                break
            except Exception:
                log.exception("Could not copy old session file")


app = Client(
    APP_NAME,
    lang_code="fa",
    platform="Android"
)


# ------------------------- State -------------------------

voice_calls = {}
play_locks = {}
active_connections = {}

# Group owners
owners = {}

# Group access:
# access_roles[group_guid][user_guid] = "admin" or "special"
access_roles = {}


# ------------------------- Generic helpers -------------------------

def getv(obj, *names, default=None):

    for name in names:
        try:
            if isinstance(obj, dict):
                value = obj.get(name)
            else:
                value = getattr(obj, name, None)

            if value is not None:
                return value

        except Exception:
            pass

    return default


def as_dict(obj):

    if isinstance(obj, dict):
        return obj

    try:
        return vars(obj)
    except Exception:
        return {}


def unwrap_message(update):

    return (
        getv(
            update,
            "new_message",
            "message",
            "updated_message",
            default=None
        )
        or update
    )


def message_text(message):

    value = getv(
        message,
        "text",
        "message",
        default=""
    )

    if not isinstance(value, str):
        value = getv(
            value,
            "text",
            default=""
        )

    return (value or "").strip()


def chat_guid(update, message):

    return (
        getv(
            update,
            "object_guid",
            "chat_id",
            "chat_guid",
            default=None
        )
        or getv(
            message,
            "object_guid",
            "chat_id",
            "chat_guid",
            default=None
        )
    )


def private_chat(update, message, guid):

    flag = getv(
        update,
        "is_private",
        default=None
    )

    if flag is None:
        flag = getv(
            message,
            "is_private",
            default=None
        )

    if flag is not None:
        return bool(flag)

    return str(guid or "").startswith("u0")


def extract_rubika_link(text):

    match = re.search(
        r"https?://[^\s]+",
        text or ""
    )

    if not match:
        return None

    link = match.group(0).rstrip(
        ".,!؟)]}"
    )

    host = (
        urlparse(link).hostname or ""
    ).lower()

    if (
        host == "rubika.ir"
        or host.endswith(".rubika.ir")
        or host == "rubika.com"
        or host.endswith(".rubika.com")
    ):
        return link

    return None


async def maybe_await(value):

    if inspect.isawaitable(value):
        return await value

    return value


# ------------------------- Owner / access database -------------------------

def init_owner_database():

    with sqlite3.connect(DB_PATH) as con:

        con.execute("""
            CREATE TABLE IF NOT EXISTS group_owners (
                group_guid TEXT PRIMARY KEY,
                owner_guid TEXT NOT NULL,
                updated_at INTEGER
            )
        """)

        # New table for admins and special users
        con.execute("""
            CREATE TABLE IF NOT EXISTS group_access (
                group_guid TEXT NOT NULL,
                user_guid TEXT NOT NULL,
                role TEXT NOT NULL,
                updated_at INTEGER,
                PRIMARY KEY (group_guid, user_guid)
            )
        """)

        owner_rows = con.execute(
            "SELECT group_guid, owner_guid FROM group_owners"
        ).fetchall()

        for group_guid, owner_guid in owner_rows:
            owners[group_guid] = owner_guid

        access_rows = con.execute(
            "SELECT group_guid, user_guid, role FROM group_access"
        ).fetchall()

        for group_guid, user_guid, role in access_rows:

            if group_guid not in access_roles:
                access_roles[group_guid] = {}

            access_roles[group_guid][user_guid] = role

    log.info(
        "Loaded %s group owners and %s access entries",
        len(owners),
        sum(len(users) for users in access_roles.values())
    )


def get_sender_guid(update, message):

    sender = getv(
        message,
        "author_object_guid",
        "sender_object_guid",
        "sender_guid",
        "user_guid",
        "from_id",
        "sender_id",
        default=None
    )

    if sender is None:

        sender = getv(
            update,
            "author_object_guid",
            "sender_object_guid",
            "sender_guid",
            "user_guid",
            "from_id",
            "sender_id",
            default=None
        )

    if sender is None:

        author = getv(
            message,
            "author",
            "sender",
            "from",
            default=None
        )

        sender = getv(
            author,
            "object_guid",
            "user_guid",
            "guid",
            "id",
            default=None
        )

    return (
        str(sender)
        if sender is not None
        else None
    )


def set_group_owner(group_guid, owner_guid):

    with sqlite3.connect(DB_PATH) as con:

        con.execute("""
            INSERT OR REPLACE INTO group_owners
            (group_guid, owner_guid, updated_at)
            VALUES (?, ?, ?)
        """, (
            group_guid,
            owner_guid,
            int(time.time())
        ))

    owners[group_guid] = owner_guid

    log.info(
        "Group owner registered: %s",
        group_guid
    )


def is_group_owner(group_guid, sender_guid):

    owner_guid = owners.get(group_guid)

    return (
        owner_guid is not None
        and sender_guid is not None
        and str(owner_guid) == str(sender_guid)
    )


def set_group_role(group_guid, user_guid, role):

    if role not in ("admin", "special"):
        return False

    with sqlite3.connect(DB_PATH) as con:

        con.execute("""
            INSERT OR REPLACE INTO group_access
            (group_guid, user_guid, role, updated_at)
            VALUES (?, ?, ?, ?)
        """, (
            group_guid,
            user_guid,
            role,
            int(time.time())
        ))

    access_roles.setdefault(
        group_guid,
        {}
    )[user_guid] = role

    log.info(
        "Role assigned: group=%s user=%s role=%s",
        group_guid,
        user_guid,
        role
    )

    return True


def remove_group_role(group_guid, user_guid, role):

    current = access_roles.get(
        group_guid,
        {}
    ).get(user_guid)

    if current != role:
        return False

    with sqlite3.connect(DB_PATH) as con:

        con.execute("""
            DELETE FROM group_access
            WHERE group_guid = ?
            AND user_guid = ?
            AND role = ?
        """, (
            group_guid,
            user_guid,
            role
        ))

    access_roles.get(
        group_guid,
        {}
    ).pop(user_guid, None)

    log.info(
        "Role removed: group=%s user=%s role=%s",
        group_guid,
        user_guid,
        role
    )

    return True


def get_group_role(group_guid, user_guid):

    if not user_guid:
        return None

    return access_roles.get(
        group_guid,
        {}
    ).get(str(user_guid))


def has_group_access(group_guid, user_guid):

    if is_group_owner(group_guid, user_guid):
        return True

    return get_group_role(
        group_guid,
        user_guid
    ) in ("admin", "special")


# ------------------------- Reply system -------------------------

def get_message_id(message):

    return getv(
        message,
        "message_id",
        "msg_id",
        "id",
        default=None
    )


async def send_group_notice(
    guid,
    text,
    reply_message=None
):

    reply_id = None

    if reply_message is not None:
        reply_id = get_message_id(reply_message)

    if reply_id is not None:

        try:

            return await maybe_await(
                app.send_message(
                    guid,
                    text,
                    reply_to_message_id=str(reply_id)
                )
            )

        except TypeError:

            log.warning(
                "send_message does not accept reply_to_message_id"
            )

        except Exception:

            log.exception(
                "Reply send failed; trying normal message"
            )

    return await maybe_await(
        app.send_message(
            guid,
            text
        )
    )


# ------------------------- Private invite joining -------------------------

async def join_from_private_link(link):

    for method_name in (
        "join_group_by_link",
        "join_group",
        "join_chat"
    ):

        method = getattr(
            app,
            method_name,
            None
        )

        if not callable(method):
            continue

        try:

            result = await maybe_await(
                method(link)
            )

            return True, str(result)[:300]

        except Exception as exc:

            log.warning(
                "%s failed for invite link: %s",
                method_name,
                exc
            )

    resolver = getattr(
        app,
        "get_link_from_app_url",
        None
    )

    if callable(resolver):

        try:

            result = await maybe_await(
                resolver(link)
            )

            return (
                False,
                "لینک شناسایی شد اما عضویت مستقیم در این نسخه "
                f"تأیید نشد: {str(result)[:200]}"
            )

        except Exception as exc:

            return False, str(exc)[:250]

    return (
        False,
        "در این نسخه متد عضویت از لینک پیدا نشد."
    )


def save_link_result(link, ok, note):

    with sqlite3.connect(DB_PATH) as con:

        con.execute("""
            CREATE TABLE IF NOT EXISTS joined_links (
                link TEXT PRIMARY KEY,
                status TEXT,
                note TEXT,
                updated_at INTEGER
            )
        """)

        con.execute(
            """
            INSERT OR REPLACE INTO joined_links
            (link,status,note,updated_at)
            VALUES(?,?,?,?)
            """,
            (
                link,
                "requested" if ok else "failed",
                note,
                int(time.time())
            )
        )


# ------------------------- Voice call creation / RTC -------------------------

async def create_voice_call(group_guid):

    if not str(group_guid).startswith("g0"):
        raise RuntimeError(
            "شناسهٔ این گفتگو گروهی نیست."
        )

    method = getattr(
        app,
        "create_group_voice_chat",
        None
    )

    if callable(method):

        result = await maybe_await(
            method(group_guid)
        )

    else:

        builder = getattr(
            app,
            "builder",
            None
        )

        if not callable(builder):
            raise RuntimeError(
                "متد ایجاد ویس‌کال در Rubpy پیدا نشد."
            )

        result = await maybe_await(
            builder(
                "createGroupVoiceChat",
                input={
                    "chat_guid": group_guid
                }
            )
        )

    status = getv(
        result,
        "status",
        "status_det",
        default=None
    )

    if status and str(status).upper() not in (
        "OK",
        "SUCCESS",
        "200"
    ):
        raise RuntimeError(
            f"پاسخ روبیکا: {status}"
        )

    voice_calls[group_guid] = result

    return result


def get_voice_player():

    if callable(exported_voice_chat_player):
        return exported_voice_chat_player

    candidate = getattr(
        app,
        "voice_chat_player",
        None
    )

    if callable(candidate):
        return candidate

    return None


def audio_duration(path):

    try:

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path)
            ],
            capture_output=True,
            text=True,
            timeout=15
        )

        if result.returncode == 0:

            duration = float(
                result.stdout.strip()
            )

            if 0 < duration <= MAX_AUDIO_SECONDS:
                return duration

    except Exception:
        pass

    return None


async def close_rtc_connection(connection):

    if connection is None:
        return

    close_method = getv(
        connection,
        "close",
        default=None
    )

    if callable(close_method):

        try:

            await maybe_await(
                close_method()
            )

            return

        except Exception:
            pass

    peer = getv(
        connection,
        "peer_connection",
        default=None
    )

    if peer is not None:

        try:
            await maybe_await(
                peer.close()
            )

        except Exception:
            pass


async def stream_to_voice_chat(
    group_guid,
    audio_path
):

    player = get_voice_player()

    if player is None:

        raise RuntimeError(
            "افزونه RTC نصب یا قابل‌استفاده نیست. "
            "برای پخش زنده باید rubpy[rtc] و "
            "وابستگی‌های رسانه‌ای نصب باشند."
        )

    if group_guid not in voice_calls:

        raise RuntimeError(
            "اول دستور «ویسکال» را اجرا کن."
        )

    connection = await maybe_await(
        player(
            group_guid,
            Path(audio_path)
        )
    )

    active_connections[group_guid] = connection

    duration = audio_duration(
        audio_path
    )

    if duration is None:
        duration = 180

    try:

        await asyncio.sleep(
            duration
        )

    finally:

        active_connections.pop(
            group_guid,
            None
        )

        await close_rtc_connection(
            connection
        )


# ------------------------- Reply-to-audio download -------------------------

def get_reply_id(message):

    return getv(
        message,
        "reply_to_message_id",
        "reply_message_id",
        "reply_to",
        default=None
    )


def get_file_obj(message):

    return getv(
        message,
        "file",
        "media",
        "attachment",
        default=None
    )


def file_id_from_message(message):

    file_obj = get_file_obj(
        message
    )

    return getv(
        file_obj,
        "file_id",
        "id",
        default=None
    )


def file_name_from_message(message):

    file_obj = get_file_obj(
        message
    )

    name = getv(
        file_obj,
        "file_name",
        "name",
        default=None
    )

    if not name:
        name = "rubika_audio"

    suffix = Path(
        str(name)
    ).suffix.lower()

    if suffix not in (
        ".mp3",
        ".m4a",
        ".ogg",
        ".wav",
        ".flac",
        ".aac",
        ".opus"
    ):
        suffix = ".audio"

    safe = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        Path(str(name)).stem
    )[:60] or "audio"

    return safe + suffix


async def get_replied_message(
    group_guid,
    reply_id
):

    method = getattr(
        app,
        "get_messages_by_id",
        None
    )

    if not callable(method):

        raise RuntimeError(
            "متد get_messages_by_id در این نسخه موجود نیست."
        )

    result = await maybe_await(
        method(
            group_guid,
            str(reply_id)
        )
    )

    candidates = getv(
        result,
        "messages",
        "message",
        "data",
        default=None
    )

    if candidates is None:
        candidates = result

    if isinstance(candidates, (list, tuple)):

        if not candidates:
            return None

        return candidates[0]

    if (
        isinstance(candidates, dict)
        and "messages" in candidates
    ):

        items = candidates.get(
            "messages"
        ) or []

        return (
            items[0]
            if items
            else None
        )

    return candidates


async def download_replied_audio(
    group_guid,
    command_message
):

    reply_id = get_reply_id(
        command_message
    )

    if not reply_id:

        raise RuntimeError(
            "روی پیام آهنگ ریپلای کن و فقط «پخش» را بفرست."
        )

    source_message = await get_replied_message(
        group_guid,
        reply_id
    )

    if source_message is None:

        raise RuntimeError(
            "پیام ریپلای‌شده پیدا نشد."
        )

    file_id = file_id_from_message(
        source_message
    )

    if not file_id:

        nested = getv(
            source_message,
            "message",
            "new_message",
            default=None
        )

        file_id = (
            file_id_from_message(nested)
            if nested is not None
            else None
        )

    if not file_id:

        raise RuntimeError(
            "پیام ریپلای‌شده فایل صوتی قابل‌دانلود ندارد."
        )

    download = getattr(
        app,
        "download_file",
        None
    )

    if not callable(download):

        raise RuntimeError(
            "متد download_file در Rubpy موجود نیست."
        )

    target = DOWNLOAD_DIR / (
        f"reply_{abs(hash(str(file_id)))}_"
        f"{file_name_from_message(source_message)}"
    )

    result = await maybe_await(
        download(
            str(file_id),
            save_as=str(target)
        )
    )

    if isinstance(result, (str, Path)):
        target = Path(result)

    if (
        not target.exists()
        or target.stat().st_size == 0
    ):

        raise RuntimeError(
            "دانلود فایل آهنگ ناموفق بود."
        )

    if target.stat().st_size > MAX_FILE_SIZE:

        target.unlink(
            missing_ok=True
        )

        raise RuntimeError(
            "حجم آهنگ بیشتر از ۲۵ مگابایت است."
        )

    return target


# ------------------------- Owner access management -------------------------

async def get_target_user_from_reply(
    group_guid,
    command_message
):

    reply_id = get_reply_id(
        command_message
    )

    if not reply_id:
        return None

    target_message = await get_replied_message(
        group_guid,
        reply_id
    )

    if target_message is None:
        return None

    target_guid = get_sender_guid(
        target_message,
        target_message
    )

    if not target_guid:

        nested = getv(
            target_message,
            "message",
            "new_message",
            default=None
        )

        if nested is not None:
            target_guid = get_sender_guid(
                target_message,
                nested
            )

    return (
        str(target_guid)
        if target_guid
        else None
    )


async def handle_owner_access_command(
    group_guid,
    command,
    message
):

    role_commands = {
        "ادمین",
        "حذف ادمین",
        "ویژه",
        "حذف ویژه"
    }

    list_commands = {
        "لیست ادمین",
        "لیست ویژه"
    }

    if command not in role_commands | list_commands:
        return False

    if command in list_commands:

        role = (
            "admin"
            if command == "لیست ادمین"
            else "special"
        )

        users = access_roles.get(
            group_guid,
            {}
        )

        selected = [
            user_guid
            for user_guid, user_role in users.items()
            if user_role == role
        ]

        title = (
            "👑 فهرست ادمین‌های گروه"
            if role == "admin"
            else "⭐ فهرست کاربران ویژه گروه"
        )

        if selected:

            body = "\n".join(
                f"{index}. {user_guid}"
                for index, user_guid in enumerate(
                    selected,
                    start=1
                )
            )

            result_text = (
                f"{title}\n\n{body}"
            )

        else:

            result_text = (
                f"{title}\n\n"
                "هنوز کاربری ثبت نشده است."
            )

        await send_group_notice(
            group_guid,
            result_text,
            message
        )

        return True

    target_guid = await get_target_user_from_reply(
        group_guid,
        message
    )

    if not target_guid:

        await send_group_notice(
            group_guid,
            "⚠️ برای اجرای این دستور باید روی پیام کاربر موردنظر ریپلای کنی.",
            message
        )

        return True

    if is_group_owner(
        group_guid,
        target_guid
    ):

        await send_group_notice(
            group_guid,
            "⚠️ مالک گروه قابل تغییر به ادمین یا ویژه نیست.",
            message
        )

        return True

    if command == "ادمین":

        set_group_role(
            group_guid,
            target_guid,
            "admin"
        )

        await send_group_notice(
            group_guid,
            "✅ کاربر موردنظر به‌عنوان ادمین ثبت شد.\n"
            "اکنون می‌تواند از دستورات موزیک استفاده کند.",
            message
        )

    elif command == "ویژه":

        set_group_role(
            group_guid,
            target_guid,
            "special"
        )

        await send_group_notice(
            group_guid,
            "⭐ کاربر موردنظر به کاربران ویژه اضافه شد.\n"
            "اکنون می‌تواند از دستورات موزیک استفاده کند.",
            message
        )

    elif command == "حذف ادمین":

        removed = remove_group_role(
            group_guid,
            target_guid,
            "admin"
        )

        await send_group_notice(
            group_guid,
            (
                "✅ دسترسی ادمین کاربر حذف شد."
                if removed
                else "⚠️ این کاربر ادمین این گروه نیست."
            ),
            message
        )

    elif command == "حذف ویژه":

        removed = remove_group_role(
            group_guid,
            target_guid,
            "special"
        )

        await send_group_notice(
            group_guid,
            (
                "✅ دسترسی ویژه کاربر حذف شد."
                if removed
                else "⚠️ این کاربر در فهرست ویژه نیست."
            ),
            message
        )

    return True


# ------------------------- Group commands -------------------------

async def handle_group_command(
    update,
    message,
    group_guid,
    text
):

    command = text.strip().replace(
        "\u200c",
        ""
    )

    sender_guid = get_sender_guid(
        update,
        message
    )

    # ---------------- Activation / Owner ----------------

    if command == "فعال":

        if not sender_guid:

            log.warning(
                "Cannot identify sender for activation"
            )

            return

        current_owner = owners.get(
            group_guid
        )

        if current_owner is None:

            set_group_owner(
                group_guid,
                sender_guid
            )

            await send_group_notice(
                group_guid,
                "✅ ربات موزیک فعال شد.\n"
                "👑 شما مالک این ربات در این گروه هستید.\n\n"
                "برای دسترسی دادن به کاربران:\n"
                "روی پیام آن‌ها ریپلای کن و «ادمین» یا «ویژه» بفرست.",
                message
            )

        else:

            await send_group_notice(
                group_guid,
                "🎵 ربات موزیک فعال است.",
                message
            )

        return

    # ---------------- Owner commands ----------------

    if is_group_owner(
        group_guid,
        sender_guid
    ):

        handled = await handle_owner_access_command(
            group_guid,
            command,
            message
        )

        if handled:
            return

    # ---------------- Access protection ----------------

    if not has_group_access(
        group_guid,
        sender_guid
    ):
        return

    # ---------------- Voice call ----------------

    if command in (
        "ویسکال",
        "ویس کال",
        "کال",
        "شروع ویسکال",
        "شروع ویس کال"
    ):

        try:

            await create_voice_call(
                group_guid
            )

            await send_group_notice(
                group_guid,
                "🎙 ویس‌کال ایجاد شد؛ برای ورود واقعی و پخش زنده، "
                "روی پیام آهنگ ریپلای کن و «پخش» بفرست.",
                message
            )

        except Exception as exc:

            log.exception(
                "voice call creation failed"
            )

            await send_group_notice(
                group_guid,
                "⚠️ ویسکال ایجاد شد اکانت وارد نشد برای وارد شدن اکانت و پخش اهنگ به پیوی من "
                f"{str(exc)[:250]}",
                message
            )

        return

    # ---------------- Call notice ----------------

    if command in (
        "کال",
        "ورود به کال",
        "joincall"
    ):

        if group_guid not in voice_calls:

            await send_group_notice(
                group_guid,
                "⚠️ اول «ویسکال» را بزن.",
                message
            )

        else:

            await send_group_notice(
                group_guid,
                "⚠️ اتصال واقعی کال با تبادل SDP هنگام اجرای "
                "«پخش» برقرار می‌شود؛ ورود خالی به کال پشتیبانی نمی‌شود.",
                message
            )

        return

    # ---------------- Music playback ----------------

    if command == "پخش":

        if group_guid not in voice_calls:

            await send_group_notice(
                group_guid,
                "⚠️ اول «ویسکال» را اجرا کن.",
                message
            )

            return

        lock = play_locks.setdefault(
            group_guid,
            asyncio.Lock()
        )

        if lock.locked():

            await send_group_notice(
                group_guid,
                "⚠️ یک آهنگ در حال پخش است؛ کمی صبر کن.",
                message
            )

            return

        async with lock:

            try:

                audio_path = await download_replied_audio(
                    group_guid,
                    message
                )

                await send_group_notice(
                    group_guid,
                    "🎧 در حال اتصال به ویس‌کال و پخش آهنگ...",
                    message
                )

                await stream_to_voice_chat(
                    group_guid,
                    audio_path
                )

                await send_group_notice(
                    group_guid,
                    "✅ پخش آهنگ تمام شد.",
                    message
                )

            except Exception as exc:

                log.exception(
                    "reply music playback failed"
                )

                await send_group_notice(
                    group_guid,
                    f"⚠️ پخش انجام نشد: {str(exc)[:300]}",
                    message
                )

        return

    # Everything else is ignored.


# ------------------------- Main update handler -------------------------

@app.on_message_updates()
async def on_message(update):

    try:

        message = unwrap_message(
            update
        )

        text = message_text(
            message
        )

        guid = chat_guid(
            update,
            message
        )

        if not guid:
            return

        # ---------------- Private messages ----------------

        if private_chat(
            update,
            message,
            guid
        ):

            link = extract_rubika_link(
                text
            )

            if link:

                ok, note = await join_from_private_link(
                    link
                )

                save_link_result(
                    link,
                    ok,
                    note
                )

                log.info(
                    "Private invite handled: success=%s note=%s",
                    ok,
                    note
                )

            return

        # ---------------- Group messages ----------------

        if not str(guid).startswith("g0"):
            return

        await handle_group_command(
            update,
            message,
            guid,
            text
        )

    except Exception:

        log.exception(
            "update handler failed"
        )


# ------------------------- Main -------------------------

def main():

    init_owner_database()

    print("=" * 48)

    print("RUBIKA MUSIC USERBOT - OWNER / ADMIN / SPECIAL")

    print("Session:", APP_NAME)

    print("Data directory:", DATA_DIR)

    print("Private: invite links only, silent")

    print(
        "Group commands: فعال | ویسکال | کال | reply + پخش"
    )

    print(
        "Owner commands: ادمین | ویژه | حذف ادمین | حذف ویژه"
    )

    print(
        "List commands: لیست ادمین | لیست ویژه"
    )

    print("Owner protection: enabled")

    print("Admin and special access: enabled")

    print("RTC: real SDP join is performed by voice_chat_player")

    print("=" * 48)

    app.run()


if __name__ == "__main__":
    main()
