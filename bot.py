import io
import os
import re
import logging
from collections import defaultdict
from google import genai
from google.genai import types
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

# ---- Config (environment variables se) ----
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_HISTORY = 20
MAX_FILE_MB = 15

SYSTEM_PROMPT = """
Tum ek Study & Summary Bot ho jo students ki padhai me madad karta hai.

Rules:
- User jis language me likhe (Hindi, Hinglish ya English), usi me jawab do. Sarl bhasha use karo.
- Jawab Telegram ke liye plain text me do. Markdown symbols (**, ##, `) bilkul use mat karo.
  Headings ke liye CAPITAL letters aur points ke liye "•" ya numbers use karo.
- Sirf diye gaye material (PDF, notes, video) ke base par jawab do. Agar jawab material me nahi hai, to saaf bolo.
- Kuch bhi apni taraf se mat banao. Agar kuch pakka nahi pata, to bol do.
- Concepts ko exam ke nazariye se samjhao: definitions, key points, formulas, examples.
- Jawab seedha aur organized rakho, bina faltu bhoomika ke.
"""

TASKS = {
    "summary": (
        "Is material ka summary banao:\n"
        "1) 3-4 line me overview\n"
        "2) KEY POINTS (bullet points me)\n"
        "3) Zaroori terms/definitions agar hain\n"
        "4) Last me 2 line ka conclusion"
    ),
    "notes": (
        "Is material se exam-ready short notes banao: topic-wise headings, har heading ke neeche "
        "chhote bullet points, important definitions, formulas aur yaad rakhne wali baatein."
    ),
    "mcq": (
        "Is material se 10 MCQs banao. Har question ke 4 options (A, B, C, D) do. "
        "Sabhi questions ke baad ANSWER KEY do, jisme sahi option aur 1 line ki explanation ho."
    ),
}

YT_RE = re.compile(
    r"(https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w\-?=&%.]+)"
)

logging.basicConfig(level=logging.INFO)
client = genai.Client(api_key=GEMINI_API_KEY)

history = defaultdict(list)  # chat_id -> conversation (text only)
sources = {}                 # chat_id -> {"kind": "pdf"|"text"|"youtube", ...}


# ---------- helpers ----------
def add_history(chat_id, role, text):
    history[chat_id].append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
    history[chat_id] = history[chat_id][-MAX_HISTORY:]


def source_parts(chat_id):
    src = sources.get(chat_id)
    if not src:
        return []
    if src["kind"] == "pdf":
        return [types.Part.from_bytes(data=src["data"], mime_type="application/pdf")]
    if src["kind"] == "text":
        return [types.Part.from_text(text="STUDY MATERIAL:\n" + src["text"])]
    if src["kind"] == "youtube":
        return [types.Part(file_data=types.FileData(file_uri=src["url"]))]
    return []


async def ask_gemini(chat_id, user_parts, history_text):
    contents = history[chat_id] + [
        types.Content(role="user", parts=source_parts(chat_id) + user_parts)
    ]
    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
        reply = response.text or "Kuch jawab nahi mila, dobara try karo."
    except Exception:
        logging.exception("Gemini error")
        return "Abhi kuch problem aa gayi. File bahut badi ya video private ho sakta hai, thodi der baad try karo."
    add_history(chat_id, "user", history_text)
    add_history(chat_id, "model", reply)
    return reply


async def send_long(bot, chat_id, text):
    for i in range(0, len(text), 4000):  # Telegram limit 4096
        await bot.send_message(chat_id, text[i:i + 4000])


def action_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Summary", callback_data="summary"),
         InlineKeyboardButton("📝 Notes", callback_data="notes")],
        [InlineKeyboardButton("❓ MCQs", callback_data="mcq")],
    ])


async def set_source(update, chat_id, src, label):
    sources[chat_id] = src
    history.pop(chat_id, None)  # naye material par purani baat-cheet saaf
    await update.message.reply_text(
        f"{label} mil gaya. Kya chahiye? Neeche button dabao, ya seedha sawal poochho.",
        reply_markup=action_keyboard(),
    )


async def run_task(bot, chat_id, task):
    if chat_id not in sources:
        await bot.send_message(chat_id, "Pehle PDF, DOCX, TXT file bhejo ya YouTube link bhejo.")
        return
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    reply = await ask_gemini(chat_id, [types.Part.from_text(text=TASKS[task])], f"[{task} request]")
    await send_long(bot, chat_id, reply)


def extract_docx(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    lines = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(lines)


# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Namaste! Main Study & Summary Bot hoon.\n\n"
        "Mujhe bhejo:\n"
        "• PDF / DOCX / TXT file\n"
        "• YouTube video ka link\n"
        "• Kisi topic ki photo\n\n"
        "Phir main Summary, Notes ya MCQs bana dunga, aur tumhare sawalon ke jawab bhi dunga.\n\n"
        "Commands: /summary /notes /mcq /reset"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    history.pop(chat_id, None)
    sources.pop(chat_id, None)
    await update.message.reply_text("Sab kuch reset ho gaya. Naya material bhejo.")


def make_command(task):
    async def cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await run_task(context.bot, update.effective_chat.id, task)
    return cmd


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await run_task(context.bot, query.message.chat_id, query.data)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    doc = update.message.document
    name = (doc.file_name or "").lower()

    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await update.message.reply_text(f"File {MAX_FILE_MB}MB se badi hai. Chhoti file ya kuch pages bhejo.")
        return

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    tg_file = await doc.get_file()
    data = bytes(await tg_file.download_as_bytearray())

    try:
        if name.endswith(".pdf") or doc.mime_type == "application/pdf":
            await set_source(update, chat_id, {"kind": "pdf", "data": data}, "PDF")
        elif name.endswith(".docx"):
            text = extract_docx(data)
            if not text.strip():
                await update.message.reply_text("DOCX me padhne layak text nahi mila.")
                return
            await set_source(update, chat_id, {"kind": "text", "text": text}, "DOCX")
        elif name.endswith(".txt"):
            await set_source(update, chat_id, {"kind": "text", "text": data.decode("utf-8", "ignore")}, "Notes")
        else:
            await update.message.reply_text("Sirf PDF, DOCX aur TXT files support hain.")
    except Exception:
        logging.exception("File error")
        await update.message.reply_text("File padhne me dikkat aayi. Dusri file try karo.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    tg_file = await update.message.photo[-1].get_file()
    image_bytes = bytes(await tg_file.download_as_bytearray())
    caption = update.message.caption or "Is image me jo padhai ka content hai use samjhao aur summary do."
    parts = [
        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        types.Part.from_text(text=caption),
    ]
    reply = await ask_gemini(chat_id, parts, f"[User ne image bheji] {caption}")
    await send_long(context.bot, chat_id, reply)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text

    m = YT_RE.search(text)
    if m:
        await set_source(update, chat_id, {"kind": "youtube", "url": m.group(1)}, "YouTube video")
        return

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    reply = await ask_gemini(chat_id, [types.Part.from_text(text=text)], text)
    await send_long(context.bot, chat_id, reply)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    for task in TASKS:
        app.add_handler(CommandHandler(task, make_command(task)))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()


if __name__ == "__main__":
    main()
