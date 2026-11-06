
import os
import re
import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from PIL import Image

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024

WORK_DIR = Path("/tmp/rename_bot")
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)

client = TelegramClient(
    "rename_bot_session",
    API_ID,
    API_HASH
)

user_tasks = {}
user_locks = {}


def get_lock(user_id):
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]


def safe_filename(name):
    name = Path(name).name
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip().strip(".")

    if not name:
        name = "renamed_file"

    return name[:180]


def cleanup(user_id):
    data = user_tasks.pop(user_id, None)

    if not data:
        return

    for key in ("file_path", "thumb_path"):
        path = data.get(key)

        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def make_thumbnail(source, destination):
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail((320, 320))

        image.save(
            destination,
            "JPEG",
            quality=85,
            optimize=True
        )

    if os.path.getsize(destination) > 200 * 1024:
        with Image.open(destination) as image:
            image.save(
                destination,
                "JPEG",
                quality=60,
                optimize=True
            )


async def send_file_back(event, user_id):

    data = user_tasks.get(user_id)

    if not data:
        return

    old_path = Path(data["file_path"])
    new_name = data["new_name"]

    new_path = WORK_DIR / (
        f"{user_id}_renamed_{new_name}"
    )

    thumb_path = data.get("thumb_path")

    try:

        os.rename(old_path, new_path)

        await event.reply(
            "⏳ ফাইল Rename করা হয়েছে।\n"
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
                f"📁 Name: {new_name}"
            )
        )

        await event.reply(
            "🎉 সম্পন্ন!\n"
            "নতুন ফাইল পাঠিয়ে আবার Rename করতে পারবেন।"
        )

    except FloodWaitError as e:

        await event.reply(
            f"⏳ Telegram অপেক্ষা করতে বলেছে: "
            f"{e.seconds} সেকেন্ড।"
        )

    except Exception:

        logging.exception("File sending failed")

        await event.reply(
            "❌ ফাইল পাঠানো ব্যর্থ হয়েছে।\n"
            "ফাইলের সাইজ এবং Telegram সীমা পরীক্ষা করুন।"
        )

    finally:

        cleanup(user_id)

        if new_path.exists():
            try:
                new_path.unlink()
            except OSError:
                pass


@client.on(events.NewMessage(incoming=True))
async def handle_message(event):

    if not event.is_private:
        return

    user_id = event.sender_id

    if user_id is None:
        return

    text = (event.raw_text or "").strip()

    # START
    if text == "/start":

        await event.reply(
            "👋 Welcome to File Rename Bot!\n\n"
            "📤 প্রথমে ফাইল ফরোয়ার্ড করুন।\n"
            "✏️ এরপর নতুন নাম লিখুন।\n"
            "🖼️ তারপর Thumbnail পাঠান।\n\n"
            "Thumbnail বাদ দিতে /skip লিখুন।\n"
            "কাজ বাতিল করতে /cancel লিখুন।"
        )

        return

    # CANCEL
    if text == "/cancel":

        cleanup(user_id)

        await event.reply(
            "❌ কাজ বাতিল করা হয়েছে।"
        )

        return

    async with get_lock(user_id):

        data = user_tasks.get(user_id)

        # =================================
        # RECEIVE ORIGINAL FILE
        # =================================

        if event.file and not event.photo:

            cleanup(user_id)

            file_size = event.file.size or 0

            if file_size > MAX_FILE_SIZE:

                await event.reply(
                    "❌ ফাইলটি 2GB-এর বেশি।\n"
                    "বর্তমান সীমা অতিক্রম করেছে।"
                )

                return

            original_name = safe_filename(
                event.file.name or "file"
            )

            file_path = WORK_DIR / (
                f"{user_id}_{event.id}_{original_name}"
            )

            await event.reply(
                "⏳ ফাইল গ্রহণ করা হয়েছে।\n"
                "ডাউনলোড হচ্ছে..."
            )

            try:

                downloaded = await event.download_media(
                    file=str(file_path)
                )

                if not downloaded:
                    await event.reply(
                        "❌ ফাইল ডাউনলোড ব্যর্থ হয়েছে।"
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
                    "✏️ নতুন ফাইলের নাম লিখুন।"
                )

            except Exception:

                logging.exception("Download failed")

                cleanup(user_id)

                await event.reply(
                    "❌ ফাইল ডাউনলোড করা সম্ভব হয়নি।"
                )

            return

        # =================================
        # RECEIVE NEW FILE NAME
        # =================================

        if data and data["phase"] == "filename":

            if not text or text.startswith("/"):
                return

            new_name = safe_filename(text)

            original_ext = Path(
                data["original_name"]
            ).suffix

            if not Path(new_name).suffix and original_ext:
                new_name += original_ext

            data["new_name"] = new_name
            data["phase"] = "thumbnail"

            await event.reply(
                "✅ নতুন নাম গ্রহণ করা হয়েছে!\n\n"
                f"📁 নতুন নাম: {new_name}\n\n"
                "🖼️ এখন Thumbnail ছবি পাঠান।\n"
                "না চাইলে /skip লিখুন।"
            )

            return

        # =================================
        # SKIP THUMBNAIL
        # =================================

        if data and data["phase"] == "thumbnail":

            if text == "/skip":

                await send_file_back(
                    event,
                    user_id
                )

            return

        # =================================
        # RECEIVE THUMBNAIL
        # =================================

        if (
            data
            and data["phase"] == "thumbnail"
            and event.photo
        ):

            source_path = WORK_DIR / (
                f"{user_id}_{event.id}_source.jpg"
            )

            thumb_path = WORK_DIR / (
                f"{user_id}_{event.id}_thumb.jpg"
            )

            try:

                await event.reply(
                    "⏳ Thumbnail প্রসেস করা হচ্ছে..."
                )

                await event.download_media(
                    file=str(source_path)
                )

                make_thumbnail(
                    str(source_path),
                    str(thumb_path)
                )

                data["thumb_path"] = str(thumb_path)

                if source_path.exists():
                    source_path.unlink()

                await send_file_back(
                    event,
                    user_id
                )

            except Exception:

                logging.exception(
                    "Thumbnail processing failed"
                )

                if source_path.exists():
                    source_path.unlink()

                if thumb_path.exists():
                    thumb_path.unlink()

                await event.reply(
                    "❌ Thumbnail তৈরি করা যায়নি।\n"
                    "আবার ছবি পাঠান অথবা /skip লিখুন।"
                )

            return


async def main():

    logging.info("Starting Rename Bot")

    await client.start(
        bot_token=BOT_TOKEN
    )

    logging.info("Bot started successfully")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
