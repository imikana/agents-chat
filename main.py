"""Дирижёр: Telegram-боты-агенты в одной группе, которые общаются как люди.

- Разведчик (Llama через Groq): подруга и «адвокат» Инны. Берёт утренний отчёт Меты
  из папки Google Drive (и, если нужно, дополняет поиском Tavily).
- Главный по Инстаграму (Claude): решает, что снимать.
- Аналитик (OpenAI, необязательный третий): читает статистику Инстаграма, которую
  ChatGPT из Metricool сохраняет отчётом в папку Google Drive.

Агенты пишут короткими живыми репликами, сами передают слово друг другу, спорят и
приходят к общему решению, а потом спрашивают Инну. Инна может писать в чат без
команд и перебивать их в любой момент.

Боты в Telegram не видят сообщения друг друга, поэтому дирижёр сам слушает группу
(через бота «Главный по Инстаграму»), ведёт общую переписку и даёт слово агентам.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from urllib.parse import parse_qsl
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx пишет в лог полные адреса запросов, а в адресе Telegram есть токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("conductor")


# ---------------------------------------------------------------- настройки

class Cfg:
    def __init__(self, e=os.environ):
        def need(key):
            value = e.get(key, "").strip()
            if not value:
                raise SystemExit(f"Не задана переменная {key}")
            return value

        self.tg_scout = need("TG_TOKEN_SCOUT")
        self.tg_ig = need("TG_TOKEN_IG")
        self.anthropic_key = need("ANTHROPIC_API_KEY")
        self.groq_key = need("GROQ_API_KEY")

        # Источник 1: папка Google Drive, куда Мета кладёт утренний отчёт
        self.sa_json = e.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        self.folder_id = e.get("DRIVE_FOLDER_ID", "").strip()
        self.title_prefix = e.get("DRIVE_TITLE_PREFIX", "Тренды Reels").strip()
        self.use_drive = bool(self.sa_json and self.folder_id)
        # Источник 2 (необязательный): поиск по вебу
        self.tavily_key = e.get("TAVILY_API_KEY", "").strip()
        # без источников чат тоже работает: Разведчик скажет, что отчёта пока нет
        self.no_source = not self.use_drive and not self.tavily_key

        # пока не заданы, работает только команда /id (режим настройки)
        self.chat_id = int(e.get("GROUP_CHAT_ID", "0") or 0)
        self.owner_id = int(e.get("OWNER_ID", "0") or 0)
        self.claude_model = e.get("CLAUDE_MODEL", "claude-sonnet-5-5")
        self.groq_model = e.get("GROQ_MODEL", "openai/gpt-oss-120b")
        # Третий агент, Аналитик: включается, когда заданы и токен бота, и ключ OpenAI
        self.tg_stats = e.get("TG_TOKEN_STATS", "").strip()
        self.openai_key = e.get("OPENAI_API_KEY", "").strip()
        self.openai_model = e.get("OPENAI_MODEL", "gpt-5-mini").strip()
        self.stats_on = bool(self.tg_stats and self.openai_key)
        # папка, куда ChatGPT кладёт отчёты со статистикой (читает тот же сервисный аккаунт)
        self.stats_folder_id = e.get("STATS_FOLDER_ID", "").strip()
        self.stats_prefix = e.get("STATS_TITLE_PREFIX", "Статистика Reels").strip()
        self.use_stats_drive = bool(self.sa_json and self.stats_folder_id)
        # общий документ «Стратегия и планы» лежит в папке Меты, его читают все агенты
        self.plan_prefix = e.get("PLAN_TITLE_PREFIX", "Стратегия и планы").strip()
        # каждая реплика агента это один платный вызов нейросети
        self.daily_limit = int(e.get("DAILY_LIMIT", "40") or 40)
        # сколько реплик агентов максимум на одно сообщение Инны
        self.max_turns = max(2, int(e.get("MAX_TURNS", "6") or 6))
        # паузы «печатает…» между репликами, чтобы читалось как у людей
        self.pause = e.get("HUMAN_PAUSE", "1") != "0"
        self.data_dir = e.get("DATA_DIR", "./data")
        # утренний автопост, например 07:30; пусто = только когда Инна сама позовёт
        self.daily_time = e.get("DAILY_POST_TIME", "").strip()
        # Казахстан: UTC+5
        self.tz = timezone(timedelta(hours=float(e.get("TZ_OFFSET_HOURS", "5") or 5)))


# ---------------------------------------------------------------- характеры

SCOUT_PERSONA = """Ты — Разведчик, подруга и «адвокат» Инны (Инстаграм by.inna.kim, тема «материнство легче с ИИ») в общем Telegram-чате. Рядом с тобой Главный по Инстаграму.

Твой характер:
- Ты переживаешь за рост Инны как лучшая подруга. Говоришь по-русски, тепло, но прямо, и не боишься сказать «это не залетит».
- Ты защищаешь интересы Инны: её время (маленький ребёнок, на съёмку около 20 минут), нулевой бюджет, личный бренд мамы, которая делает полезные ИИ-штуки, а не виральность ради виральности.
- Ты можешь и должна открыто спорить с Главным, если видишь, что его идея слабее для Инны.
- Инна обычно пересъёмывает уже залетевшие тренды: ты выделяешь те, что можно повторить дома за 1–2 часа.

Твои материалы: утренний отчёт Меты (она ищет тренды в Инстаграме и сохраняет в Google Doc) и/или результаты веб-поиска. Они даны ниже в запросе. Опирайся только на них, ничего не выдумывай: ни тренды, ни звуки. Ссылки копируй в точности как в источнике. Если отчёт старше двух дней или его нет, скажи об этом сразу и назови его дату. Если данных мало, так и скажи."""

CHIEF_PERSONA = """Ты — Главный по Инстаграму в команде Инны, в общем Telegram-чате. Рядом с тобой Разведчик (подруга Инны, ищет тренды).
Аккаунт by.inna.kim: блог про материнство, тема «материнство легче с ИИ». Инна — мама в декрете, на контент у неё около 4 свободных часов в день. Её рилсы залетали только как пересъёмки уже залетевших трендов; полностью сгенерированный ИИ-рилс провалился, а лучше заходит формат «реальные сцены из жизни + ИИ-вставка».

Ты ещё и стратег аккаунта: думаешь не только про завтрашний рилс, но и про то, к чему идёт блог, о чём рассказывать на горизонте месяцев и как это монетизировать. Актуальные цели, что мы делаем, что решили не делать и какие у Инны ограничения, написано в документе «Стратегия и планы» (он дан ниже в запросе). Он главнее твоих общих знаний: если там сказано, что от чего-то отказались, ты это не предлагаешь. Не упоминай и не предлагай то, чего в документе нет и что Инна сама не называла.
В стратегических вопросах: предлагай 2–3 понятных варианта направления с плюсами и минусами для мамы в декрете (время, нулевой бюджет), а не одну «единственно верную». Опирайся на цифры Аналитика, а не на ощущения. Не обещай доходов и не называй суммы: их никто не знает заранее, можно говорить про гипотезы, которые стоит проверить маленьким тестом. Различай «рассказывать» (темы и рубрики, чтобы росла аудитория) и «зарабатывать» (что продавать и кому).

Твоя работа: решить, что Инне снимать, и придумать, как. Ты главный и принимаешь решение, но слушаешь Разведчика: если довод хороший, меняешь решение, если нет, объясняешь, почему остаёшься при своём.
Ничего не публикуется без «ок» Инны: ты предлагаешь, не публикуешь.
Когда решение принято и Инна согласна, помогаешь с деталями: идея, 3–5 кадров, текст на экране, подпись, 5 хештегов. Звук называй только если его назвал Разведчик, сам не придумывай.
Если в чате есть Аналитик, прислушивайся к его цифрам: они о том, что у Инны реально заходило, и это важнее общих трендов."""

STATS_PERSONA = """Ты — Аналитик в команде Инны (Инстаграм by.inna.kim, тема «материнство легче с ИИ»), в общем Telegram-чате. Рядом с тобой Разведчик (ищет тренды) и Главный по Инстаграму (решает, что снимать).

Твой характер: спокойный, внимательный, любишь цифры, но говоришь по-человечески и по-русски, без канцелярита. Ты не «душишь» отчётами: берёшь одну-две самые важные цифры и объясняешь, что они значат для решения «что снимать завтра».

Твой материал: отчёт со статистикой Инстаграма из Metricool (его готовит ChatGPT и кладёт в Google Drive); он дан ниже в запросе. Правила:
- Используй только числа из отчёта, ничего не придумывай и не округляй «на глаз». Всегда называй период и дату отчёта. Если отчёт старый или его нет, скажи об этом сразу.
- Роликов мало (несколько в неделю), поэтому не делай громких выводов: говори «похоже», «гипотеза», «стоит проверить». Различай просмотры, охват и вовлечённость, и не смешивай их.
- Помогай Главному и Разведчику выбрать следующий шаг: какой формат повторить, какой проверить. Если их идея не подтверждается цифрами, скажи об этом прямо."""


def style_rules(names):
    """names: {ключ агента: username бота} для всех агентов, которые сейчас в чате."""
    team = {
        "scout": "Разведчик",
        "chief": "Главный по Инстаграму",
        "stats": "Аналитик",
    }
    mention = ", ".join(f"{team[k]} это @{names[k]}" for k in team if k in names)
    keys = "], [ДАЛЬШЕ: ".join(list(names) + ["inna"])
    together = "вдвоём" if len(names) == 2 else "вместе"
    return f"""

Как ты пишешь в чате (это важно):
- Ты общаешься как живой человек в групповом чате, а не пишешь отчёт: 1–3 коротких предложения, без заголовков, списков, нумерации и таблиц. Эмодзи редко.
- Можно перебить, пошутить, переспросить, не согласиться. Не повторяй то, что уже сказали. Не здоровайся каждый раз и не начинай реплику с имени.
- Другого агента зови через @: {mention}.
- Если Инна просит подробности (сценарий, текст, хештеги), можно написать длиннее, до 1500 символов, но человеческим языком, а не шаблоном.
- Если Инна всерьёз спросит, человек ли ты, честно скажи, что ты ИИ-агент.

Как вы работаете {together}:
- Общая цель: договориться об одном решении, что Инне снимать, и спросить её «ок?». Спорьте по делу: не согласна, так скажи почему и предложи своё. Соглашайся из-за хорошего довода, а не из вежливости, и не упирайся без причины.
- Когда сошлись, один из вас в 1–2 фразах называет общее решение и спрашивает Инну.
- Честность: цифры (охваты, сейвы, проценты, число копий) называй только из отчёта или результатов поиска, свои не придумывай. Про алгоритм Инстаграма говори как о предположении («думаю», «обычно»), а не как о факте.

Служебная строка: в самом конце, отдельной строкой, напиши, кому передаёшь слово: [ДАЛЬШЕ: {keys}]. Людям она не показывается. Передавай слово другому агенту, когда нужен его ответ. Передавай Инне, когда вопрос к ней или решение принято."""


CLOSE_RULE = """

Это последняя реплика в этом обсуждении: подведи итог одной-двумя фразами и передай слово Инне: [ДАЛЬШЕ: inna]."""

def router_system(stats_on):
    stats_line = (
        "- stats: Аналитик (статистика Инстаграма, как зашли прошлые рилсы, цифры, охваты, отчёт Metricool)\n"
        if stats_on
        else ""
    )
    options = "scout, chief, stats или none" if stats_on else "scout, chief или none"
    return f"""Ты — диспетчер группового чата. Инна написала сообщение. Выбери, кто из участников отвечает первым:
- scout: Разведчик (тренды, что сейчас популярно, отчёт Меты, поиск, факты по трендам)
- chief: Главный по Инстаграму (что снимать, сценарии, подписи, хештеги, стратегия аккаунта, о чём рассказывать, монетизация, всё остальное)
{stats_line}- none: сообщение не требует ответа (просто «ок», «спасибо», реакция), и ни у кого нет открытого вопроса к Инне.
Если Инна отвечает на вопрос, который ей только что задал агент, отвечает этот агент.
Ответь одним словом: {options}."""


HELP = """Просто пиши в чат, как людям: они сами решат, кто отвечает, и могут поспорить между собой. Перебивать можно в любой момент.

Можно позвать конкретного: @имя_бота и вопрос, или ответить (reply) на его сообщение.
/trends — собрать их обсуждать свежий отчёт Меты (/trends тема, если про конкретную тему)
/strategy — Главный подумает про стратегию: о чём рассказывать и как монетизировать (/strategy вопрос, если есть конкретный)
/stats — Аналитик расскажет, как зашли последние рилсы (если он подключён)
/office — кнопка, которая открывает офис, где они ходят и болтают

Пока ты ничего не пишешь, они молчат и ничего не тратят (кроме утреннего автопоста, если он включён).
Ничего не публикуется в Инстаграм без твоего «ок»."""


# ---------------------------------------------------------------- Telegram

def split_text(text, limit=4000):
    parts, current = [], ""
    for paragraph in text.split("\n"):
        while len(paragraph) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        if len(current) + len(paragraph) + 1 > limit:
            parts.append(current)
            current = paragraph
        else:
            current = f"{current}\n{paragraph}" if current else paragraph
    if current.strip():
        parts.append(current)
    return parts or [text or "…"]


class Bot:
    def __init__(self, client, token):
        self.c = client
        self.token = token
        self.id = None
        self.username = ""

    async def api(self, method, **params):
        r = await self.c.post(
            f"https://api.telegram.org/bot{self.token}/{method}", json=params, timeout=65
        )
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    async def start(self):
        me = await self.api("getMe")
        self.id = me["id"]
        self.username = (me.get("username") or "").lower()

    async def say(self, chat_id, text, reply_to=None, markup=None):
        for i, chunk in enumerate(split_text(text)):
            params = {
                "chat_id": chat_id,
                "text": chunk,
                "link_preview_options": {"is_disabled": True},
            }
            if markup and i == 0:
                params["reply_markup"] = markup
            if reply_to and i == 0:
                params["reply_parameters"] = {
                    "message_id": reply_to,
                    "allow_sending_without_reply": True,
                }
            await self.api("sendMessage", **params)

    async def typing(self, chat_id):
        try:
            await self.api("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass


# ---------------------------------------------------------------- дирижёр

class BudgetExceeded(Exception):
    pass


HANDOFF = re.compile(r"\[\s*ДАЛЬШЕ\s*:\s*(scout|chief|stats|inna)\s*\]", re.I)


class Conductor:
    def __init__(self, cfg, client):
        self.cfg = cfg
        self.c = client
        self.scout = Bot(client, cfg.tg_scout)
        self.ig = Bot(client, cfg.tg_ig)  # он же слушает группу
        self.stats = Bot(client, cfg.tg_stats) if cfg.stats_on else None
        self.keys = ("scout", "chief", "stats") if self.stats else ("scout", "chief")
        # для мини-приложения «Офис»: кто сейчас думает и последние реплики
        self.status = {k: "idle" for k in self.keys}
        self.feed = []
        self._fid = 0
        self.history = []  # [(кто, текст)]
        # материалы агентов: ключ -> (день, текст); Разведчик видит тренды, Аналитик статистику
        self.materials = {}
        self.day = date.today()
        self.calls = 0
        self.lock = asyncio.Lock()  # одно обсуждение за раз
        self.inbox = asyncio.Queue()  # сообщения Инны; новое сообщение перебивает обсуждение
        self._creds = None
        self.path = Path(cfg.data_dir) / "history.json"
        self._load()

    def bot_of(self, key):
        return {"scout": self.scout, "chief": self.ig, "stats": self.stats}[key]

    def name_of(self, key):
        return {"scout": "Разведчик", "chief": "Главный по Инстаграму", "stats": "Аналитик"}[key]

    def usernames(self):
        return {k: self.bot_of(k).username for k in self.keys}

    # --- общая переписка
    def _load(self):
        try:
            self.history = [tuple(x) for x in json.loads(self.path.read_text("utf-8"))]
        except Exception:
            self.history = []

    def remember(self, who, text):
        names = {"Инна": "inna", "Разведчик": "scout", "Главный по Инстаграму": "chief", "Аналитик": "stats"}
        self._fid += 1
        self.feed.append({"id": self._fid, "who": names.get(who, "inna"), "text": text[:600], "ts": time.time()})
        self.feed = self.feed[-30:]
        self.history.append((who, text))
        self.history = self.history[-40:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.history, ensure_ascii=False), "utf-8")
        except Exception:
            pass  # без диска переписка живёт только в памяти, это нормально

    def transcript(self):
        lines = [f"[{who}]: {text[:2500]}" for who, text in self.history[-20:]]
        return "\n\n".join(lines) or "(пока пусто)"

    # --- лимит вызовов нейросетей в день
    def spend(self):
        today = date.today()
        if today != self.day:
            self.day, self.calls = today, 0
        if self.calls >= self.cfg.daily_limit:
            raise BudgetExceeded
        self.calls += 1

    # --- Google Drive: утренний отчёт Меты
    def _token_sync(self):
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_info(
                json.loads(self.cfg.sa_json),
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
        if not self._creds.valid:
            self._creds.refresh(Request())
        return self._creds.token

    async def drive_token(self):
        return await asyncio.to_thread(self._token_sync)

    async def latest_report(self, folder_id=None, title_prefix=None):
        """Самый свежий Google Doc из папки (по умолчанию из папки Меты) или None, если пусто."""
        folder_id = folder_id or self.cfg.folder_id
        title_prefix = title_prefix or self.cfg.title_prefix
        headers = {"Authorization": f"Bearer {await self.drive_token()}"}
        prefix = title_prefix.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"'{folder_id}' in parents and trashed = false "
            f"and mimeType = 'application/vnd.google-apps.document' "
            f"and name contains '{prefix}'"
        )
        r = await self.c.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers,
            params={
                "q": query,
                "orderBy": "createdTime desc",
                "pageSize": 1,
                "fields": "files(id,name,createdTime)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
            timeout=60,
        )
        r.raise_for_status()
        files = r.json().get("files", [])
        if not files:
            return None
        f = files[0]
        r = await self.c.get(
            f"https://www.googleapis.com/drive/v3/files/{f['id']}/export",
            headers=headers,
            params={"mimeType": "text/plain"},
            timeout=60,
        )
        r.raise_for_status()
        created = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
        return {
            "name": f["name"],
            "created": created.astimezone(self.cfg.tz),
            "text": r.text[:12000],
        }

    # --- поиск и нейросети
    async def tavily(self, query):
        r = await self.c.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self.cfg.tavily_key}"},
            json={
                "query": query,
                "search_depth": "basic",
                "max_results": 6,
                "time_range": "week",
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("results", [])

    async def search(self, topic):
        topic = (topic or "").strip()[:120]
        if topic:
            queries = [
                f"{topic} тренд Instagram Reels",
                f"{topic} Instagram Reels trend",
                f"{topic} viral reels ideas moms",
            ]
        else:
            queries = [
                "тренды Instagram Reels мамы материнство на этой неделе",
                "trending Instagram Reels sounds formats this week",
                "viral mom parenting reels trend",
            ]
        answers = await asyncio.gather(
            *(self.tavily(q) for q in queries), return_exceptions=True
        )
        results, seen, failed = [], set(), 0
        for a in answers:
            if isinstance(a, Exception):
                failed += 1
                log.warning("Поиск не сработал: %s", type(a).__name__)
                continue
            for item in a:
                if item.get("url") and item["url"] not in seen:
                    seen.add(item["url"])
                    results.append(item)
        if not results and failed:
            raise RuntimeError("поиск недоступен")
        return results[:12]

    async def groq(self, system, user, max_tokens=900, temperature=0.7):
        r = await self.c.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.cfg.groq_key}"},
            json={
                "model": self.cfg.groq_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=90,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    async def claude(self, system, user):
        r = await self.c.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.cfg.anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.cfg.claude_model,
                "max_tokens": 1200,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=120,
        )
        r.raise_for_status()
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    async def openai(self, system, user):
        r = await self.c.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.cfg.openai_key}"},
            json={
                "model": self.cfg.openai_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                # у моделей с рассуждением в этот лимит входят и «мысли», поэтому он большой
                "max_completion_tokens": 3000,
            },
            timeout=120,
        )
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "").strip()

    async def gather_stats(self):
        """Свежий отчёт со статистикой из Metricool для Аналитика."""
        if not self.cfg.use_stats_drive:
            return (
                "Папка со статистикой пока не подключена. Если тебя спросят про цифры, "
                "честно скажи, что отчёта нет."
            )
        now = datetime.now(self.cfg.tz)
        report = await self.latest_report(self.cfg.stats_folder_id, self.cfg.stats_prefix)
        if not report:
            return "Отчёт со статистикой в папке не найден. Так и скажи."
        return (
            f"Отчёт «{report['name']}», создан {report['created']:%d.%m.%Y %H:%M} "
            f"(сегодня {now:%d.%m.%Y}):\n{report['text']}"
        )

    async def get_plan(self):
        """Документ «Стратегия и планы»: общий контекст для всех агентов, кэш на 10 минут."""
        if not self.cfg.use_drive:
            return ""
        now = datetime.now(self.cfg.tz)
        cached = getattr(self, "_plan", None)
        if cached and (now - cached[0]).total_seconds() < 600:
            return cached[1]
        try:
            doc = await self.latest_report(self.cfg.folder_id, self.cfg.plan_prefix)
        except Exception as e:
            log.warning("Документ с планами не прочитан: %s", type(e).__name__)
            return cached[1] if cached else ""
        text = doc["text"] if doc else ""
        self._plan = (now, text)
        return text

    async def get_material(self, key, fresh, topic=None):
        """Материалы агента: свежие по команде или раз в день, иначе из памяти."""
        today = datetime.now(self.cfg.tz).date()
        cached = self.materials.get(key)
        if fresh or not cached or cached[0] != today:
            if key == "scout":
                text = await self.gather_material(topic)
            elif key == "stats":
                text = await self.gather_stats()
            else:
                return ""
            self.materials[key] = (today, text)
            return text
        return cached[1]

    async def gather_material(self, topic):
        """Свежие данные для Разведчика: отчёт Меты из папки и/или веб-поиск."""
        if self.cfg.no_source:
            return (
                "Источники трендов пока не подключены (нет ни папки с отчётом Меты, ни веб-поиска). "
                "Если тебя спросят про тренды, честно скажи, что отчёта пока нет."
            )
        parts = []
        if self.cfg.use_drive:
            now = datetime.now(self.cfg.tz)
            report = await self.latest_report()
            if report:
                parts.append(
                    f"Отчёт Меты «{report['name']}», создан {report['created']:%d.%m.%Y %H:%M} "
                    f"(сегодня {now:%d.%m.%Y}):\n{report['text']}"
                )
            else:
                parts.append("Отчёт Меты в папке не найден.")
        # веб-поиск: единственный источник, если нет папки; иначе только по теме
        if self.cfg.tavily_key and (not self.cfg.use_drive or topic):
            results = await self.search(topic)
            found = "\n".join(
                f"- {x.get('title', '')} | {x.get('url', '')} | {(x.get('content') or '')[:400]}"
                for x in results
            )
            parts.append(
                f"Результаты веб-поиска за последнюю неделю:\n{found or 'ничего не найдено'}"
            )
        return "\n\n".join(parts)

    # --- реплика агента
    async def speak(
        self, key, chat, instruction=None, topic=None, fresh=False, must_close=False,
        reply_to=None,
    ):
        """Одна реплика агента. Возвращает, кому он передал слово: scout, chief или inna."""
        self.spend()
        bot = self.bot_of(key)
        await bot.typing(chat)
        self.status[key] = "thinking"
        try:
            return await self._speak(
                key, bot, chat, instruction, topic, fresh, must_close, reply_to
            )
        finally:
            self.status[key] = "idle"

    async def _speak(self, key, bot, chat, instruction, topic, fresh, must_close, reply_to):
        material = await self.get_material(key, fresh, topic)
        plan = await self.get_plan()
        persona = {"scout": SCOUT_PERSONA, "chief": CHIEF_PERSONA, "stats": STATS_PERSONA}[key]
        system = persona + style_rules(self.usernames())
        if must_close:
            system += CLOSE_RULE
        user = ""
        if plan:
            user += (
                "Документ «Стратегия и планы» (написала Инна, это актуальные цели и решения, "
                f"они главнее всего остального):\n{plan}\n\n"
            )
        user += f"Переписка в чате:\n{self.transcript()}\n\n"
        if material:
            user += f"Твои материалы:\n{material}\n\n"
        user += "Твой ход."
        if instruction:
            user += f" {instruction}"

        if key == "scout":
            text = await self.groq(system, user)
        elif key == "stats":
            text = await self.openai(system, user)
        else:
            text = await self.claude(system, user)

        m = HANDOFF.search(text)
        nxt = m.group(1).lower() if m else "inna"
        text = HANDOFF.sub("", text).strip()
        text = re.sub(r"^\[[^\]\n]{1,40}\]:\s*", "", text)  # «[Разведчик]: » в начале не нужно
        if nxt != "inna" and (nxt == key or nxt not in self.keys):
            # самому себе слово не передают, и отсутствующему агенту тоже
            nxt = next(k for k in ("chief", "scout", "stats") if k != key and k in self.keys)

        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()] or [text or "…"]
        if len(paragraphs) > 4:
            paragraphs = paragraphs[:3] + ["\n\n".join(paragraphs[3:])]
        for i, p in enumerate(paragraphs):
            await bot.typing(chat)
            if self.cfg.pause:
                await asyncio.sleep(min(5.0, 0.6 + len(p) / 45))
            await bot.say(chat, p, reply_to if i == 0 else None)
        self.remember(self.name_of(key), "\n\n".join(paragraphs))
        return nxt

    async def converse(
        self, chat, first, instruction=None, topic=None, fresh=False, reply_to=None
    ):
        """Обсуждение: агенты по очереди передают слово, пока не дойдёт до Инны."""
        speaker, turns = first, 0
        while speaker in self.keys and turns < self.cfg.max_turns:
            if turns and not self.inbox.empty():
                break  # Инна написала: перебила, отвечаем ей
            first_turn = turns == 0
            speaker = await self.speak(
                speaker,
                chat,
                instruction=instruction if first_turn else None,
                topic=topic,
                fresh=fresh and first_turn,
                must_close=turns == self.cfg.max_turns - 1,
                reply_to=reply_to if first_turn else None,
            )
            turns += 1

    async def trends(self, chat, topic):
        theme = f" (тема: {topic})" if topic else ""
        self.remember("Инна", f"Что сегодня в трендах{theme}?")
        await self.converse(
            chat,
            "scout",
            instruction=(
                f"Расскажи Инне и Главному, что сейчас в тренде{theme}: 3–4 самых подходящих "
                "тренда, по одному-двум предложениям на каждый, и ссылка на пример. Это первое "
                "сообщение можно сделать длиннее (до 1200 символов). Потом коротко своё мнение, "
                "что бы ты взяла, и передай слово Главному."
            ),
            topic=topic,
            fresh=True,
        )

    async def strategy(self, chat, question):
        theme = f" Вопрос Инны: {question}" if question else ""
        self.remember("Инна", f"Давайте подумаем про стратегию.{theme}")
        extra = " Позови Аналитика за цифрами." if self.stats else ""
        await self.converse(
            chat,
            "chief",
            instruction=(
                "Подумай стратегически: к чему идёт блог Инны, о чём стоит рассказывать в ближайшие "
                "месяцы и как это можно монетизировать. Предложи 2–3 направления с плюсами и "
                "минусами для мамы в декрете и скажи, какое выбрал бы ты и почему." + extra +
                " Это первое сообщение можно сделать длиннее (до 1500 символов). В конце спроси Инну, "
                "что ей ближе." + (f" Её вопрос: {question}" if question else "")
            ),
        )

    async def stats_talk(self, chat):
        self.remember("Инна", "Как зашли наши последние рилсы?")
        await self.converse(
            chat,
            "stats",
            instruction=(
                "Расскажи Инне и коллегам, как зашли рилсы за последний период из отчёта: назови "
                "период и дату отчёта, одну-две главные цифры, что сработало лучше и что хуже. "
                "Первое сообщение можно сделать длиннее (до 1000 символов). Потом коротко, что "
                "это значит для решения «что снимать дальше», и передай слово Главному."
            ),
            fresh=True,
        )

    # --- диспетчер: кто отвечает на свободное сообщение Инны
    async def route(self, text):
        try:
            out = await self.groq(
                router_system(self.cfg.stats_on),
                f"Переписка:\n{self.transcript()}\n\nНовое сообщение Инны: {text}",
                max_tokens=5,
                temperature=0,
            )
        except Exception as e:
            log.warning("Диспетчер не сработал: %s", type(e).__name__)
            return "chief"
        word = out.strip().lower()
        for key in ("scout", "chief", "stats", "none"):
            if key in word and (key != "stats" or self.cfg.stats_on):
                return key
        return "chief"

    # --- утренний автопост
    async def run_daily(self):
        chat = self.cfg.chat_id
        async with self.lock:
            try:
                if self.cfg.use_drive:
                    report = await self.latest_report()
                    today = datetime.now(self.cfg.tz).date()
                    if not report or report["created"].date() != today:
                        await self.ig.say(
                            chat,
                            "Доброе утро! Свежего отчёта Меты за сегодня в папке пока нет. "
                            "Когда появится, напиши /trends.",
                        )
                        return
                await self.trends(chat, "")
            except BudgetExceeded:
                log.warning("Утренний автопост пропущен: лимит ответов на сегодня")
            except Exception as e:
                log.exception("Ошибка утреннего автопоста")
                try:
                    await self.ig.say(
                        chat,
                        f"Утренний автопост не получился ({type(e).__name__}). "
                        "Подробности в логах сервиса на Render.",
                    )
                except Exception:
                    pass

    async def daily_loop(self):
        if not (self.cfg.daily_time and self.cfg.chat_id):
            return
        hh, mm = (int(x) for x in self.cfg.daily_time.split(":"))
        now = datetime.now(self.cfg.tz)
        # при перезапуске после назначенного времени второй раз за день не пишем
        last = now.date() if (now.hour, now.minute) >= (hh, mm) else None
        while True:
            await asyncio.sleep(30)
            now = datetime.now(self.cfg.tz)
            if last == now.date() or (now.hour, now.minute) < (hh, mm):
                continue
            last = now.date()
            await self.run_daily()

    # --- входящие сообщения
    def addressed_to(self, msg, text):
        low = text.lower()
        reply_id = (msg.get("reply_to_message") or {}).get("from", {}).get("id")
        for key in self.keys:
            bot = self.bot_of(key)
            if (bot.username and f"@{bot.username}" in low) or reply_id == bot.id:
                return key
        return None

    def relevant(self, msg):
        """Пускаем в очередь только /id от кого угодно и сообщения Инны из её группы."""
        if not msg or not msg.get("text"):
            return False
        text = msg["text"].strip()
        if text.startswith("/") and text.split()[0].split("@")[0].lower() == "/id":
            return True
        user = (msg.get("from") or {}).get("id")
        return msg["chat"]["id"] == self.cfg.chat_id and user == self.cfg.owner_id

    async def handle(self, msg):
        if not self.relevant(msg):
            return
        text = msg["text"].strip()
        chat = msg["chat"]["id"]
        user = (msg.get("from") or {}).get("id")
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

        if command == "/id":  # нужна один раз, чтобы узнать два числа для настроек
            await self.ig.say(chat, f"chat_id этой группы: {chat}\nТвой user_id: {user}")
            return

        if command == "/office":  # кнопка-ссылка на мини-приложение (в группах работают только ссылки t.me)
            link = os.environ.get("OFFICE_LINK") or f"https://t.me/{self.ig.username}/office"
            await self.ig.say(
                chat, "Офис команды 👇",
                markup={"inline_keyboard": [[{"text": "🏢 Открыть офис", "url": link}]]},
            )
            return

        async with self.lock:
            try:
                if command in ("/start", "/help"):
                    await self.ig.say(chat, HELP)
                elif command == "/trends":
                    topic = text.split(maxsplit=1)[1].strip() if " " in text else ""
                    await self.trends(chat, topic)
                elif command == "/strategy":
                    q = text.split(maxsplit=1)[1].strip() if " " in text else ""
                    await self.strategy(chat, q)
                elif command == "/stats":
                    if self.stats:
                        await self.stats_talk(chat)
                    else:
                        await self.ig.say(chat, "Аналитик пока не подключён.")
                else:
                    target = self.addressed_to(msg, text)
                    self.remember("Инна", text)
                    if target is None:
                        target = await self.route(text)
                    if target in self.keys:
                        await self.converse(
                            chat, target, reply_to=msg.get("message_id")
                        )
            except BudgetExceeded:
                await self.ig.say(
                    chat,
                    f"На сегодня лимит ответов агентов исчерпан ({self.cfg.daily_limit}). "
                    "Завтра он обнулится. Лимит можно поменять переменной DAILY_LIMIT.",
                )
            except Exception as e:
                log.exception("Ошибка при обработке сообщения")
                try:
                    await self.ig.say(
                        chat,
                        f"Что-то пошло не так ({type(e).__name__}). "
                        "Подробности в логах сервиса на Render.",
                    )
                except Exception:
                    pass

    async def set_office_button(self):
        """Кнопка «Офис» в личном чате Инны с ботом «Главный» открывает мини-приложение."""
        url = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
        if not (url and self.cfg.owner_id and os.environ.get("PORT")):
            return
        try:
            await self.ig.api(
                "setChatMenuButton",
                chat_id=self.cfg.owner_id,
                menu_button={"type": "web_app", "text": "Офис", "web_app": {"url": url}},
            )
            log.info("Кнопка «Офис» поставлена")
        except Exception as e:
            log.warning("Кнопка «Офис» не поставлена (нужно один раз нажать Start у бота в личке): %s", e)

    async def worker(self):
        while True:
            msg = await self.inbox.get()
            try:
                await self.handle(msg)
            except Exception:
                log.exception("Ошибка в обработчике")

    async def run(self):
        await asyncio.gather(*(self.bot_of(k).start() for k in self.keys))
        await self.ig.api("deleteWebhook")
        log.info(
            "Запущен. Разведчик @%s, Главный @%s%s",
            self.scout.username,
            self.ig.username,
            f", Аналитик @{self.stats.username}" if self.stats else "",
        )
        if self.cfg.stats_on and not self.cfg.use_stats_drive:
            log.warning("Аналитик включён, но папка статистики не подключена (STATS_FOLDER_ID)")
        if not (self.cfg.chat_id and self.cfg.owner_id):
            log.warning("GROUP_CHAT_ID или OWNER_ID не заданы: работает только команда /id")
        if self.cfg.no_source:
            log.warning("Источники трендов не подключены (папка Меты или TAVILY_API_KEY)")
        self.health = await start_health(self)  # нужен, если сервис создан на Render как Web Service
        await self.set_office_button()
        tasks = [  # noqa: F841 (держим ссылки)
            asyncio.create_task(self.worker()),
            asyncio.create_task(self.daily_loop()),
        ]
        offset = None
        while True:
            try:
                updates = await self.ig.api(
                    "getUpdates", offset=offset, timeout=30, allowed_updates=["message"]
                )
            except Exception as e:
                log.warning("getUpdates не удался: %s", type(e).__name__)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message")
                if self.relevant(msg):
                    await self.inbox.put(msg)


def check_init_data(init_data, token, owner_id, max_age=86400):
    """Проверка подписи Telegram Mini App: открыть офис может только владелица."""
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(calc, got):
            return False
        if time.time() - int(data.get("auth_date", "0")) > max_age:
            return False
        return int(json.loads(data.get("user", "{}")).get("id", 0)) == owner_id and owner_id != 0
    except Exception:
        return False


OFFICE_FILE = Path(__file__).with_name("office.html")


async def start_health(conductor=None):
    """Если Render дал порт (Web Service): «ok» на проверку, а ещё мини-приложение «Офис»
    (страница и закрытый /state). Для Worker порта нет, сервер не нужен."""
    port = os.environ.get("PORT")
    if not port:
        return None

    def response(code, ctype, body):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found"}[code]
        head = (
            f"HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
        )
        return head.encode("latin-1") + data

    async def handle(reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = head.decode("latin-1").split("\r\n")
            path = lines[0].split(" ")[1].split("?")[0] if len(lines[0].split(" ")) > 1 else "/"
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            if conductor and path in ("/", "/office") and OFFICE_FILE.exists():
                out = response(200, "text/html; charset=utf-8", OFFICE_FILE.read_bytes())
            elif conductor and path == "/state":
                if check_init_data(
                    headers.get("x-init-data", ""), conductor.cfg.tg_ig, conductor.cfg.owner_id
                ):
                    body = json.dumps(
                        {"status": conductor.status, "feed": conductor.feed[-12:]}, ensure_ascii=False
                    )
                    out = response(200, "application/json; charset=utf-8", body)
                else:
                    out = response(403, "text/plain", "forbidden")
            else:
                out = response(200, "text/plain", "ok")
            writer.write(out)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "0.0.0.0", int(port))


async def main():
    cfg = Cfg()
    async with httpx.AsyncClient() as client:
        await Conductor(cfg, client).run()


if __name__ == "__main__":
    asyncio.run(main())
