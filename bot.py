import hmac
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path

import requests
import requests.packages.urllib3 as urllib3
from dotenv import load_dotenv

from reports import ComplaintStore, address_top, create_report_xlsx, now_moscow, parse_date_range


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

REPORT_PASSWORD = os.getenv("REPORT_PASSWORD", "2026")
BASE_URL = os.getenv("MAX_API_URL", "https://botapi.max.ru")
HEADERS = {"Authorization": TOKEN, "Content-Type": "application/json"}
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-76236081993774"))

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
REPORTS_DIR = DATA_DIR / "reports"
STORE = ComplaintStore(DATA_DIR / "complaints.db")

STATE_IDLE = "idle"
STATE_WAITING_MEDIA = "waiting_media"
STATE_WAITING_ADDRESS = "waiting_address"
STATE_REPORT_PASSWORD = "report_password"
STATE_REPORT_MENU = "report_menu"
STATE_REPORT_CUSTOM = "report_custom"

user_states: dict[int, str] = {}
user_data: dict[int, dict] = {}
user_chat_id: dict[int, int] = {}
report_periods: dict[int, tuple[date, date]] = {}


def api_get(path: str, params: dict | None = None) -> dict:
    try:
        response = requests.get(
            f"{BASE_URL}{path}",
            headers=HEADERS,
            params=params,
            timeout=40,
            verify=False,
        )
        response.raise_for_status()
        return response.json()
    except Exception as error:
        logger.error("GET %s error: %s", path, error)
        return {}


def api_post(path: str, params: dict | None = None, body: dict | None = None) -> dict:
    try:
        response = requests.post(
            f"{BASE_URL}{path}",
            headers=HEADERS,
            params=params,
            json=body,
            timeout=30,
            verify=False,
        )
        if not response.ok:
            logger.error(
                "POST %s %s status=%s: %s",
                path,
                params,
                response.status_code,
                response.text[:500],
            )
        return response.json() if response.content else {}
    except Exception as error:
        logger.error("POST %s error: %s", path, error)
        return {}


def message_button(text: str, payload: str) -> dict:
    return {"type": "message", "text": text, "payload": payload}


def keyboard(rows: list[list[dict]]) -> dict:
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def send_message(chat_id: int, text: str, attachments: list | None = None) -> dict:
    body: dict = {"text": text}
    if attachments:
        body["attachments"] = attachments
    return api_post("/messages", params={"chat_id": chat_id}, body=body)


def upload_file(path: Path) -> dict | None:
    upload = api_post("/uploads", params={"type": "file"})
    upload_url = upload.get("url")
    if not upload_url:
        logger.error("MAX API did not return an upload URL: %s", upload)
        return None

    try:
        with path.open("rb") as source:
            response = requests.post(
                upload_url,
                files={
                    "data": (
                        path.name,
                        source,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
                timeout=120,
            )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("token") or payload.get("retval")
        if not token:
            logger.error("MAX upload did not return a token: %s", payload)
            return None
        return {"type": "file", "payload": {"token": token}}
    except Exception as error:
        logger.error("File upload error: %s", error, exc_info=True)
        return None


def start_dialog(user_id: int, chat_id: int, user_name: str) -> None:
    user_states[user_id] = STATE_WAITING_MEDIA
    user_chat_id[user_id] = chat_id
    user_data[user_id] = {
        "media_attachments": [],
        "complaint_text": "",
        "user_name": user_name,
    }
    send_message(
        chat_id,
        "Здравствуйте! 👋\n\n"
        "Это бот для подачи обращений о нарушениях на АЗС "
        "(заправка топлива в тару вместо бака автомобиля).\n\n"
        "📎 Пожалуйста, отправьте фото, видео или опишите нарушение текстом.",
    )


def handle_media_step(user_id: int, text: str, attachments: list) -> None:
    chat_id = user_chat_id[user_id]
    data = user_data[user_id]

    if text:
        data["complaint_text"] = text

    for attachment in attachments:
        if attachment.get("type") in ("image", "video", "file", "sticker"):
            data["media_attachments"].append(attachment)

    if not text and not attachments:
        send_message(chat_id, "Пожалуйста, отправьте фото, видео или текстовое описание.")
        return

    user_states[user_id] = STATE_WAITING_ADDRESS
    send_message(
        chat_id,
        "✅ Принято!\n\n"
        "📍 Теперь укажите адрес заправочной станции, где произошло нарушение.",
    )


def handle_address_step(user_id: int, address: str) -> None:
    chat_id = user_chat_id[user_id]
    address = address.strip()
    if not address:
        send_message(chat_id, "Пожалуйста, введите адрес заправки.")
        return

    data = user_data[user_id]
    STORE.add(
        user_id=user_id,
        user_name=data.get("user_name", "Неизвестный"),
        description=data.get("complaint_text", ""),
        address=address,
    )

    send_message(
        chat_id,
        "🙏 Спасибо за ваше обращение!\n\n"
        "Информация передана в службу контроля. "
        "Нарушение будет рассмотрено в ближайшее время.",
    )
    forward_complaint(user_id, data, address)

    user_states[user_id] = STATE_IDLE
    user_data.pop(user_id, None)


def forward_complaint(user_id: int, data: dict, address: str) -> None:
    if not TARGET_CHAT_ID:
        logger.error("TARGET_CHAT_ID не задан")
        return

    user_name = data.get("user_name", "Неизвестный")
    complaint_text = data.get("complaint_text", "")
    media_list = data.get("media_attachments", [])

    header = (
        "🚨 Новое обращение о нарушении на АЗС\n"
        "━━━━━━━━━━━\n"
        f"👤 От: {user_name} (id: {user_id})\n"
        f"📍 Адрес: {address}"
    )
    if complaint_text:
        header += f"\n\n📝 Описание:\n{complaint_text}"

    send_message(
        TARGET_CHAT_ID,
        header,
        attachments=media_list if media_list else None,
    )
    logger.info(
        "Обращение от %s (id=%s) сохранено и переслано в чат %s",
        user_name,
        user_id,
        TARGET_CHAT_ID,
    )


def begin_report(user_id: int, chat_id: int) -> None:
    user_chat_id[user_id] = chat_id
    user_states[user_id] = STATE_REPORT_PASSWORD
    user_data.pop(user_id, None)
    send_message(chat_id, "🔐 Введите пароль администратора:")


def show_report_menu(user_id: int) -> None:
    chat_id = user_chat_id[user_id]
    user_states[user_id] = STATE_REPORT_MENU
    send_message(
        chat_id,
        "Добро пожаловать в админ-панель.\n\n"
        "Пожалуйста, укажите, за какой период нужно сформировать отчёт:",
        attachments=[
            keyboard(
                [
                    [
                        message_button("Сегодня", "/report_today"),
                        message_button("Последние 7 дней", "/report_week"),
                    ],
                    [
                        message_button("Текущий месяц", "/report_month"),
                        message_button("Указать даты", "/report_custom"),
                    ],
                ]
            )
        ],
    )


def resolve_period(command: str) -> tuple[date, date] | None:
    today = now_moscow().date()
    if command in ("/report_today", "сегодня"):
        return today, today
    if command in ("/report_week", "последние 7 дней", "неделя"):
        return today - timedelta(days=6), today
    if command in ("/report_month", "текущий месяц", "месяц"):
        return today.replace(day=1), today
    return None


def send_report_summary(user_id: int, start: date, end: date) -> None:
    chat_id = user_chat_id[user_id]
    rows = STORE.get_period(start, end)
    report_periods[user_id] = (start, end)
    user_states[user_id] = STATE_REPORT_MENU

    period = f"{start:%d.%m.%Y} — {end:%d.%m.%Y}"
    if not rows:
        send_message(
            chat_id,
            f"📊 Отчёт за период {period}\n\n"
            "За выбранный период нарушений не зафиксировано.",
            attachments=[
                keyboard([[message_button("Выбрать другой период", "/report_menu")]])
            ],
        )
        return

    top = address_top(rows)
    top_lines = [
        f"{index}. {address} — {count}"
        for index, (address, count) in enumerate(top, start=1)
    ]
    send_message(
        chat_id,
        f"📊 Отчёт за период {period}\n\n"
        f"Всего зафиксировано нарушений: {len(rows)}\n\n"
        "Чаще всего указывали адреса:\n"
        + "\n".join(top_lines),
        attachments=[
            keyboard(
                [
                    [message_button("Скачать Excel", "/report_excel")],
                    [message_button("Выбрать другой период", "/report_menu")],
                ]
            )
        ],
    )


def send_excel_report(user_id: int) -> None:
    chat_id = user_chat_id[user_id]
    period = report_periods.get(user_id)
    if not period:
        send_message(chat_id, "Сначала выберите период отчёта.")
        show_report_menu(user_id)
        return

    start, end = period
    rows = STORE.get_period(start, end)
    if not rows:
        send_message(chat_id, "За выбранный период нет данных для Excel.")
        return

    filename = f"report_{start:%Y-%m-%d}_{end:%Y-%m-%d}.xlsx"
    report_path = create_report_xlsx(rows, start, end, REPORTS_DIR / filename)
    attachment = upload_file(report_path)
    if not attachment:
        send_message(chat_id, "Не удалось сформировать файл. Попробуйте ещё раз позже.")
        return

    send_message(
        chat_id,
        f"📎 Excel-отчёт за период {start:%d.%m.%Y} — {end:%d.%m.%Y}",
        attachments=[attachment],
    )


def handle_report_input(user_id: int, text: str) -> bool:
    chat_id = user_chat_id[user_id]
    command = text.strip().casefold()
    state = user_states.get(user_id, STATE_IDLE)

    if command == "/cancel":
        user_states[user_id] = STATE_IDLE
        report_periods.pop(user_id, None)
        send_message(chat_id, "Действие отменено.")
        return True

    if state == STATE_REPORT_PASSWORD:
        if hmac.compare_digest(text.strip(), REPORT_PASSWORD):
            show_report_menu(user_id)
        else:
            send_message(chat_id, "❌ Неверный пароль. Попробуйте ещё раз или отправьте /cancel.")
        return True

    if command in ("/report_menu", "выбрать другой период"):
        show_report_menu(user_id)
        return True

    if command == "/report_custom" or command == "указать даты":
        user_states[user_id] = STATE_REPORT_CUSTOM
        send_message(
            chat_id,
            "Введите период в формате:\n"
            "ДД.ММ.ГГГГ - ДД.ММ.ГГГГ\n\n"
            "Обе даты будут включены в отчёт.",
        )
        return True

    if command in ("/report_excel", "скачать excel"):
        send_excel_report(user_id)
        return True

    period = resolve_period(command)
    if period:
        send_report_summary(user_id, *period)
        return True

    if state == STATE_REPORT_CUSTOM:
        try:
            start, end = parse_date_range(text)
        except ValueError as error:
            send_message(chat_id, f"❌ {error}")
            return True
        send_report_summary(user_id, start, end)
        return True

    if state == STATE_REPORT_MENU:
        show_report_menu(user_id)
        return True

    return False


def on_bot_started(update: dict) -> None:
    user = update.get("user", {})
    user_id = user.get("user_id")
    user_name = user.get("name", "Пользователь")
    chat_id = update.get("chat_id")
    if not user_id or not chat_id:
        logger.warning("bot_started без user_id/chat_id: %s", update)
        return
    start_dialog(user_id, chat_id, user_name)


def on_bot_added(update: dict) -> None:
    global TARGET_CHAT_ID
    chat_id = update.get("chat_id")
    if chat_id:
        TARGET_CHAT_ID = chat_id
        logger.info("Бот добавлен в чат. TARGET_CHAT_ID=%s", TARGET_CHAT_ID)


def on_message_created(update: dict) -> None:
    message = update.get("message", {})
    body = message.get("body", {})
    sender = message.get("sender", {})
    recipient = message.get("recipient", {})

    user_id = sender.get("user_id")
    user_name = sender.get("name", "Пользователь")
    chat_id = recipient.get("chat_id")
    if not user_id or not chat_id:
        logger.warning("message_created без user_id/chat_id: %s", update)
        return
    if chat_id == TARGET_CHAT_ID or sender.get("is_bot"):
        return

    text = body.get("text", "")
    attachments = body.get("attachments", [])
    user_chat_id[user_id] = chat_id

    logger.info(
        "Сообщение от %s (uid=%s, chat=%s): %r",
        user_name,
        user_id,
        chat_id,
        text[:60],
    )

    if text.strip().casefold() == "/report":
        begin_report(user_id, chat_id)
        return

    if user_states.get(user_id, STATE_IDLE).startswith("report_"):
        handle_report_input(user_id, text)
        return

    state = user_states.get(user_id, STATE_IDLE)
    if state == STATE_IDLE:
        start_dialog(user_id, chat_id, user_name)
    elif state == STATE_WAITING_MEDIA:
        handle_media_step(user_id, text, attachments)
    elif state == STATE_WAITING_ADDRESS:
        handle_address_step(user_id, text)


HANDLERS = {
    "bot_started": on_bot_started,
    "bot_added": on_bot_added,
    "message_created": on_message_created,
}


def run() -> None:
    logger.info("Бот запущен. Ожидание событий...")
    marker = None

    while True:
        try:
            params = {"timeout": 30}
            if marker:
                params["marker"] = marker

            data = api_get("/updates", params=params)
            updates = data.get("updates", [])
            if data.get("marker"):
                marker = data["marker"]

            for update in updates:
                update_type = update.get("update_type", "")
                handler = HANDLERS.get(update_type)
                if not handler:
                    logger.debug("Игнорируем событие: %s", update_type)
                    continue
                try:
                    handler(update)
                except Exception as error:
                    logger.error(
                        "Ошибка в %s: %s",
                        update_type,
                        error,
                        exc_info=True,
                    )
        except KeyboardInterrupt:
            logger.info("Бот остановлен.")
            break
        except Exception as error:
            logger.error("Ошибка polling: %s", error, exc_info=True)
            time.sleep(5)


if __name__ == "__main__":
    run()
