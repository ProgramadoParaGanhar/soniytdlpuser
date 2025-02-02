import logging
import os
import asyncio
import random
import hashlib
from urllib.parse import urlparse
from typing import Optional

from telegram import Update, InputFile, Bot, InputMediaDocument, InputMediaAudio
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

# Configuração do logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)
file_handler = logging.FileHandler('bot.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

# Constantes da API
MAX_PREMIUM_PARTS = 4000  # Valor hipotético, ajustar conforme config
MAX_REGULAR_PARTS = 2000  # Valor hipotético, ajustar conforme config
PART_SIZE = 524288  # 512KB
BIG_FILE_THRESHOLD = 10 * 1024 * 1024  # 10MB

# Configurações
TOKEN = os.getenv("TELEGRAM_TOKEN")  # Mantenha assim, mas confira o nome da variável
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 2 * 1024 * 1024 * 1024))  # 2GB
FFMPEG_PATH = os.getenv("FFMPEG_PATH")

if not TOKEN:
    logger.error("Telegram bot token not configured.")
    exit()

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join("/tmp", "downloads"))  # Usando /tmp

TIMEOUT_SECONDS = 1000  # 16 minutos

class UploadError(Exception):
    pass

def is_valid_url(url: str) -> bool:
    """Valida uma URL."""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False

def create_user_download_dir(user_id: int) -> str:
    """Cria um diretório de download específico para o usuário."""
    user_dir = os.path.join(DOWNLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    return user_dir

def compute_md5(file_path: str) -> str:
    """Calcula o hash MD5 do arquivo."""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

async def upload_file_part(
        bot: Bot,
        file_id: int,
        part_index: int,
        bytes_data: bytes,
        total_parts: int,
        is_premium: bool
) -> bool:
    """Faz o upload de uma parte do arquivo."""
    try:
        if total_parts > (MAX_PREMIUM_PARTS if is_premium else MAX_REGULAR_PARTS):
            raise UploadError("Limite de partes excedido para o tipo de conta")

        if len(bytes_data) > PART_SIZE and part_index != total_parts - 1:
            raise UploadError("Tamanho da parte excedeu 512KB")

        if len(bytes_data) == 0:
            raise UploadError("Parte do arquivo vazia")

        if total_parts > 1:
            await bot.send(
                method="upload.saveBigFilePart" if total_parts * PART_SIZE > BIG_FILE_THRESHOLD else "upload.saveFilePart",
                data={
                    "file_id": file_id,
                    "file_part": part_index,
                    "file_total_parts": total_parts,
                    "bytes": bytes_data
                }
            )
        return True
    except Exception as e:
        error_msg = str(e).lower()
        if "flood_premium_wait" in error_msg:
            raise UploadError("Limite de upload atingido para conta regular") from e
        logger.error(f"Erro no upload da parte {part_index}: {str(e)}")
        return False

async def upload_large_file(file_path: str, update: Update, context: ContextTypes.DEFAULT_TYPE, is_audio: bool) -> bool:
    """Faz o upload de arquivos grandes usando a API do Telegram."""
    file_size = os.path.getsize(file_path)
    total_parts = (file_size + PART_SIZE - 1) // PART_SIZE
    file_id = random.getrandbits(64)
    use_big_file = file_size > BIG_FILE_THRESHOLD
    md5_checksum = compute_md5(file_path) if not use_big_file else None
    is_premium = False  # Implementar lógica de verificação de Premium

    logger.info(f"Iniciando upload de {file_path} ({total_parts} partes)")

    try:
        # Upload paralelo com até 4 partes simultâneas
        semaphore = asyncio.Semaphore(4)

        async def upload_part(part_index: int):
            async with semaphore:
                with open(file_path, 'rb') as f:
                    f.seek(part_index * PART_SIZE)
                    data = f.read(PART_SIZE)

                for attempt in range(3):
                    success = await upload_file_part(
                        context.bot,
                        file_id,
                        part_index,
                        data,
                        total_parts,
                        is_premium
                    )
                    if success:
                        return
                    await asyncio.sleep(2 ** attempt)
                raise UploadError(f"Falha no upload da parte {part_index}")

        tasks = [upload_part(i) for i in range(total_parts)]
        await asyncio.gather(*tasks)

        # Constrói o objeto InputFile correto
        input_file = {
            "_": "InputFileBig" if use_big_file else "InputFile",
            "id": file_id,
            "parts": total_parts,
            "name": os.path.basename(file_path)
        }
        if not use_big_file:
            input_file["md5_checksum"] = md5_checksum

        # Envia a mídia usando o arquivo carregado
        media_args = {
            "media": InputMediaDocument(media=input_file) if not is_audio else InputMediaAudio(media=input_file),
            "chat_id": update.message.chat_id,
            "caption": "✅ Download concluído!"
        }

        await context.bot.send_media_group(**media_args)
        return True

    except UploadError as e:
        logger.error(f"Erro no upload: {str(e)}")
        await update.message.reply_text(f"❌ Erro no upload: {str(e)}")
        return False
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def send_media(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str, is_audio: bool) -> bool:
    """Gerencia o envio de mídia com fallback para upload tradicional."""
    file_size = os.path.getsize(file_path)

    if file_size > MAX_FILE_SIZE:
        await update.message.reply_text("⚠️ Arquivo excede o tamanho máximo permitido")
        return False

    try:
        if file_size > 50 * 1024 * 1024:  # 50MB
            return await upload_large_file(file_path, update, context, is_audio)

        with open(file_path, 'rb') as file:
            if is_audio:
                await context.bot.send_audio(
                    chat_id=update.message.chat_id,
                    audio=file,
                    title=os.path.basename(file_path)
                )
            else:
                await context.bot.send_video(
                    chat_id=update.message.chat_id,
                    video=file,
                    caption="✅ Download concluído!",
                    supports_streaming=True
                )
        return True
    except Exception as e:
        logger.error(f"Erro no envio: {str(e)}")
        await update.message.reply_text(f"❌ Erro no envio: {str(e)}")
        return False
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def download_media(url: str, user_id: int, audio_only: bool = False) -> str:
    """Faz o download de mídia usando yt-dlp."""
    user_dir = create_user_download_dir(user_id)
    ydl_opts = {
        'ffmpeg_location': FFMPEG_PATH,
        'outtmpl': os.path.join(user_dir, '%(title)s.%(ext)s'),
        'restrictfilenames': True,
        'max_filesize': MAX_FILE_SIZE,
        'cookiefile': os.getenv("COOKIES_PATH"),
        'user_agent': os.getenv("USER_AGENT"),
        'nocheckcertificate': True,
        'format': 'bestaudio/best' if audio_only else 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192'
        }] if audio_only else []
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            filename = ydl.prepare_filename(info)
            return filename if os.path.exists(filename) else None
    except Exception as e:
        logger.error(f"Erro no download: {str(e)}")
        raise

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, is_audio: bool):
    """Processa uma URL recebida."""
    user = update.message.from_user
    try:
        await update.message.reply_text("⏳ Processando seu pedido...")
        file_path = await download_media(url, user.id, is_audio)

        if not file_path:
            raise ValueError("Falha no download do arquivo")

        if not await send_media(update, context, file_path, is_audio):
            await update.message.reply_text("❌ Falha ao enviar o arquivo")

    except Exception as e:
        logger.error(f"Erro geral: {str(e)}")
        await update.message.reply_text(f"❌ Erro: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para /start."""
    await update.message.reply_text(
        "🎬 YouTube Download Bot\n"
        "Envie um link ou use /audio para MP3"
    )

async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para /audio."""
    url = ' '.join(context.args)
    if not is_valid_url(url):
        await update.message.reply_text("⚠️ URL inválida")
        return
    await handle_url(update, context, url, True)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para mensagens com URLs."""
    url = update.message.text
    if not is_valid_url(url):
        await update.message.reply_text("⚠️ URL inválida")
        return
    await handle_url(update, context, url, False)

def main():
    """Inicializa o bot."""
    request = HTTPXRequest(connect_timeout=15, read_timeout=TIMEOUT_SECONDS)
    bot = Bot(token=TOKEN, request=request)
    app = Application.builder().bot(bot).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("audio", audio_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Bot iniciado com sucesso!")
    app.run_polling()

if __name__ == "__main__":
    main()