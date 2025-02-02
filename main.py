import logging
import os
import asyncio
import hashlib
from urllib.parse import urlparse
from typing import Optional

from telegram import Update, Bot
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

# Configurações
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 2 * 1024 * 1024 * 1024))  # 2GB
TIMEOUT_SECONDS = 1000  # 16 minutos

if not TOKEN:
    logger.error("Telegram bot token not configured.")
    exit()

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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

def create_progress_hook(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """
    Retorna uma função progress_hook que atualiza a mensagem do Telegram
    com informações de progresso do download.
    """
    def progress_hook(progress: dict):
        try:
            if progress.get('status') == 'downloading':
                downloaded = progress.get('downloaded_bytes', 0)
                total = progress.get('total_bytes', 1)  # evita divisão por zero
                percent = downloaded / total * 100
                eta = progress.get('eta', 0)
                text = (
                    f"⏳ **Baixando...**\n"
                    f"Progresso: {percent:.2f}%\n"
                    f"{downloaded / 1024:.2f} KB de {total / 1024:.2f} KB\n"
                    f"ETA: {eta} s"
                )
                # Atualiza a mensagem de forma segura no loop assíncrono
                asyncio.run_coroutine_threadsafe(
                    context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        parse_mode="Markdown"
                    ),
                    context.application.loop
                )
            elif progress.get('status') == 'finished':
                text = "✅ Download concluído!"
                asyncio.run_coroutine_threadsafe(
                    context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text
                    ),
                    context.application.loop
                )
        except Exception as e:
            logger.error(f"Erro no progress hook: {e}")
    return progress_hook

async def download_media(url: str, user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int, is_audio: bool = False) -> Optional[str]:
    """Faz o download de mídia usando yt-dlp com atualização de progresso."""
    user_dir = create_user_download_dir(user_id)

    # Envia uma mensagem inicial e guarda o message_id para atualização
    progress_message = await context.bot.send_message(chat_id=chat_id, text="⏳ Iniciando download...")
    message_id = progress_message.message_id

    # Cria o hook de progresso
    progress_hook = create_progress_hook(context, chat_id, message_id)

    ydl_opts = {
        'ffmpeg_location': FFMPEG_PATH,
        'outtmpl': os.path.join(user_dir, '%(title)s.%(ext)s'),
        'restrictfilenames': True,
        'max_filesize': MAX_FILE_SIZE,
        'cookiefile': os.getenv("COOKIES_PATH"),
        'user_agent': os.getenv("USER_AGENT"),
        'nocheckcertificate': True,
        'format': 'bestaudio/best' if is_audio else 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
        'progress_hooks': [progress_hook],
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192'
        }] if is_audio else []
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=True)
            filename = ydl.prepare_filename(info)
            return filename if os.path.exists(filename) else None
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"❌ Erro no download: {str(e)}"
        )
        logger.error(f"Erro no download: {e}")
        raise

async def send_media(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str, is_audio: bool) -> bool:
    """Envia a mídia para o usuário utilizando os métodos padrão da API do Telegram."""
    file_size = os.path.getsize(file_path)
    if file_size > MAX_FILE_SIZE:
        await update.message.reply_text("⚠️ Arquivo excede o tamanho máximo permitido")
        if os.path.exists(file_path):
            os.remove(file_path)
        return False

    try:
        with open(file_path, 'rb') as file:
            if is_audio:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=file,
                    title=os.path.basename(file_path)
                )
            else:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=file,
                    caption="✅ Download concluído!",
                    supports_streaming=True
                )
        return True
    except Exception as e:
        logger.error(f"Erro no envio: {e}")
        await update.message.reply_text(f"❌ Erro no envio: {e}")
        return False
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, is_audio: bool):
    """Processa uma URL recebida, realizando o download com feedback de progresso e enviando a mídia."""
    user = update.message.from_user
    chat_id = update.effective_chat.id
    try:
        await update.message.reply_text("⏳ Processando seu pedido...")
        file_path = await download_media(url, user.id, context, chat_id, is_audio)

        if not file_path:
            raise ValueError("Falha no download do arquivo")

        if not await send_media(update, context, file_path, is_audio):
            await update.message.reply_text("❌ Falha ao enviar o arquivo")
    except Exception as e:
        logger.error(f"Erro geral: {e}")
        await update.message.reply_text(f"❌ Erro: {e}")

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
    """Handler para mensagens contendo URLs."""
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
