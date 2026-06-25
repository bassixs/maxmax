import requests
import requests.packages.urllib3 as urllib3
import time
import logging
import os
from dotenv import load_dotenv

load_dotenv()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("MAX_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("MAX_BOT_TOKEN is not set in .env")

BASE_URL = "https://botapi.max.ru"
HEADERS = {"Authorization": TOKEN, "Content-Type": "application/json"}

# ID закрытого чата — устанавливается автоматически когда бот добавлен в чат (событие bot_added)
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-76236081993774"))

STATE_IDLE = "idle"
STATE_WAITING_MEDIA = "waiting_media"
STATE_WAITING_ADDRESS = "waiting_address"

# Храним состояние и данные по user_id
user_states: dict[int, str] = {}
user_data: dict[int, dict] = {}
# chat_id для ответа пользователю (в MAX личный чат != user_id)
user_chat_id: dict[int, int] = {}


# ─── API ──────────────────────────────────────────────────────────────────────

def api_get(path: str, params: dict = None) -> dict:
    try:
        r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=40, verify=False)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"GET {path} error: {e}")
        return {}


def api_post(path: str, params: dict = None, body: dict = None) -> dict:
    try:
        r = requests.post(
            f"{BASE_URL}{path}", headers=HEADERS, params=params, json=body, timeout=15, verify=False
        )
        if not r.ok:
            logger.error(f"POST {path} {params} status={r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}
    except Exception as e:
        logger.error(f"POST {path} error: {e}")
        return {}


def send_message(chat_id: int, text: str, attachments: list = None) -> dict:
    body: dict = {"text": text}
    if attachments:
        body["attachments"] = attachments
    return api_post("/messages", params={"chat_id": chat_id}, body=body)


# ─── Диалог ───────────────────────────────────────────────────────────────────

def start_dialog(user_id: int, chat_id: int, user_name: str):
    user_states[user_id] = STATE_WAITING_MEDIA
    user_chat_id[user_id] = chat_id
    user_data[user_id] = {"media_attachments": [], "complaint_text": "", "user_name": user_name}
    send_message(
        chat_id,
        "Здравствуйте! 👋\n\n"
        "Это бот для подачи обращений о нарушениях на АЗС "
        "(заправка топлива в тару вместо бака автомобиля).\n\n"
        "📎 Пожалуйста, отправьте фото, видео или опишите нарушение текстом.",
    )


def handle_media_step(user_id: int, text: str, attachments: list):
    chat_id = user_chat_id[user_id]
    data = user_data[user_id]

    if text:
        data["complaint_text"] = text

    for att in attachments:
        if att.get("type") in ("image", "video", "file", "sticker"):
            data["media_attachments"].append(att)

    if not text and not attachments:
        send_message(chat_id, "Пожалуйста, отправьте фото, видео или текстовое описание.")
        return

    user_states[user_id] = STATE_WAITING_ADDRESS
    send_message(chat_id, "✅ Принято!\n\n📍 Теперь укажите адрес заправочной станции, где произошло нарушение.")


def handle_address_step(user_id: int, address: str):
    chat_id = user_chat_id[user_id]

    if not address.strip():
        send_message(chat_id, "Пожалуйста, введите адрес заправки.")
        return

    data = user_data[user_id]

    send_message(
        chat_id,
        "🙏 Спасибо за ваше обращение!\n\n"
        "Информация передана в службу контроля. "
        "Нарушение будет рассмотрено в ближайшее время.",
    )

    forward_complaint(user_id, data, address.strip())

    user_states[user_id] = STATE_IDLE
    user_data.pop(user_id, None)


def forward_complaint(user_id: int, data: dict, address: str):
    if not TARGET_CHAT_ID:
        logger.error("TARGET_CHAT_ID не задан — бот ещё не добавлен в закрытый чат!")
        return

    user_name = data.get("user_name", "Неизвестный")
    complaint_text = data.get("complaint_text", "")
    media_list = data.get("media_attachments", [])

    header = (
        "🚨 Новое обращение о нарушении на АЗС\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 От: {user_name} (id: {user_id})\n"
        f"📍 Адрес: {address}"
    )
    if complaint_text:
        header += f"\n\n📝 Описание:\n{complaint_text}"

    send_message(TARGET_CHAT_ID, header, attachments=media_list if media_list else None)

    logger.info(f"Обращение от {user_name} (id={user_id}) переслано в чат {TARGET_CHAT_ID}")


# ─── Обработчики событий ──────────────────────────────────────────────────────

def on_bot_started(update: dict):
    # bot_started: chat_id и user_id внутри update напрямую
    user_id = update.get("user", {}).get("user_id")
    user_name = update.get("user", {}).get("name", "Пользователь")
    chat_id = update.get("chat_id")  # у bot_started chat_id на верхнем уровне

    if not user_id or not chat_id:
        logger.warning(f"bot_started без user_id/chat_id: {update}")
        return

    logger.info(f"bot_started: user_id={user_id} chat_id={chat_id} name={user_name}")
    start_dialog(user_id, chat_id, user_name)


def on_bot_added(update: dict):
    global TARGET_CHAT_ID
    chat_id = update.get("chat_id")
    if chat_id:
        TARGET_CHAT_ID = chat_id
        logger.info(f"Бот добавлен в чат. TARGET_CHAT_ID = {TARGET_CHAT_ID}")


def on_message_created(update: dict):
    msg = update.get("message", {})
    body = msg.get("body", {})
    sender = msg.get("sender", {})
    recipient = msg.get("recipient", {})

    user_id = sender.get("user_id")
    user_name = sender.get("name", "Пользователь")
    # chat_id живёт в message.recipient.chat_id
    chat_id = recipient.get("chat_id")

    if not user_id or not chat_id:
        logger.warning(f"message_created без user_id/chat_id: {update}")
        return

    # Игнорируем сообщения из закрытого чата (TARGET_CHAT_ID)
    if chat_id == TARGET_CHAT_ID:
        return

    # Игнорируем сообщения от самого бота
    if sender.get("is_bot"):
        return

    text = body.get("text", "")
    attachments = body.get("attachments", [])

    logger.info(f"Сообщение от {user_name} (uid={user_id}, chat={chat_id}): {text[:60]!r}")

    state = user_states.get(user_id, STATE_IDLE)

    if state == STATE_IDLE:
        start_dialog(user_id, chat_id, user_name)
        return

    user_chat_id[user_id] = chat_id

    if state == STATE_WAITING_MEDIA:
        handle_media_step(user_id, text, attachments)
    elif state == STATE_WAITING_ADDRESS:
        handle_address_step(user_id, text)


# ─── Polling ──────────────────────────────────────────────────────────────────

HANDLERS = {
    "bot_started": on_bot_started,
    "bot_added": on_bot_added,
    "message_created": on_message_created,
}


def run():
    logger.info("Бот запущен. Ожидание событий...")
    marker = None

    while True:
        try:
            params = {"timeout": 30}
            if marker:
                params["marker"] = marker

            data = api_get("/updates", params=params)
            updates = data.get("updates", [])
            new_marker = data.get("marker")
            if new_marker:
                marker = new_marker

            for upd in updates:
                update_type = upd.get("update_type", "")
                handler = HANDLERS.get(update_type)
                if handler:
                    try:
                        handler(upd)
                    except Exception as e:
                        logger.error(f"Ошибка в {update_type}: {e}", exc_info=True)
                else:
                    logger.debug(f"Игнорируем событие: {update_type}")

        except KeyboardInterrupt:
            logger.info("Бот остановлен.")
            break
        except Exception as e:
            logger.error(f"Ошибка polling: {e}", exc_info=True)
            time.sleep(5)


if __name__ == "__main__":
    run()
