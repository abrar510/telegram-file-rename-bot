
import os
import re
import asyncio
import logging
import tempfile
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from PIL import Image

# =========================================
# CONFIGURATION
# =========================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

# সাধারণ অ্যাকাউন্টের জন্য সর্বোচ্চ 2GB
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024

WORK_DIR = Path("/tmp/rename_bot")
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

client = TelegramClient(
    "rename_bot_session",
    API_ID,
    API_HASH
)

# প্রতি ইউজারের আলাদা কাজ
user_tasks = {}
user_locks = {}


# =========================================
# HELPERS
# =========================================

def get_lock(user_id):
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()

    return user_locks[user_id]


def safe_filename(name):
    name = Path(name).name

    # অনিরাপদ অক্ষর বাদ
    name = re.sub(r'[\\/:*?"<>|]', "_", name)

    name = name.strip().strip(".")

    if not name:
        name = "renamed_file"

    return name[:180]


def cleanup_task(user_id):
    data = user_tasks.pop(user_id, None)

    if not data:
        return

    for key in ("file_path", "thumb_path"):
        path = data.get(key)

        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logging.warning("Could not remove %s", path)


def convert_thumbnail(source, destination):
    """
    Telegram-এর জন্য JPEG thumbnail তৈরি করে।
    320x320-এর মধ্যে রাখা হয়।
    """

    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail((320, 320))

        image.save(
            destination,
            format="JPEG",
            quality=85,
            optimize=True
        )

    # Telegram thumbnail 200KB-এর কম রাখা
    if os.path.getsize(destination) > 200 * 1024:
        with Image.open(destination) as image:
            image.save(
                destination,
                format="JPEG",
                quality=65,
                optimize=True
            )


# =========================================
# START
# =========================================

@client.on(events.NewMessage(pattern=r"^/start$"))
async def start(event):

    if not event.is_private:
        return

    await event.reply(
        "👋 Welcome to File Rename Bot!\n\n"
        "📤 প্রথমে একটি ফাইল ফরোয়ার্ড করুন।\n"
        "✏️ এরপর নতুন ফাইলের নাম লিখুন।\n"
        "🖼️ তারপর নতুন Thumbnail পাঠান।\n\n"
        "Thumbnail দিতে না চাইলে /skip পাঠান।\n"
        "কাজ বাতিল করতে /cancel পাঠান।"
    )


# =========================================
# CANCEL
# =========================================

@client.on(events.NewMessage(pattern=r"^/cancel$"))
async def cancel(event):

    if not event.is_private:
        return

    user_id = event.sender_id

    cleanup_task(user_id)

    await event.reply(
        "❌ আপনার কাজ বাতিল করা হয়েছে।\n"
        "নতুন ফাইল পাঠিয়ে আবার শুরু করুন।"
    )


# =========================================
# RECEIVE FILE
# =========================================

@client.on(events.NewMessage(incoming=True))
async def receive_file(event):

    if not event.is_private:
        return

    if not event.file:
        return

    if event.sender_id is None:
        return

    user_id = event.sender_id

    # পুরনো কাজ থাকলে নতুন ফাইল গ্রহণ
    cleanup_task(user_id)

    file_size = event.file.size or 0

    if file_size > MAX_FILE_SIZE:
        await event.reply(
            "❌ ফাইলটি 2GB-এর বেশি।\n"
            "এই বটের বর্তমান সীমা অতিক্রম করেছে।"
        )
        return

    original_name = event.file.name or "file"

    original_name = safe_filename(original_name)

    file_path = WORK_DIR / (
        f"{user_id}_{event.id}_{original_name}"
    )

    await event.reply(
        "⏳ ফাইল গ্রহণ করা হয়েছে।\n"
        "ডাউনলোড শুরু হচ্ছে..."
    )

    try:

        downloaded = await event.download_media(
            file=str(file_path)
        )

        if not downloaded:
            await event.reply(
                "❌ ফাইল ডাউনলোড করা সম্ভব হয়নি।"
            )
            return

        user_tasks[user_id] = {
            "file_path": str(file_path),
            "original_name": original_name,
            "phase": "filename"
        }

        await event.reply(
            "✅ ফাইল গ্রহণ করা হয়েছে!\n\n"
            f"📁 বর্তমান নাম: {original_name}\n\n"
            "✏️ এখন নতুন ফাইলের নাম লিখে পাঠান।\n"
            "উদাহরণ: MyMovie.mkv"
        )

    except Exception:
        logging.exception("Download failed")

        cleanup_task(user_id)

        await event.reply(
            "❌ ফাইল ডাউনলোড ব্যর্থ হয়েছে।"
        )


# =========================================
# RECEIVE NEW NAME / THUMBNAIL
# =========================================

@client.on(events.NewMessage(incoming=True))
async def process_message(event):

    if not event.is_private:
        return

    if event.sender_id is None:
        return

    if event.file:
        return

    if not event.raw_text:
        return

    text = event.raw_text.strip()

    if text.startswith("/"):
        return

    user_id = event.sender_id

    if user_id not in user_tasks:
        return

    async with get_lock(user_id):

        data = user_tasks.get(user_id)

        if not data:
            return

        # -----------------------------
        # STEP 1: RECEIVE NEW FILE NAME
        # -----------------------------

        if data["phase"] == "filename":

            new_name = safe_filename(text)

            original_ext = Path(
                data["original_name"]
            ).suffix

            # Extension না থাকলে পুরনো extension রাখবে
            if not Path(new_name).suffix and original_ext:
                new_name += original_ext

            data["new_name"] = new_name
            data["phase"] = "thumbnail"

            await event.reply(
                "✅ নতুন নাম গ্রহণ করা হয়েছে!\n\n"
                f"📁 নতুন নাম: {new_name}\n\n"
                "🖼️ এখন নতুন Thumbnail ছবি পাঠান।\n"
                "Thumbnail না চাইলে /skip পাঠান।"
            )

            return

        # -----------------------------
        # STEP 2: SKIP THUMBNAIL
        # -----------------------------

        if data["phase"] == "thumbnail" and text == "/skip":

            await send_renamed_file(event, user_id)

            return


# =========================================
# RECEIVE THUMBNAIL PHOTO
# =========================================

@client.on(events.NewMessage(incoming=True))
async def receive_thumbnail(event):

    if not event.is_private:
        return

    if event.sender_id is None:
        return

    if not event.photo:
        return

    user_id = event.sender_id

    if user_id not in user_tasks:
        return

    async with get_lock(user_id):

        data = user_tasks.get(user_id)

        if not data or data["phase"] != "thumbnail":
            return

        source_path = WORK_DIR / (
            f"{user_id}_{event.id}_source.jpg"
        )

        thumb_path = WORK_DIR / (
            f"{user_id}_{event.id}_thumb.jpg"
        )

        try:

            await event.reply(
                "⏳ Thumbnail গ্রহণ করা হচ্ছে..."
            )

            await event.download_media(
                file=str(source_path)
            )

            convert_thumbnail(
                str(source_path),
                str(thumb_path)
            )

            data["thumb_path"] = str(thumb_path)

            if source_path.exists():
                source_path.unlink()

            await send_renamed_file(event, user_id)

        except Exception:
            logging.exception("Thumbnail processing failed")

            if source_path.exists():
                source_path.unlink()

            if thumb_path.exists():
                thumb_path.unlink()

            await event.reply(
                "❌ Thumbnail প্রসেস করা সম্ভব হয়নি।\n"
                "আবার একটি ছবি পাঠান অথবা /skip লিখুন।"
            )


# =========================================
# SEND RENAMED FILE
# =========================================

async def send_renamed_file(event, user_id):

    data = user_tasks.get(user_id)

    if not data:
        return

    file_path = Path(data["file_path"])

    new_name = data["new_name"]

    thumb_path = data.get("thumb_path")

    new_path = WORK_DIR / (
        f"{user_id}_renamed_{new_name}"
    )

    try:

        os.rename(file_path, new_path)

        await event.reply(
            "⏳ ফাইলের নাম পরিবর্তন করা হয়েছে।\n"
            "এখন ফাইল পাঠানো হচ্ছে..."
        )

        await client.send_file(
            event.chat_id,
            str(new_path),
            force_document=True,
            file_name=new_name,
            thumb=thumb_path,
            caption=(
                "✅ File Renamed Successfully!\n\n"
                f"📁 Name: {new_name}\n"
                "🖼️ Custom Thumbnail Applied"
                if thumb_path
                else
                "✅ File Renamed Successfully!\n\n"
                f"📁 Name: {new_name}"
            )
        )

        await event.reply(
            "🎉 কাজ সম্পন্ন!\n"
            "আবার নতুন ফাইল পাঠাতে পারেন।"
        )

    except FloodWaitError as e:

        await event.reply(
            "⏳ Telegram-এর জন্য অপেক্ষা করতে হবে: "
            f"{e.seconds} সেকেন্ড।"
        )

    except Exception:

        logging.exception("File sending failed")

        await event.reply(
            "❌ ফাইল পাঠানো সম্ভব হয়নি।\n"
            "ফাইলের সাইজ অথবা Telegram সীমা পরীক্ষা করুন।"
        )

    finally:

        cleanup_task(user_id)

        if new_path.exists():
            try:
                new_path.unlink()
            except OSError:
                pass


# =========================================
# RUN
# =========================================

async def main():

    logging.info("Starting File Rename Bot")

    await client.start(
        bot_token=BOT_TOKEN
    )

    logging.info("Bot started successfully")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
