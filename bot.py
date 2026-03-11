"""
Google Form Auto-Filler Telegram Bot v2
========================================
- Scrapes any public Google Form
- Auto-fills from saved user profile
- Asks ALL unknown questions at once
- Shows confirmation summary before submit
- Handles file uploads via prefilled URL
- Handles all field types: text, MC, dropdown, checkbox, date, time, scale, grid
"""

import json
import logging
import os
import re

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
USER_INFO_FILE = "user_info.json"

TYPE_SHORT_TEXT      = 0
TYPE_PARAGRAPH       = 1
TYPE_MULTIPLE_CHOICE = 2
TYPE_DROPDOWN        = 3
TYPE_CHECKBOX        = 4
TYPE_LINEAR_SCALE    = 5
TYPE_TITLE           = 6
TYPE_GRID            = 7
TYPE_DATE            = 9
TYPE_TIME            = 10
TYPE_FILE_UPLOAD     = 13
SKIP_TYPES           = {TYPE_TITLE}

DEFAULT_INFO = {
    "name": "", "first_name": "", "last_name": "",
    "email": "", "phone": "",
    "address": "", "city": "", "state": "", "country": "India", "pincode": "",
    "college": "", "university": "", "branch": "", "department": "",
    "year": "", "roll_number": "", "student_id": "",
    "company": "", "designation": "",
    "dob": "", "age": "", "gender": "",
    "linkedin": "", "github": "", "website": "",
}

KEYWORD_MAP = [
    (["full name", "your name", "participant name", "applicant name", "student name", "candidate name"], "name"),
    (["first name", "firstname", "given name"], "first_name"),
    (["last name", "lastname", "surname", "family name"], "last_name"),
    (["email", "e-mail", "mail id", "email id", "email address", "gmail", "email-id"], "email"),
    (["phone", "mobile", "contact no", "whatsapp", "phone number", "mobile number", "contact number", "cell"], "phone"),
    (["address", "home address", "residence", "permanent address", "current address", "postal address"], "address"),
    (["city", "town", "district"], "city"),
    (["state", "province"], "state"),
    (["country", "nation"], "country"),
    (["pin", "pincode", "zip", "postal code", "post code", "pin code"], "pincode"),
    (["college", "institution", "institute", "school name", "college name", "name of college"], "college"),
    (["university", "university name", "name of university"], "university"),
    (["branch", "stream", "specialization", "specialisation", "course", "programme", "program", "field of study"], "branch"),
    (["department", "dept", "department name"], "department"),
    (["year of study", "current year", "studying year", "semester", "sem", "academic year", "year of passing"], "year"),
    (["roll no", "roll number", "enrollment", "registration number", "reg no", "register number", "usn", "prn"], "roll_number"),
    (["student id", "id number", "student number", "id no"], "student_id"),
    (["company", "organization", "organisation", "employer", "workplace", "firm", "company name"], "company"),
    (["designation", "job title", "position", "role", "post"], "designation"),
    (["dob", "date of birth", "birth date", "birthday", "born on"], "dob"),
    (["age", "your age", "age (in years)"], "age"),
    (["gender", "sex"], "gender"),
    (["linkedin", "linkedin profile", "linkedin url", "linkedin id"], "linkedin"),
    (["github", "github profile", "github url", "github id"], "github"),
    (["website", "portfolio", "personal website", "portfolio link"], "website"),
]


def load_user_info() -> dict:
    if os.path.exists(USER_INFO_FILE):
        with open(USER_INFO_FILE) as f:
            return json.load(f)
    env_info = os.getenv("USER_INFO_JSON")
    if env_info:
        try:
            return json.loads(env_info)
        except json.JSONDecodeError:
            pass
    return {}


def save_user_info(info: dict):
    try:
        with open(USER_INFO_FILE, "w") as f:
            json.dump(info, f, indent=2)
    except OSError:
        pass
    logger.info("USER_INFO_JSON=%s", json.dumps(info))


def match_field(question: str):
    q = question.lower().strip()
    for keywords, field in KEYWORD_MAP:
        for kw in keywords:
            if kw in q:
                return field
    return None


def get_submit_url(url: str) -> str:
    url = url.strip().split("?")[0]
    url = re.sub(r"/(edit|viewform|prefill)$", "", url)
    return url.rstrip("/") + "/formResponse"


def get_viewform_url(url: str) -> str:
    url = url.strip().split("?")[0]
    url = re.sub(r"/(edit|formResponse|prefill)$", "", url)
    return url.rstrip("/") + "/viewform"


def generate_prefilled_url(form_url: str, answers: dict) -> str:
    base = get_viewform_url(form_url)
    params = []
    for field_id, value in answers.items():
        if field_id.startswith("entry."):
            if isinstance(value, list):
                for v in value:
                    params.append(f"{field_id}={requests.utils.quote(str(v), safe='')}")
            else:
                params.append(f"{field_id}={requests.utils.quote(str(value), safe='')}")
    return base + "?" + "&".join(params) if params else base


def scrape_google_form(url: str) -> list:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    raw_data = None
    for script in soup.find_all("script"):
        if script.string and "FB_PUBLIC_LOAD_DATA_" in script.string:
            raw_data = script.string
            break
    if not raw_data:
        raise ValueError("Could not read form data. Make sure the form is public.")
    match = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*\]);", raw_data, re.DOTALL)
    if not match:
        raise ValueError("Could not parse form structure.")
    data = json.loads(match.group(1))
    questions = []
    try:
        items = data[1][1]
    except (IndexError, TypeError):
        raise ValueError("Unexpected form data structure.")
    for item in items:
        try:
            title = item[1]
            description = item[2] if len(item) > 2 else ""
            q_type_block = item[4]
            if not q_type_block:
                questions.append({"title": title, "field_id": None, "type": TYPE_TITLE,
                                   "options": [], "required": False, "scale_min": None,
                                   "scale_max": None, "description": description})
                continue
            for field_block in q_type_block:
                field_id = f"entry.{field_block[0]}"
                q_type = field_block[2]
                options = []
                scale_min = scale_max = None
                if len(field_block) > 1 and field_block[1]:
                    raw_opts = field_block[1]
                    if q_type == TYPE_LINEAR_SCALE:
                        try:
                            scale_min = raw_opts[0][0]
                            scale_max = raw_opts[1][0]
                        except (IndexError, TypeError):
                            scale_min, scale_max = 1, 5
                    else:
                        options = [opt[0] for opt in raw_opts if opt and opt[0]]
                required = bool(field_block[4]) if len(field_block) > 4 else False
                questions.append({"title": title, "field_id": field_id, "type": q_type,
                                   "options": options, "required": required,
                                   "scale_min": scale_min, "scale_max": scale_max,
                                   "description": description})
        except (IndexError, TypeError):
            continue
    return questions


def auto_fill(questions: list, user_info: dict):
    filled = {}
    unanswered = []
    has_file_upload = False
    for q in questions:
        if q["type"] in SKIP_TYPES or q["field_id"] is None:
            continue
        if q["type"] == TYPE_FILE_UPLOAD:
            has_file_upload = True
            continue
        field = match_field(q["title"])
        if field and user_info.get(field):
            value = user_info[field]
            if q["options"] and q["type"] in (TYPE_MULTIPLE_CHOICE, TYPE_DROPDOWN):
                matched = next(
                    (opt for opt in q["options"]
                     if value.lower() in opt.lower() or opt.lower() in value.lower()),
                    None
                )
                value = matched if matched else value
            filled[q["field_id"]] = value
        else:
            unanswered.append(q)
    return filled, unanswered, has_file_upload


def submit_form(form_url: str, answers: dict) -> bool:
    submit_url = get_submit_url(form_url)
    headers = {"User-Agent": "Mozilla/5.0",
               "Content-Type": "application/x-www-form-urlencoded",
               "Referer": form_url}
    payload = {}
    for k, v in answers.items():
        if isinstance(v, list):
            payload[k] = v  # requests handles repeated keys for checkboxes
        else:
            payload[k] = v
    payload.update({"draftResponse": "[]", "pageHistory": "0"})
    resp = requests.post(submit_url, data=payload, headers=headers, timeout=15, allow_redirects=True)
    return resp.status_code in (200, 302)


def type_label(q_type: int) -> str:
    return {
        TYPE_SHORT_TEXT: "Short answer", TYPE_PARAGRAPH: "Paragraph",
        TYPE_MULTIPLE_CHOICE: "Multiple choice", TYPE_DROPDOWN: "Dropdown",
        TYPE_CHECKBOX: "Checkboxes", TYPE_LINEAR_SCALE: "Linear scale",
        TYPE_GRID: "Grid", TYPE_DATE: "Date", TYPE_TIME: "Time",
        TYPE_FILE_UPLOAD: "File upload",
    }.get(q_type, "Text")


def build_confirmation_message(filled: dict, questions: list) -> str:
    lines = ["📋 *Review your answers before submitting:*\n"]
    for q in questions:
        if q["type"] in SKIP_TYPES or q["field_id"] is None or q["type"] == TYPE_FILE_UPLOAD:
            continue
        value = filled.get(q["field_id"])
        if isinstance(value, list):
            value = ", ".join(value)
        if value:
            lines.append(f"• *{q['title']}*: {value}")
        else:
            lines.append(f"• *{q['title']}*: _(skipped)_")
    lines.append("\nReply *yes* to submit ✅  or  *no* to cancel ❌")
    return "\n".join(lines)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_info = load_user_info()
    has_info = any(user_info.get(k) for k in ["name", "email", "phone"])
    await update.message.reply_text(
        "👋 *Google Form Auto-Filler Bot*\n\n"
        "Paste any Google Form link — I'll fill and submit it for you!\n\n"
        f"{'✅ Your info is saved and ready!' if has_info else '⚠️ No info saved yet — use /setinfo first!'}\n\n"
        "📋 *Commands:*\n"
        "/setinfo — Save your personal details\n"
        "/myinfo — View saved details\n"
        "/help — Full usage guide\n\n"
        "Paste a Google Form URL to begin! 🚀",
        parse_mode="Markdown"
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *How it works:*\n\n"
        "1️⃣ Use /setinfo to save your profile *once*\n"
        "2️⃣ Paste any Google Form URL\n"
        "3️⃣ I auto-fill everything I recognise\n"
        "4️⃣ I show ALL remaining questions at once\n"
        "5️⃣ Reply: `Q1: answer` / `Q2: 3` / `Q3: skip`\n"
        "6️⃣ I show a full confirmation summary\n"
        "7️⃣ Reply *yes* → submitted! ✅\n\n"
        "📎 *File upload forms:* I send a pre-filled link.\n"
        "You just upload the file and click Submit.\n\n"
        "✅ Handles: text, MC, dropdown, checkboxes,\n"
        "date, time, linear scale, grid fields.",
        parse_mode="Markdown"
    )


async def setinfo_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    info = load_user_info()
    preview_keys = ["name", "email", "phone", "address", "city", "state", "pincode",
                    "college", "branch", "year", "roll_number", "company", "dob", "age", "gender"]
    saved_lines = "\n".join(
        f"  `{k}`: {info[k]}" for k in preview_keys if info.get(k)
    ) or "  _(nothing saved yet)_"
    await update.message.reply_text(
        "📝 *Save your profile — send key: value pairs, one per line:*\n\n"
        "`name: Rahul Sharma`\n`email: rahul@gmail.com`\n`phone: 9876543210`\n"
        "`address: 12 MG Road, Andheri`\n`city: Mumbai`\n`state: Maharashtra`\n"
        "`pincode: 400001`\n`college: VJTI Mumbai`\n`branch: Computer Engineering`\n"
        "`year: 3rd Year`\n`roll_number: 2021CE045`\n`company: TCS`\n"
        "`dob: 15/08/2002`\n`age: 22`\n`gender: Male`\n"
        "`linkedin: linkedin.com/in/rahul`\n`github: github.com/rahul`\n\n"
        "📌 *Currently saved:*\n" + saved_lines,
        parse_mode="Markdown"
    )
    ctx.user_data["mode"] = "setinfo"


async def myinfo_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    info = load_user_info()
    if not info or not any(info.values()):
        await update.message.reply_text("Nothing saved yet. Use /setinfo to add your details.")
        return
    lines = "\n".join(f"• *{k}*: {v}" for k, v in info.items() if v)
    await update.message.reply_text(f"📋 *Your saved profile:*\n\n{lines}", parse_mode="Markdown")


async def ask_all_at_once(update: Update, unanswered: list):
    lines = ["📝 *Please answer these questions:*\n",
             "Reply with `Q1: answer`, `Q2: answer` format.\nType `skip` for optional ones.\n"]
    for i, q in enumerate(unanswered):
        tag = " *(required)*" if q["required"] else " _(optional)_"
        lines.append(f"*Q{i+1}: {q['title']}* [{type_label(q['type'])}]{tag}")
        if q.get("description"):
            lines.append(f"   _{q['description']}_")
        if q["type"] in (TYPE_MULTIPLE_CHOICE, TYPE_DROPDOWN, TYPE_CHECKBOX):
            for j, opt in enumerate(q["options"]):
                lines.append(f"   {j+1}. {opt}")
            if q["type"] == TYPE_CHECKBOX:
                lines.append("   _(Select multiple: e.g. `1, 3`)_")
        elif q["type"] == TYPE_LINEAR_SCALE:
            lines.append(f"   Scale: {q['scale_min']} to {q['scale_max']}")
        elif q["type"] == TYPE_DATE:
            lines.append("   _(Format: DD/MM/YYYY)_")
        elif q["type"] == TYPE_TIME:
            lines.append("   _(Format: HH:MM)_")
        elif q["type"] == TYPE_GRID:
            lines.append("   _(Grid question — answer each row)_")
        lines.append("")
    lines.append("_Example:_\n`Q1: Computer Science`\n`Q2: 3`\n`Q3: 1, 2`\n`Q4: skip`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mode = ctx.user_data.get("mode")

    if mode == "setinfo":
        info = load_user_info() or DEFAULT_INFO.copy()
        updated = []
        for line in text.split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip().lower().replace(" ", "_")
                value = value.strip()
                if key in DEFAULT_INFO:
                    info[key] = value
                    if key == "name" and " " in value:
                        parts = value.split(" ", 1)
                        info["first_name"] = parts[0]
                        info["last_name"] = parts[1]
                    updated.append(key)
        if updated:
            save_user_info(info)
            await update.message.reply_text(
                f"✅ *Saved:* {', '.join(updated)}\n\nNow paste a Google Form link!",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("❓ Use format: `name: Your Name`", parse_mode="Markdown")
        ctx.user_data["mode"] = None
        return

    if mode == "bulk_answering":
        unanswered = ctx.user_data.get("unanswered", [])
        filled = ctx.user_data.get("filled", {})
        form_url = ctx.user_data.get("form_url", "")
        has_file_upload = ctx.user_data.get("has_file_upload", False)
        all_questions = ctx.user_data.get("all_questions", [])

        answers_given = {}
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^[Qq]?(\d+)[.:\)]\s*(.+)$", line)
            if m:
                answers_given[int(m.group(1)) - 1] = m.group(2).strip()
        if not answers_given and len(unanswered) == 1:
            answers_given[0] = text

        skipped = []
        for i, q in enumerate(unanswered):
            if i not in answers_given:
                continue
            ans = answers_given[i]
            if ans.lower() in ("skip", "-", "n/a", "none", ""):
                skipped.append(q["title"])
                continue
            if q["type"] == TYPE_CHECKBOX and q["options"]:
                selected = []
                for part in re.split(r"[,;]", ans):
                    part = part.strip()
                    if part.isdigit():
                        idx = int(part) - 1
                        if 0 <= idx < len(q["options"]):
                            selected.append(q["options"][idx])
                    else:
                        selected.append(part)
                filled[q["field_id"]] = selected
                continue
            if q["type"] in (TYPE_MULTIPLE_CHOICE, TYPE_DROPDOWN) and q["options"] and ans.isdigit():
                idx = int(ans) - 1
                if 0 <= idx < len(q["options"]):
                    ans = q["options"][idx]
            filled[q["field_id"]] = ans

        still_missing = [
            (i, q) for i, q in enumerate(unanswered)
            if q["required"] and q["field_id"] not in filled
        ]
        if still_missing:
            missing_text = "\n".join(f"Q{i+1}: {q['title']}" for i, q in still_missing)
            await update.message.reply_text(
                f"⚠️ *Still missing required answers:*\n\n{missing_text}\n\nPlease answer them!",
                parse_mode="Markdown"
            )
            ctx.user_data["unanswered"] = [q for _, q in still_missing]
            ctx.user_data["filled"] = filled
            await ask_all_at_once(update, ctx.user_data["unanswered"])
            return

        ctx.user_data["filled"] = filled
        ctx.user_data["mode"] = "confirming"
        await update.message.reply_text(
            build_confirmation_message(filled, all_questions), parse_mode="Markdown"
        )
        return

    if mode == "confirming":
        filled = ctx.user_data.get("filled", {})
        form_url = ctx.user_data.get("form_url", "")
        has_file_upload = ctx.user_data.get("has_file_upload", False)

        if text.lower() in ("yes", "y", "confirm", "submit", "ok", "haan", "ha"):
            if has_file_upload:
                prefilled = generate_prefilled_url(form_url, filled)
                await update.message.reply_text(
                    "⚠️ *This form has a file upload field.*\n"
                    "Google Forms doesn't allow bots to upload files automatically.\n\n"
                    "I've pre-filled all other fields for you.\n"
                    "Just open this link, upload your file, and press Submit:\n\n"
                    f"🔗 *Pre-filled Form Link:*\n{prefilled}\n\n"
                    "You only need to: upload file → click Submit ✅",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("⏳ Submitting your form...")
                try:
                    success = submit_form(form_url, filled)
                    if success:
                        await update.message.reply_text(
                            f"✅ *Form submitted successfully!* 🎉\n\n"
                            f"📊 *{len(filled)}* fields filled\n\nSend another form link anytime!",
                            parse_mode="Markdown"
                        )
                    else:
                        await update.message.reply_text(
                            "⚠️ Submission may have failed.\n"
                            "The form might require a Google login. Try opening it manually."
                        )
                except Exception as e:
                    await update.message.reply_text(f"❌ Submit error: {e}")

        elif text.lower() in ("no", "n", "cancel", "nahi", "nope"):
            await update.message.reply_text(
                "❌ *Cancelled.* No form was submitted.\n\nSend a form link again whenever you're ready.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "Please reply *yes* to submit or *no* to cancel.", parse_mode="Markdown"
            )
            return
        ctx.user_data["mode"] = None
        return

    if "docs.google.com/forms" in text:
        await process_form(update, ctx, text)
        return

    await update.message.reply_text(
        "Paste a Google Form link, or use /setinfo to update your profile.\n/help for guide."
    )


async def process_form(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str):
    await update.message.reply_text("🔍 Scanning form questions...")
    try:
        questions = scrape_google_form(url)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not read form: {e}\n\n"
            "• Make sure the form is *public* (test in incognito tab)\n"
            "• Copy the URL directly from your browser address bar",
            parse_mode="Markdown"
        )
        return
    if not questions:
        await update.message.reply_text("❌ No fillable questions found.")
        return

    user_info = load_user_info()
    filled, unanswered, has_file_upload = auto_fill(questions, user_info)

    total_fillable = len([q for q in questions if q["type"] not in SKIP_TYPES and q["type"] != TYPE_FILE_UPLOAD and q["field_id"]])
    auto_titles = [q["title"] for q in questions if q.get("field_id") in filled]
    summary = ["📋 *Form Scan Complete:*\n",
               f"✅ *Auto-filled {len(filled)}/{total_fillable}:*"]
    for t in auto_titles:
        summary.append(f"   • {t}")
    if has_file_upload:
        summary.append("\n📎 *File upload field detected — will give you a pre-filled link*")
    if unanswered:
        summary.append(f"\n❓ *{len(unanswered)} question(s) need your input*")
    else:
        summary.append("\n🎯 *All fields filled!*")
    await update.message.reply_text("\n".join(summary), parse_mode="Markdown")

    ctx.user_data["all_questions"] = questions
    ctx.user_data["filled"] = filled
    ctx.user_data["form_url"] = url
    ctx.user_data["has_file_upload"] = has_file_upload

    if unanswered:
        ctx.user_data["mode"] = "bulk_answering"
        ctx.user_data["unanswered"] = unanswered
        await ask_all_at_once(update, unanswered)
    else:
        ctx.user_data["mode"] = "confirming"
        await update.message.reply_text(
            build_confirmation_message(filled, questions), parse_mode="Markdown"
        )


def main():
    import threading
    import http.server

    # Render requires a bound port — this dummy server satisfies that requirement
    def _dummy_server():
        port = int(os.getenv("PORT", 8080))
        handler = http.server.BaseHTTPRequestHandler

        # Silence the default request logs from the dummy server
        handler.log_message = lambda *args: None

        server = http.server.HTTPServer(("0.0.0.0", port), handler)
        print(f"🌐 Dummy HTTP server listening on port {port} (for Render)")
        server.serve_forever()

    threading.Thread(target=_dummy_server, daemon=True).start()

    print("🤖 Starting Google Form Filler Bot v2...")
    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("\n❌ Set TELEGRAM_BOT_TOKEN in your .env file first!")
        return
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setinfo", setinfo_cmd))
    app.add_handler(CommandHandler("myinfo", myinfo_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Bot running! Open Telegram and send /start")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
