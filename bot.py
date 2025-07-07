from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from aiocache import Cache
from fastapi import FastAPI
import uvicorn
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputFile,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)
import yt_dlp as youtube_dl

sys.modules["youtube_dl"] = youtube_dl  # noqa: E402
from savify import Savify  # noqa: E402
from savify.types import Format, Quality  # noqa: E402
from savify.utils import PathHolder, safe_path_string  # noqa: E402
from savify.track import Track  # noqa: E402
from savify.savify import _sort_dir  # noqa: E402
import spotipy  # noqa: E402
from spotipy.oauth2 import SpotifyClientCredentials  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

SPOTIFY_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

SEARCH_CACHE = Cache(Cache.MEMORY, ttl=300)  # 5 minutes
DOWNLOAD_CACHE = Cache(Cache.MEMORY, ttl=86400)  # 24 hours

DOWNLOAD_DIR = Path("/app/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

QUEUE_MAX_SIZE = 3
queue: asyncio.LifoQueue[Dict[str, Any]] = asyncio.LifoQueue(maxsize=QUEUE_MAX_SIZE)

APPLICATION: Application[Any, Any, Any, Any, Any, Any] | None = None

# language preferences
LANG_FILE = Path("/app/langs.json")
LANGUAGES: Dict[str, str] = {
    "en": "\U0001F1FA\U0001F1F8 English",
    "es": "\U0001F1EA\U0001F1F8 Español",
    "de": "\U0001F1E9\U0001F1EA Deutsch",
    "fr": "\U0001F1EB\U0001F1F7 Français",
    "it": "\U0001F1EE\U0001F1F9 Italiano",
    "pt": "\U0001F1F5\U0001F1F9 Português",
    "ru": "\U0001F1F7\U0001F1FA Русский",
    "uk": "\U0001F1FA\U0001F1E6 Українська",
    "zh": "\U0001F1E8\U0001F1F3 中文",
    "ja": "\U0001F1EF\U0001F1F5 日本語",
    "ko": "\U0001F1F0\U0001F1F7 한국어",
    "ar": "\U0001F1F8\U0001F1E6 العربية",
    "tr": "\U0001F1F9\U0001F1F7 Türkçe",
    "hi": "\U0001F1EE\U0001F1F3 हिंदी",
    "bn": "\U0001F1E7\U0001F1E9 বাংলা",
}
USER_LANGS: Dict[str, str] = {}

# user-facing messages in multiple languages
MESSAGES: Dict[str, Dict[str, str]] = {
    "choose_language": {
        "en": "Please choose your language:",
        "es": "Por favor, selecciona tu idioma:",
        "de": "Bitte wähle deine Sprache:",
        "fr": "Veuillez choisir votre langue :",
        "it": "Seleziona la tua lingua:",
        "pt": "Por favor, escolha seu idioma:",
        "ru": "Пожалуйста, выберите язык:",
        "uk": "Будь ласка, оберіть мову:",
        "zh": "请选择你的语言：",
        "ja": "言語を選択してください:",
        "ko": "언어를 선택하세요:",
        "ar": "يرجى اختيار لغتك:",
        "tr": "Lütfen dilinizi seçin:",
        "hi": "कृपया अपनी भाषा चुनें:",
        "bn": "আপনার ভাষা নির্বাচন করুন:",
    },
    "choose_track": {
        "en": "Choose a track:",
        "es": "Elige una pista:",
        "de": "Wähle einen Titel:",
        "fr": "Choisissez une piste :",
        "it": "Scegli una traccia:",
        "pt": "Escolha uma faixa:",
        "ru": "Выберите трек:",
        "uk": "Виберіть трек:",
        "zh": "选择一个曲目：",
        "ja": "トラックを選択してください:",
        "ko": "트랙을 선택하세요:",
        "ar": "اختر مسارًا:",
        "tr": "Bir parça seçin:",
        "hi": "एक ट्रैक चुनें:",
        "bn": "একটি ট্র্যাক চয়ন করুন:",
    },
    "download_started": {
        "en": "Download started, please wait....",
        "es": "Descarga iniciada, espera por favor....",
        "de": "Download gestartet, bitte warten...",
        "fr": "Téléchargement lancé, veuillez patienter...",
        "it": "Download avviato, attendere...",
        "pt": "Download iniciado, aguarde...",
        "ru": "Загрузка началась, пожалуйста, подождите...",
        "uk": "Завантаження розпочато, зачекайте...",
        "zh": "开始下载，请稍候...",
        "ja": "ダウンロードを開始しました。しばらくお待ちください...",
        "ko": "다운로드를 시작합니다. 잠시만 기다려주세요...",
        "ar": "تم بدء التنزيل، يرجى الانتظار...",
        "tr": "İndirme başlatıldı, lütfen bekleyin...",
        "hi": "डाउनलोड शुरू हुआ, कृपया प्रतीक्षा करें...",
        "bn": "ডাউনলোড শুরু হয়েছে, অনুগ্রহ করে অপেক্ষা করুন...",
    },
    "language_saved": {
        "en": "Language saved! Send me a song name or Spotify link.",
        "es": "¡Idioma guardado! Envíame un nombre de canción o enlace de Spotify.",
        "de": "Sprache gespeichert! Sende mir einen Songnamen oder Spotify-Link.",
        "fr": "Langue enregistrée ! Envoyez-moi un nom de chanson ou un lien Spotify.",
        "it": "Lingua salvata! Inviami il nome di un brano o un link Spotify.",
        "pt": "Idioma salvo! Envie um nome de música ou link do Spotify.",
        "ru": "Язык сохранён! Отправьте название песни или ссылку Spotify.",
        "uk": "Мову збережено! Надішліть назву пісні або посилання Spotify.",
        "zh": "语言已保存！发送歌曲名称或 Spotify 链接。",
        "ja": "言語を保存しました！曲名または Spotify リンクを送ってください。",
        "ko": "언어가 저장되었습니다! 노래 제목 또는 Spotify 링크를 보내주세요.",
        "ar": "تم حفظ اللغة! أرسل لي اسم أغنية أو رابط سبوتيفاي.",
        "tr": "Dil kaydedildi! Bana bir şarkı adı veya Spotify bağlantısı gönder.",
        "hi": "भाषा सहेजी गई! मुझे गीत का नाम या Spotify लिंक भेजें।",
        "bn": "ভাষা সংরক্ষণ হয়েছে! আমাকে একটি গান নাম বা Spotify লিঙ্ক পাঠান।",
    },
    "send_song": {
        "en": "Send me a song name or Spotify link.",
        "es": "Envíame un nombre de canción o enlace de Spotify.",
        "de": "Sende mir einen Songnamen oder Spotify-Link.",
        "fr": "Envoyez-moi un nom de chanson ou un lien Spotify.",
        "it": "Inviami il nome di un brano o un link Spotify.",
        "pt": "Envie um nome de música ou link do Spotify.",
        "ru": "Отправьте название песни или ссылку Spotify.",
        "uk": "Надішліть назву пісні або посилання Spotify.",
        "zh": "发送歌曲名称或 Spotify 链接。",
        "ja": "曲名または Spotify リンクを送ってください。",
        "ko": "노래 제목 또는 Spotify 링크를 보내주세요.",
        "ar": "أرسل لي اسم أغنية أو رابط سبوتيفاي.",
        "tr": "Bana bir şarkı adı veya Spotify bağlantısı gönder.",
        "hi": "मुझे गीत का नाम या Spotify लिंक भेजें।",
        "bn": "আমাকে একটি গান নাম বা Spotify লিঙ্ক পাঠান।",
    },
    "too_many_downloads": {
        "en": "Too many downloads in progress, please try later.",
        "es": "Demasiadas descargas en progreso, inténtalo más tarde.",
        "de": "Zu viele Downloads laufen, bitte später versuchen.",
        "fr": "Trop de téléchargements en cours, réessayez plus tard.",
        "it": "Troppi download in corso, riprova più tardi.",
        "pt": "Muitos downloads em andamento, tente mais tarde.",
        "ru": "Слишком много загрузок, попробуйте позже.",
        "uk": "Забагато завантажень, спробуйте пізніше.",
        "zh": "下载过多，请稍后再试。",
        "ja": "ダウンロードが多すぎます。後でお試しください。",
        "ko": "다운로드가 너무 많습니다. 나중에 다시 시도하세요.",
        "ar": "عمليات تنزيل كثيرة جدًا، حاول لاحقًا.",
        "tr": "Çok fazla indirme işlemi var, lütfen daha sonra deneyin.",
        "hi": "बहुत अधिक डाउनलोड प्रगति पर हैं, बाद में प्रयास करें।",
        "bn": "অনেকগুলি ডাউনলোড চলছে, পরে চেষ্টা করুন।",
    },
    "download_failed": {
        "en": "Download failed.",
        "es": "La descarga falló.",
        "de": "Download fehlgeschlagen.",
        "fr": "Échec du téléchargement.",
        "it": "Download fallito.",
        "pt": "Falha no download.",
        "ru": "Ошибка загрузки.",
        "uk": "Помилка завантаження.",
        "zh": "下载失败。",
        "ja": "ダウンロードに失敗しました。",
        "ko": "다운로드 실패.",
        "ar": "فشل التنزيل.",
        "tr": "İndirme başarısız oldu.",
        "hi": "डाउनलोड विफल हुआ।",
        "bn": "ডাউনলোড ব্যর্থ হয়েছে।",
    },
    "download_button": {
        "en": "Download \U0001F53D",
        "es": "Descargar \U0001F53D",
        "de": "Herunterladen \U0001F53D",
        "fr": "Télécharger \U0001F53D",
        "it": "Scarica \U0001F53D",
        "pt": "Baixar \U0001F53D",
        "ru": "Скачать \U0001F53D",
        "uk": "Завантажити \U0001F53D",
        "zh": "下载 \U0001F53D",
        "ja": "ダウンロード \U0001F53D",
        "ko": "다운로드 \U0001F53D",
        "ar": "تنزيل \U0001F53D",
        "tr": "İndir \U0001F53D",
        "hi": "डाउनलोड \U0001F53D",
        "bn": "ডাউনলোড \U0001F53D",
    },
    "share": {
        "en": "Share: {link}",
        "es": "Compartir: {link}",
        "de": "Teilen: {link}",
        "fr": "Partager : {link}",
        "it": "Condividi: {link}",
        "pt": "Compartilhar: {link}",
        "ru": "Поделиться: {link}",
        "uk": "Поділитись: {link}",
        "zh": "分享：{link}",
        "ja": "共有: {link}",
        "ko": "공유: {link}",
        "ar": "مشاركة: {link}",
        "tr": "Paylaş: {link}",
        "hi": "साझा करें: {link}",
        "bn": "শেয়ার করুন: {link}",
    },
    "menu": {
        "en": "Please choose an option:",
        "es": "Por favor, elige una opción:",
        "de": "Bitte wähle eine Option:",
        "fr": "Veuillez choisir une option :",
        "it": "Scegli un'opzione:",
        "pt": "Por favor, escolha uma opção:",
        "ru": "Пожалуйста, выберите опцию:",
        "uk": "Будь ласка, оберіть опцію:",
        "zh": "请选择一个选项：",
        "ja": "オプションを選択してください:",
        "ko": "옵션을 선택하세요:",
        "ar": "يرجى اختيار خيار:",
        "tr": "Lütfen bir seçenek seçin:",
        "hi": "कृपया एक विकल्प चुनें:",
        "bn": "একটি বিকল্প বেছে নিন:",
    },
    "menu_button": {
        "en": "Menu",
        "es": "Menú",
        "de": "Menü",
        "fr": "Menu",
        "it": "Menu",
        "pt": "Menu",
        "ru": "Меню",
        "uk": "Меню",
        "zh": "菜单",
        "ja": "メニュー",
        "ko": "메뉴",
        "ar": "القائمة",
        "tr": "Menü",
        "hi": "मेनू",
        "bn": "মেনু",
    },
    "change_language": {
        "en": "Change language",
        "es": "Cambiar idioma",
        "de": "Sprache ändern",
        "fr": "Changer de langue",
        "it": "Cambia lingua",
        "pt": "Mudar idioma",
        "ru": "Сменить язык",
        "uk": "Змінити мову",
        "zh": "更改语言",
        "ja": "言語を変更",
        "ko": "언어 변경",
        "ar": "تغيير اللغة",
        "tr": "Dili değiştir",
        "hi": "भाषा बदलें",
        "bn": "ভাষা পরিবর্তন করুন",
    },
    "donate": {
        "en": "Donate",
        "es": "Donar",
        "de": "Spenden",
        "fr": "Faire un don",
        "it": "Dona",
        "pt": "Doar",
        "ru": "Поддержать",
        "uk": "Підтримати",
        "zh": "捐赠",
        "ja": "寄付する",
        "ko": "기부하기",
        "ar": "تبرع",
        "tr": "Bağış yap",
        "hi": "दान करें",
        "bn": "দান করুন",
    },
    "donation_info": {
        "en": "Thanks for supporting!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nDonaters:\n{donaters}\n\nNickname can be sent in comment for transfer.",
        "es": "¡Gracias por apoyar!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nDonadores:\n{donaters}\n\nEl apodo puede enviarse en el comentario de la transferencia.",
        "de": "Vielen Dank für die Unterstützung!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nSpender:\n{donaters}\n\nNickname kann im Überweisungs-Kommentar angegeben werden.",
        "fr": "Merci pour votre soutien !\n\nСбер : 2202 2068 1567 7914\nPayPal : azgd0@outlook.com\n\nDonateurs :\n{donaters}\n\nLe pseudo peut être envoyé dans le commentaire du transfert.",
        "it": "Grazie per il supporto!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nDonatori:\n{donaters}\n\nIl nickname può essere inviato nel commento al trasferimento.",
        "pt": "Obrigado pelo apoio!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nDoadores:\n{donaters}\n\nO apelido pode ser enviado no comentário da transferência.",
        "ru": "Спасибо за поддержку!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nСпонсоры:\n{donaters}\n\nНикнейм можно указать в комментарии к переводу.",
        "uk": "Дякуємо за підтримку!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nДонатори:\n{donaters}\n\nНікнейм можна надіслати в коментарі до переказу.",
        "zh": "感谢您的支持！\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\n捐赠者：\n{donaters}\n\n昵称可以在转账备注中填写。",
        "ja": "ご支援ありがとうございます！\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\n寄付者:\n{donaters}\n\nニックネームは振込のコメントで送れます。",
        "ko": "후원해 주셔서 감사합니다!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\n기부자:\n{donaters}\n\n닉네임은 송금 메모로 보낼 수 있습니다.",
        "ar": "شكرًا لدعمك!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nالمتبرعون:\n{donaters}\n\nيمكن إرسال الاسم المستعار في تعليق التحويل.",
        "tr": "Destek olduğunuz için teşekkürler!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nBağışçılar:\n{donaters}\n\nTakma ad transfer açıklamasında belirtilebilir.",
        "hi": "सहयोग के लिए धन्यवाद!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nदानदाता:\n{donaters}\n\nस्थानांतरण की टिप्पणी में उपनाम भेजा जा सकता है.",
        "bn": "সমর্থনের জন্য ধন্যবাদ!\n\nСбер: 2202 2068 1567 7914\nPayPal: azgd0@outlook.com\n\nদানকারীরা:\n{donaters}\n\nট্রান্সফারের মন্তব্যে নিকনেম পাঠানো যেতে পারে।",
    },
    "none": {
        "en": "None",
        "es": "Ninguno",
        "de": "Keine",
        "fr": "Aucun",
        "it": "Nessuno",
        "pt": "Nenhum",
        "ru": "Нет",
        "uk": "Немає",
        "zh": "无",
        "ja": "なし",
        "ko": "없음",
        "ar": "لا يوجد",
        "tr": "Yok",
        "hi": "कोई नहीं",
        "bn": "কোনও নয়",
    },
}


def tr(key: str, user_id: int) -> str:
    lang = USER_LANGS.get(str(user_id), "en")
    return MESSAGES.get(key, {}).get(lang, MESSAGES.get(key, {}).get("en", ""))

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=SPOTIFY_ID, client_secret=SPOTIFY_SECRET))

app = FastAPI()

async def run_web() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


@app.get("/healthz")  # type: ignore[misc]
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


def encode_id(sid: str) -> str:
    return base64.urlsafe_b64encode(sid.encode()).decode().rstrip("=")


def decode_id(token: str) -> str:
    padding = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + padding).decode()


def load_user_langs() -> None:
    if LANG_FILE.exists():
        try:
            with LANG_FILE.open() as f:
                data = json.load(f)
                if isinstance(data, dict):
                    USER_LANGS.update({str(k): str(v) for k, v in data.items()})
        except json.JSONDecodeError:
            pass


def save_user_langs() -> None:
    with LANG_FILE.open("w") as f:
        json.dump(USER_LANGS, f)


async def compute_download_path(track_id: str) -> Path:
    data: Dict[str, Any] = await asyncio.to_thread(sp.track, track_id)
    track = Track(data)
    group: str = _sort_dir(track, "%artist%/%album%")
    file_name: str = safe_path_string(f"{str(track)}.mp3")
    return DOWNLOAD_DIR / Path(group) / file_name


async def download_track(track_id: str) -> Path:
    """Download a single track and return the resulting file path."""
    url = f"https://open.spotify.com/track/{track_id}"
    savify = Savify(
        api_credentials=(SPOTIFY_ID or "", SPOTIFY_SECRET or ""),
        quality=Quality.BEST,
        download_format=Format.MP3,
        group="%artist%/%album%",
        path_holder=PathHolder(downloads_path=str(DOWNLOAD_DIR)),
        logger=logger,
    )
    await asyncio.to_thread(savify.download, url)
    file_path = await compute_download_path(track_id)
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))
    return file_path


async def search_spotify(query: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    data = sp.search(q=query, type="track", limit=10)
    for item in data["tracks"]["items"]:
        results.append(
            {
                "id": item["id"],
                "title": item["name"],
                "artists": ", ".join(a["name"] for a in item["artists"]),
                "url": item["external_urls"]["spotify"],
                "thumb": (item["album"]["images"][0]["url"] if item["album"].get("images") else None),
            }
        )
    return results


async def send_clean_message(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    **kwargs: Any,
) -> Message:
    last_id = context.chat_data.get("last_bot_message")
    extra_id = context.chat_data.pop("extra_bot_message", None)
    for msg_id in (last_id, extra_id):
        if msg_id:
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
    msg = await context.bot.send_message(chat_id, text, **kwargs)
    context.chat_data["last_bot_message"] = msg.message_id
    return msg


async def send_language_selection(
    chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> None:
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"lang_{code}")]
        for code, name in LANGUAGES.items()
    ]
    await send_clean_message(
        chat_id,
        context,
        tr("choose_language", user_id),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def menu_button_markup(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(tr("menu_button", user_id), callback_data="menu")]]
    )


async def send_menu(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton(tr("change_language", user_id), callback_data="show_lang")],
        [InlineKeyboardButton(tr("donate", user_id), callback_data="donate")],
    ]
    await send_clean_message(
        chat_id,
        context,
        tr("menu", user_id),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


DONATERS_FILE = Path("donaters.txt")


def get_donaters() -> List[str]:
    if DONATERS_FILE.exists():
        with DONATERS_FILE.open() as f:
            return [line.strip() for line in f if line.strip()]
    return []


async def donate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()
        chat_id = query.message.chat_id if query.message else None
    else:
        message = update.effective_message
        if not isinstance(message, Message):
            return
        chat_id = message.chat_id
    if chat_id is None:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    heart_msg = await context.bot.send_message(chat_id, "💗")
    donaters = get_donaters()
    text = tr("donation_info", user_id).format(
        donaters="\n".join(donaters) if donaters else tr("none", user_id)
    )
    await send_clean_message(
        chat_id,
        context,
        text,
        reply_markup=menu_button_markup(user_id),
    )
    context.chat_data["extra_bot_message"] = heart_msg.message_id


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not isinstance(message, Message) or message.from_user is None:
        return
    await send_menu(message.chat_id, message.from_user.id, context)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or query.from_user is None:
        return
    await query.answer()
    await send_menu(query.message.chat_id, query.from_user.id, context)


async def show_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or query.from_user is None:
        return
    await query.answer()
    await send_language_selection(query.message.chat_id, query.from_user.id, context)


async def send_search_results(message: Message, query: str) -> None:
    if message.from_user is None:
        return
    cached = await SEARCH_CACHE.get(f"{message.from_user.id}:{query}")
    if cached:
        results = cached
    else:
        results = await search_spotify(query)
        await SEARCH_CACHE.set(f"{message.from_user.id}:{query}", results)

    keyboard = [
        [
            InlineKeyboardButton(
                f"\U0001F3B5 {item['title']} – {item['artists']}",
                callback_data=item["id"],
            )
        ]
        for item in results
    ]
    await message.reply_text(
        tr("choose_track", message.from_user.id),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not isinstance(message, Message) or not message.text:
        return
    query = message.text
    await send_search_results(message, query)


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if not inline_query or not inline_query.query or inline_query.from_user is None:
        return
    query = inline_query.query
    cached = await SEARCH_CACHE.get(f"{inline_query.from_user.id}:{query}")
    if cached:
        results = cached
    else:
        results = await search_spotify(query)
        await SEARCH_CACHE.set(f"{inline_query.from_user.id}:{query}", results)

    articles = []
    for item in results:
        token = encode_id(item["id"])
        text = f"{item['title']} – {item['artists']}"
        if item.get("thumb"):
            content = InputTextMessageContent(
                f'<a href="{item["thumb"]}">&#8205;</a>{text}',
                parse_mode=ParseMode.HTML,
            )
        else:
            content = InputTextMessageContent(text)

        articles.append(
            InlineQueryResultArticle(
                id=item["id"],
                title=item["title"],
                description=item["artists"],
                thumbnail_url=item.get("thumb"),
                input_message_content=content,
                reply_markup=InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton(
                            tr("download_button", inline_query.from_user.id),
                            url=f"https://t.me/{context.bot.username}?start={token}",
                        )
                    ]]
                ),
            )
        )

    await inline_query.answer(articles)


async def enqueue_download(user_id: int, chat_id: int, track_id: str) -> None:
    if APPLICATION is None:
        return
    if queue.full():
        await APPLICATION.bot.send_message(
            chat_id, tr("too_many_downloads", user_id)
        )
        return
    await queue.put({"user_id": user_id, "chat_id": chat_id, "track_id": track_id})


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    callback = update.callback_query
    if not callback or not callback.data or not isinstance(callback.message, Message) or callback.from_user is None:
        return
    await callback.answer()
    await enqueue_download(
        callback.from_user.id, callback.message.chat_id, callback.data
    )
    await callback.message.reply_text(
        tr("download_started", callback.from_user.id)
    )


async def language_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    callback = update.callback_query
    if not callback or not callback.data or callback.from_user is None:
        return
    code = callback.data.replace("lang_", "")
    USER_LANGS[str(callback.from_user.id)] = code
    save_user_langs()
    await callback.answer()
    args = context.user_data.pop("start_args", [])
    if args:
        track_id = decode_id(args[0])
        await enqueue_download(
            callback.from_user.id, callback.message.chat_id, track_id
        )
        await send_clean_message(
            callback.message.chat_id,
            context,
            tr("download_started", callback.from_user.id),
            reply_markup=menu_button_markup(callback.from_user.id),
        )
    else:
        await send_clean_message(
            callback.message.chat_id,
            context,
            tr("language_saved", callback.from_user.id),
            reply_markup=menu_button_markup(callback.from_user.id),
        )
    await send_menu(callback.message.chat_id, callback.from_user.id, context)
    try:
        await callback.message.edit_reply_markup(None)
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not isinstance(message, Message) or message.from_user is None:
        return
    user_id = str(message.from_user.id)
    if user_id not in USER_LANGS:
        context.user_data["start_args"] = context.args
        await send_language_selection(message.chat_id, message.from_user.id, context)
        return
    if context.args:
        track_id = decode_id(context.args[0])
        await enqueue_download(message.from_user.id, message.chat_id, track_id)
        await send_clean_message(
            message.chat_id,
            context,
            tr("download_started", message.from_user.id),
            reply_markup=menu_button_markup(message.from_user.id),
        )
    await send_menu(message.chat_id, message.from_user.id, context)


async def worker() -> None:
    while True:
        job = await queue.get()
        track_id = job["track_id"]
        chat_id = job["chat_id"]
        if APPLICATION is None:
            queue.task_done()
            continue
        cached = await DOWNLOAD_CACHE.get(track_id)
        if cached and Path(cached).exists():
            file_path = Path(cached)
        else:
            try:
                file_path = await download_track(track_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("download failed", exc_info=exc)
                await APPLICATION.bot.send_message(
                    chat_id, tr("download_failed", job["user_id"])
                )
                queue.task_done()
                continue
            await DOWNLOAD_CACHE.set(track_id, str(file_path))
        token = encode_id(track_id)
        share_link = f"https://t.me/{APPLICATION.bot.username}?start={token}"
        with file_path.open("rb") as f:
            await APPLICATION.bot.send_audio(
                chat_id,
                audio=InputFile(f, filename=file_path.name),
                caption=tr("share", job["user_id"]).format(link=share_link),
            )
        try:
            file_path.unlink()
        except FileNotFoundError:
            pass
        await DOWNLOAD_CACHE.delete(track_id)
        queue.task_done()


def main() -> None:
    global APPLICATION

    load_user_langs()

    async def post_init(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        app.create_task(run_web())
        for _ in range(QUEUE_MAX_SIZE):
            app.create_task(worker())

    bot_app = (
        Application.builder()
        .token(BOT_TOKEN or "")
        .rate_limiter(AIORateLimiter())
        .post_init(post_init)
        .build()
    )
    APPLICATION = bot_app

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("menu", menu_command))
    bot_app.add_handler(CallbackQueryHandler(language_handler, pattern="^lang_"))
    bot_app.add_handler(CallbackQueryHandler(show_language, pattern="^show_lang$"))
    bot_app.add_handler(CallbackQueryHandler(donate_handler, pattern="^donate$"))
    bot_app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu$"))
    bot_app.add_handler(CallbackQueryHandler(button_handler))
    bot_app.add_handler(InlineQueryHandler(handle_inline_query))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    bot_app.run_polling()


if __name__ == "__main__":
    main()
