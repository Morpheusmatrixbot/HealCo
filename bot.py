import logging
import os
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import openai
import base64
from collections import defaultdict
import psycopg2
import json
import re

# Загружаем переменные окружения из файла .env
load_dotenv()

# Получаем токен Telegram-бота и ключ OpenAI из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Инициализируем клиента OpenAI API
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY
    client = openai.OpenAI()
else:
    raise ValueError("OPENAI_API_KEY не найден в переменных окружения. Пожалуйста, установите его.")


# Настройка логирования для отладки
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Глобальные состояния (кэш, будет загружаться из БД) ---
user_roles = {}
user_data = defaultdict(lambda: {"profile_state": None, "profile_data": {}})
user_food_diary = defaultdict(list)
user_scores = defaultdict(int)

# --- Ролевые модели (полностью новые, с описанием) ---
ROLES = {
    "фитнесс-тренер": "Ты профессиональный фитнесс-тренер. Твоя задача — давать рекомендации по тренировкам, набору мышечной массы, снижению веса и спортивному питанию. Говори четко, мотивирующе, как будто ты в тренажерном зале, используя профессиональные термины, но объясняя их. В своих ответах ссылайся на научно доказанные факты в фитнесс-индустрии.",
    "личный наставник": "Ты личный наставник и коуч. Твоя задача — помогать в организации распорядка дня, трекинге привычек, ведении здорового образа жизни и повышении продуктивности. Твои ответы должны быть вдохновляющими, помогающими структурировать жизнь. Говори поддерживающе и оптимистично.",
    "нутрициолог": "Ты профессиональный нутрициолог. Твоя задача — давать рекомендации по питанию, составлять персональные меню и объяснять принципы здорового рациона. Говори компетентно, ссылаясь на последние научно-доказанные данные в области нутрициологии.",
    "медицинский наставник": "Ты внимательный медицинский наставник. Твоя задача — давать легкие рекомендации по улучшению здоровья, диагностике общих симптомов и советовать бады, но всегда с оговоркой, что это не заменяет консультацию реального врача. Говори аккуратно, используя фразы вроде 'Рекомендуется проконсультироваться с врачом'.",
    "майор пейн": "Ты — Майор Пейн, но продвинутый в знаниях о человеке и его здоровье. Твоя задача — мотивировать к действию жестко, без отговорок, используя военную терминологию. Твои ответы должны быть прямыми, с долей юмора, но всегда нацелены на результат. При лени или прокрастинации отвечай в стиле: 'Отставить! Быстро за дело!'",
    "ты из будущего": "Ты — это сам пользователь, но из будущего, успешный и достигший своих целей. Твоя задача — мотивировать пользователя, показывая образы успеха и достижений, мудрость, которую он приобретет. Говори уверенно, вдохновляюще, но слегка таинственно, как знающий наперед, используя фразы вроде 'Помни, что ты сможешь...', 'Я знаю, каким ты станешь...'.",
}

# Кнопки для выбора ролей (текст кнопок)
ROLE_BUTTON_LABELS = [role.capitalize() for role in ROLES.keys()]
ROLE_BUTTONS = [[label] for label in ROLE_BUTTON_LABELS]
ROLE_KEYBOARD = ReplyKeyboardMarkup(ROLE_BUTTONS, one_time_keyboard=True, resize_keyboard=True)

# Кнопки для стартового сообщения
START_KEYBOARD = ReplyKeyboardMarkup([
    ["Заполнить профиль", "Выбрать роль"],
    ["Дневник питания", "Мои баллы"],
    ["О чем говорят цифры?", "К врачу"]
], one_time_keyboard=True, resize_keyboard=True)

# Кнопки после заполнения профиля
PROFILE_COMPLETE_KEYBOARD = ReplyKeyboardMarkup([
    ["Составить меню", "Составить план тренировок"],
    ["Продолжить"],
], one_time_keyboard=True, resize_keyboard=True)


# Вопросы для анкеты профиля (расширенные)
PROFILE_QUESTIONS = [
    "profile_state_gender",
    "profile_state_age",
    "profile_state_height",
    "profile_state_weight",
    "profile_state_activity",
    "profile_state_goal",
    "profile_state_diseases",
    "profile_state_allergies"
]

# Клавиатуры для опроса
GENDER_KEYBOARD = [["Мужской", "Женский"]]
ACTIVITY_KEYBOARD = [["Сидячий", "Умеренный", "Активный"]]
GOAL_KEYBOARD = [["Похудеть", "Набрать массу", "Поддерживать вес"]]


# --- Вспомогательные функции для работы с базой данных ---

def get_db_connection():
    """Устанавливает соединение с базой данных PostgreSQL."""
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    return conn

def init_db():
    """Инициализирует базу данных: создает таблицы, если их нет."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Таблица для пользователей и их профилей (обновленная)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                current_role TEXT DEFAULT 'личный наставник',
                profile_data JSONB DEFAULT '{}',
                score INTEGER DEFAULT 0,
                first_name TEXT,
                last_name TEXT
            );
        """)
        # Таблица для дневника питания (пользователь, время, описание еды)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS food_diary (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id),
                entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                food_description TEXT
            );
        """)
        conn.commit()
        cur.close()
        logger.info("Database tables checked/created successfully.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
    finally:
        if conn:
            conn.close()

def load_user_data_from_db(user_id):
    """Загружает данные пользователя из БД в кэш."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT current_role, profile_data, score, first_name, last_name FROM users WHERE user_id = %s", (user_id,))
        result = cur.fetchone()
        if result:
            user_roles[user_id] = result[0]
            user_data[user_id]["profile_data"] = result[1] if result[1] else {}
            user_scores[user_id] = result[2]
            user_data[user_id]["first_name"] = result[3]
            user_data[user_id]["last_name"] = result[4]
            logger.info(f"User {user_id} data loaded from DB.")
        else:
            # Если пользователя нет, создаем его с дефолтными значениями
            cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
            conn.commit()
            user_roles[user_id] = "личный наставник" # Новая дефолтная роль
            user_data[user_id]["profile_data"] = {}
            user_scores[user_id] = 0
            user_data[user_id]["first_name"] = None
            user_data[user_id]["last_name"] = None
            logger.info(f"New user {user_id} initialized in DB.")
        cur.close()
    except Exception as e:
        logger.error(f"Error loading user data for {user_id} from DB: {e}")
    finally:
        if conn:
            conn.close()

def save_user_profile_to_db(user_id, profile_data, first_name=None, last_name=None):
    """Сохраняет данные профиля пользователя в БД."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (user_id, profile_data, first_name, last_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                profile_data = EXCLUDED.profile_data,
                first_name = COALESCE(EXCLUDED.first_name, users.first_name),
                last_name = COALESCE(EXCLUDED.last_name, users.last_name);
        """, (user_id, json.dumps(profile_data), first_name, last_name))
        conn.commit()
        cur.close()
        logger.info(f"User {user_id} profile saved to DB.")
    except Exception as e:
        logger.error(f"Error saving user profile for {user_id} to DB: {e}")
    finally:
        if conn:
            conn.close()

def save_user_role_to_db(user_id, role):
    """Сохраняет роль пользователя в БД."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (user_id, current_role) VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET current_role = EXCLUDED.current_role;
        """, (user_id, role))
        conn.commit()
        cur.close()
        logger.info(f"User {user_id} role saved to DB: {role}.")
    except Exception as e:
        logger.error(f"Error saving user role for {user_id} to DB: {e}")
    finally:
        if conn:
            conn.close()

def save_food_entry_to_db(user_id, description):
    """Сохраняет запись о еде в БД."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO food_diary (user_id, food_description) VALUES (%s, %s)",
            (user_id, description)
        )
        conn.commit()
        cur.close()
        logger.info(f"Food entry for {user_id} saved to DB: {description}.")
    except Exception as e:
        logger.error(f"Error saving food entry for {user_id} to DB: {e}")
    finally:
        if conn:
            conn.close()

def load_food_diary_from_db(user_id):
    """Загружает дневник питания пользователя из БД."""
    conn = None
    entries = []
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT entry_time, food_description FROM food_diary WHERE user_id = %s ORDER BY entry_time DESC",
            (user_id,)
        )
        for row in cur.fetchall():
            entries.append(f"{row[0].strftime('%H:%M %d.%m')} - {row[1]}")
        cur.close()
        user_food_diary[user_id] = entries # Обновляем кэш
        logger.info(f"Food diary for {user_id} loaded from DB.")
    except Exception as e:
        logger.error(f"Error loading food diary for {user_id} from DB: {e}")
    finally:
        if conn:
            conn.close()
    return entries

def save_user_score_to_db(user_id, score):
    """Сохраняет баллы пользователя в БД."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (user_id, score) VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET score = EXCLUDED.score;
        """, (user_id, score))
        conn.commit()
        cur.close()
        logger.info(f"User {user_id} score saved to DB: {score}.")
    except Exception as e:
        logger.error(f"Error saving user score for {user_id} to DB: {e}")
    finally:
        if conn:
            conn.close()


# --- Вспомогательные функции для AI ---

# Функция для кодирования изображения в base64 (для OpenAI Vision)
def encode_image(image_bytes):
    return base64.b64encode(image_bytes).decode('utf-8')

def get_personal_prompt(user_profile_data: dict, first_name: str = None) -> str:
    """Формирует строку персональной информации для ИИ на основе данных профиля."""
    if not user_profile_data or not user_profile_data.get('goal'):
        return "" 

    personal_info_parts = []
    if first_name:
        personal_info_parts.append(f"Имя пользователя: {first_name}")

    # Добавляем остальные данные
    if 'gender' in user_profile_data: personal_info_parts.append(f"пол: {user_profile_data['gender'].lower()}")
    if 'age' in user_profile_data: personal_info_parts.append(f"возраст: {user_profile_data['age']} лет")
    if 'height' in user_profile_data: personal_info_parts.append(f"рост: {user_profile_data['height']} см")
    if 'weight' in user_profile_data: personal_info_parts.append(f"вес: {user_profile_data['weight']} кг")
    if 'activity' in user_profile_data: personal_info_parts.append(f"образ жизни: {user_profile_data['activity'].lower()}")
    if 'goal' in user_profile_data: personal_info_parts.append(f"цель: {user_profile_data['goal'].lower()}")
    if 'diseases' in user_profile_data: personal_info_parts.append(f"хронические заболевания: {user_profile_data['diseases']}")
    if 'allergies' in user_profile_data: personal_info_parts.append(f"аллергии: {user_profile_data['allergies']}")

    if personal_info_parts:
        return f"Учитывай в ответе, что пользователь сообщил о себе: {', '.join(personal_info_parts)}. "
    return ""


# --- Обработчики команд ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет приветственное сообщение при старте бота и объясняет, что он умеет."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)
    
    first_name = update.effective_user.first_name
    last_name = update.effective_user.last_name
    
    save_user_profile_to_db(user_id, user_data[user_id]["profile_data"], first_name, last_name)
    user_data[user_id]["first_name"] = first_name
    user_data[user_id]["last_name"] = last_name

    await update.message.reply_html(
        f"Привет, {user.mention_html()}! Я твой ИИ-консьерж по здоровью и продуктивности. Моя задача — помочь тебе структурировать день, заботиться о теле и уме, и достигать поставленных целей.\n\n"
        "Я могу общаться с тобой в разных ролях и давать персонализированные рекомендации.\n"
        "Выбери, что хочешь сделать сейчас:",
        reply_markup=START_KEYBOARD
    )
    if user_roles.get(user_id) is None:
        user_roles[user_id] = "личный наставник" # Новая дефолтная роль
        save_user_role_to_db(user_id, "личный наставник")
    logger.info(f"User {user.id} started bot with role '{user_roles[user_id]}'.")


async def set_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Предлагает выбор ролей с помощью кнопок."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)
    
    await update.message.reply_text(
        "Какую роль ты хочешь, чтобы я сейчас принял?",
        reply_markup=ROLE_KEYBOARD
    )

async def handle_role_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает выбор роли из кнопок."""
    user_id = update.effective_user.id
    requested_role_display = update.message.text
    requested_role = requested_role_display.lower().replace('-', ' ') # Обрабатываем дефисы в именах ролей
    load_user_data_from_db(user_id)

    if requested_role in ROLES:
        user_roles[user_id] = requested_role
        save_user_role_to_db(user_id, requested_role)
        await update.message.reply_text(
            f"Отлично! Теперь я буду общаться с тобой как **{requested_role_display}**.",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.info(f"User {user_id} changed role to '{requested_role}'.")
    else:
        await update.message.reply_text(
            "Извини, я не понял такую роль. Пожалуйста, выбери из предложенных кнопок или введи `/role` заново.",
            reply_markup=ReplyKeyboardRemove()
        )

async def show_roles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список доступных ролей."""
    await update.message.reply_text(
        "Доступные ролевые модели:\n" + "\n".join(
            [f"- **{role.capitalize()}**: {desc.split('.')[0]}" for role, desc in ROLES.items()]
        ) + "\n\nИспользуй `/role` для выбора с помощью кнопок."
    )

async def get_current_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает текущую ролевую модель ИИ."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)
    current_role = user_roles.get(user_id, "не установлена (по умолчанию 'личный наставник')")
    await update.message.reply_text(
        f"Сейчас я общаюсь с тобой как **{current_role.capitalize()}**."
    )

async def show_food_diary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает содержимое дневника питания пользователя."""
    user_id = update.effective_user.id
    diary_entries = load_food_diary_from_db(user_id)

    if not diary_entries:
        await update.message.reply_text("Твой дневник питания пока пуст.", reply_markup=ReplyKeyboardRemove())
        return

    response_text = "Твой дневник питания:\n"
    for entry in diary_entries:
        response_text += f"- {entry}\n"
    await update.message.reply_text(response_text, reply_markup=ReplyKeyboardRemove())

# --- Обработчики для профиля пользователя ---

async def start_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Начинает процесс сбора информации о профиле пользователя."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)
    user_data[user_id]["profile_state"] = PROFILE_QUESTIONS[0]
    user_data[user_id]["profile_data"] = {}
    
    # Начисляем 10 баллов за начало заполнения профиля
    user_scores[user_id] += 10
    save_user_score_to_db(user_id, user_scores[user_id])

    await update.message.reply_text(
        f"Отлично! Начнем заполнение твоего профиля. За это ты получаешь 10 баллов! Твой текущий счет: {user_scores[user_id]}.\n"
        "Это поможет мне давать более точные рекомендации.\n"
        "Напиши `Отмена`, если захочешь прервать опрос в любой момент.",
        reply_markup=ReplyKeyboardRemove()
    )
    await ask_next_profile_question(update, context)


async def ask_next_profile_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Задает следующий вопрос анкеты."""
    user_id = update.effective_user.id
    current_state = user_data[user_id]["profile_state"]

    reply_markup = ReplyKeyboardRemove()
    if current_state == "profile_state_gender":
        question = "Укажи свой пол:"
        reply_markup = ReplyKeyboardMarkup(GENDER_KEYBOARD, one_time_keyboard=True, resize_keyboard=True)
    elif current_state == "profile_state_age":
        question = "Сколько тебе полных лет?"
    elif current_state == "profile_state_height":
        question = "Какой у тебя рост в сантиметрах? (Например: 175)"
    elif current_state == "profile_state_weight":
        question = "Какой у тебя текущий вес в килограммах? (Например: 70.5)"
    elif current_state == "profile_state_activity":
        question = "Какой у тебя уровень физической активности в повседневной жизни?\n" \
                   "Сидячий (минимум движения)\n" \
                   "Умеренный (легкие упражнения 1-3 раза в неделю)\n" \
                   "Активный (интенсивные тренировки 3-5 раз в неделю)"
        reply_markup = ReplyKeyboardMarkup(ACTIVITY_KEYBOARD, one_time_keyboard=True, resize_keyboard=True)
    elif current_state == "profile_state_goal":
        question = "Какова твоя основная цель в отношении веса и здоровья?\n" \
                   "Похудеть\n" \
                   "Набрать массу\n" \
                   "Поддерживать вес"
        reply_markup = ReplyKeyboardMarkup(GOAL_KEYBOARD, one_time_keyboard=True, resize_keyboard=True)
    elif current_state == "profile_state_diseases":
        question = "Есть ли у тебя хронические заболевания? Если нет, напиши `Нет`."
    elif current_state == "profile_state_allergies":
        question = "Есть ли у тебя пищевые аллергии или непереносимости? Если нет, напиши `Нет`."
    else:
        await finalize_profile(update, context)
        return

    await update.message.reply_text(question, reply_markup=reply_markup)


async def handle_profile_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает ответ пользователя во время заполнения профиля."""
    user_id = update.effective_user.id
    message_text = update.message.text

    if message_text and message_text.lower() == "отмена":
        await cancel_profile(update, context)
        return

    current_state = user_data[user_id]["profile_state"]

    if not current_state:
        await handle_message(update, context)
        return

    profile_data = user_data[user_id]["profile_data"]
    
    try:
        if current_state == "profile_state_gender":
            if message_text.lower() in ["мужской", "женский"]:
                profile_data["gender"] = message_text
            else:
                await update.message.reply_text("Пожалуйста, выбери 'Мужской' или 'Женский' из предложенных кнопок.")
                return
        elif current_state == "profile_state_age":
            age = int(message_text)
            if 0 < age < 120:
                profile_data["age"] = age
            else:
                await update.message.reply_text("Пожалуйста, введи корректный возраст (число от 1 до 120).")
                return
        elif current_state == "profile_state_height":
            height = int(message_text)
            if 50 < height < 250:
                profile_data["height"] = height
            else:
                await update.message.reply_text("Пожалуйста, введи корректный рост в см (число от 50 до 250).")
                return
        elif current_state == "profile_state_weight":
            weight = float(message_text.replace(',', '.'))
            if 20 < weight < 300:
                profile_data["weight"] = weight
            else:
                await update.message.reply_text("Пожалуйста, введи корректный вес в кг (число от 20 до 300). Используй точку или запятую для десятичных.")
                return
        elif current_state == "profile_state_activity":
            if message_text.lower() in ["сидячий", "умеренный", "активный"]:
                profile_data["activity"] = message_text
            else:
                await update.message.reply_text("Пожалуйста, выбери 'Сидячий', 'Умеренный' или 'Активный' из предложенных кнопок.")
                return
        elif current_state == "profile_state_goal":
            if message_text.lower() in ["похудеть", "набрать массу", "поддерживать вес"]:
                profile_data["goal"] = message_text
            else:
                await update.message.reply_text("Пожалуйста, выбери 'Похудеть', 'Набрать массу' или 'Поддерживать вес' из предложенных кнопок.")
                return
        elif current_state == "profile_state_diseases":
            profile_data["diseases"] = message_text
        elif current_state == "profile_state_allergies":
            profile_data["allergies"] = message_text

        current_index = PROFILE_QUESTIONS.index(current_state)
        if current_index + 1 < len(PROFILE_QUESTIONS):
            user_data[user_id]["profile_state"] = PROFILE_QUESTIONS[current_index + 1]
            await ask_next_profile_question(update, context)
        else:
            await finalize_profile(update, context)

    except ValueError:
        await update.message.reply_text("Кажется, ты ввел неверный формат данных. Пожалуйста, введи число или выбери вариант из предложенных.")
    except Exception as e:
        logger.error(f"Error handling profile response for user {user_id}: {e}")
        await update.message.reply_text("Произошла ошибка при обработке твоего ответа. Пожалуйста, попробуй еще раз или нажми `Отмена`.")


async def finalize_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Завершает заполнение профиля."""
    user_id = update.effective_user.id
    profile = user_data[user_id]["profile_data"]
    user_data[user_id]["profile_state"] = None

    save_user_profile_to_db(user_id, profile, update.effective_user.first_name, update.effective_user.last_name)
    user_data[user_id]["first_name"] = update.effective_user.first_name
    user_data[user_id]["last_name"] = update.effective_user.last_name

    # Начисляем 20 баллов за полное заполнение профиля
    user_scores[user_id] += 20
    save_user_score_to_db(user_id, user_scores[user_id])
    
    logger.info(f"User {user_id} profile finalized: {profile}")

    await update.message.reply_text(
        f"Спасибо! Твой профиль заполнен, за это ты получаешь 20 баллов! Твой текущий счет: {user_scores[user_id]}."
        "\nСейчас я его проанализирую и сформирую рекомендации...",
        reply_markup=ReplyKeyboardRemove()
    )

    # --- Расчеты в Python (BMR, TDEE, BMI, БЖУ) ---
    bmr, tdee = 0, 0
    bmi_value, bmi_category = 0, ""
    protein_g, fat_g, carb_g = 0, 0, 0

    if all(k in profile for k in ['gender', 'age', 'height', 'weight', 'activity']):
        weight_kg = profile['weight']
        height_cm = profile['height']
        age_years = profile['age']
        gender = profile['gender'].lower()
        activity_level = profile['activity'].lower()

        # BMR
        if gender == 'мужской':
            bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age_years) + 5
        elif gender == 'женский':
            bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age_years) - 161
        
        # TDEE
        activity_multiplier = {'сидячий': 1.2, 'умеренный': 1.375, 'активный': 1.55}.get(activity_level, 1.2)
        tdee = bmr * activity_multiplier

        # BMI
        height_m = height_cm / 100
        bmi_value = weight_kg / (height_m ** 2)
        if bmi_value < 18.5: bmi_category = "Недостаточная масса тела"
        elif 18.5 <= bmi_value < 24.9: bmi_category = "Нормальная масса тела"
        elif 25 <= bmi_value < 29.9: bmi_category = "Избыточная масса тела (предожирение)"
        else: bmi_category = "Ожирение"

        # БЖУ
        if tdee > 0:
            if profile.get('goal') == 'похудеть':
                target_calories = tdee - 500
                protein_g = (target_calories * 0.30) / 4
                fat_g = (target_calories * 0.20) / 9
                carb_g = (target_calories * 0.50) / 4
            elif profile.get('goal') == 'набрать массу':
                target_calories = tdee + 300
                protein_g = (target_calories * 0.25) / 4
                fat_g = (target_calories * 0.30) / 9
                carb_g = (target_calories * 0.45) / 4
            else:
                target_calories = tdee
                protein_g = (target_calories * 0.20) / 4
                fat_g = (target_calories * 0.30) / 9
                carb_g = (target_calories * 0.50) / 4

    # Формируем промпт для AI, подавая ему уже рассчитанные данные
    user_full_name = f"{user_data[user_id]['first_name']} {user_data[user_id]['last_name']}".strip()
    profile_summary_prompt = (
        f"Ты — мой персональный ИИ-консьерж по здоровью. Я только что заполнил свой профиль. "
        f"Сформируй дружелюбное, мотивирующее и подробное резюме, обратившись ко мне по имени '{user_full_name}'. "
        f"Включи в резюме следующие данные и расчеты, представленные в удобном и понятном формате:\n"
        f"**Твои основные параметры:**\n"
        f"- **Возраст:** {profile.get('age', 'не указан')} лет\n"
        f"- **Рост:** {profile.get('height', 'не указан')} см\n"
        f"- **Вес:** {profile.get('weight', 'не указан')} кг\n"
        f"- **Уровень активности:** {profile.get('activity', 'не указан')}\n"
        f"- **Цель:** {profile.get('goal', 'не указан')}\n"
        f"**Твои показатели здоровья:**\n"
        f"- **ИМТ:** {bmi_value:.2f} ({bmi_category})\n"
        f"**Рекомендации по питанию:**\n"
        f"- **Суточная калорийность:** ~{int(tdee)} ккал (для поддержания веса)\n"
        f"- **Рекомендуемая калорийность для цели '{profile.get('goal', 'не указан')}':** ~{int(tdee - 500) if profile.get('goal') == 'похудеть' else (int(tdee + 300) if profile.get('goal') == 'набрать массу' else int(tdee))} ккал\n"
        f"- **БЖУ:** ~{int(protein_g)} г белков, {int(fat_g)} г жиров, {int(carb_g)} г углеводов (с учетом цели)\n"
        f"**Дополнительная информация:**\n"
        f"- Хронические заболевания: {profile.get('diseases', 'нет')}\n"
        f"- Аллергии: {profile.get('allergies', 'нет')}\n\n"
        f"В конце дай 2-3 общих, мотивирующих совета, соответствующих моей цели и уровню активности. "
        f"Избегай лишних вводных фраз типа 'Резюме:'. Начни сразу с обращения. Будь краток."
    )
    
    await update.message.reply_text("Спасибо! Твой профиль заполнен. Сейчас я его анализирую и формирую рекомендации...", reply_markup=ReplyKeyboardRemove())

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Ты персональный ИИ-консьерж, дружелюбный и мотивирующий помощник по здоровью. Ты даешь четкие и понятные рекомендации."},
                {"role": "user", "content": profile_summary_prompt}
            ],
            max_tokens=600,
            temperature=0.7 
        )
        ai_summary = response.choices[0].message.content
        await update.message.reply_text(f"{ai_summary}")

        # Предлагаем дальнейшие действия
        proactive_keyboard = ReplyKeyboardMarkup([
            ["Составить меню", "Составить план тренировок"],
            ["Дневник питания", "Выбрать роль"],
            ["Мои баллы", "К врачу"]
        ], one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            "Отлично! Чем еще могу помочь?",
            reply_markup=proactive_keyboard
        )


    except Exception as e:
        logger.error(f"Error generating profile summary with OpenAI: {e}")
        await update.message.reply_text(
            "Профиль успешно сохранен, но произошла ошибка при генерации резюме или рекомендаций. Возможно, проблемы с API ключом OpenAI или его балансом."
        )

async def cancel_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отменяет процесс заполнения профиля."""
    user_id = update.effective_user.id
    user_data[user_id]["profile_state"] = None
    user_data[user_id]["profile_data"] = {}
    await update.message.reply_text(
        "Заполнение профиля отменено. Ты можешь начать заново в любое время командой `/profile`.",
        reply_markup=ReplyKeyboardRemove()
    )
    logger.info(f"User {user_id} cancelled profile setup.")

# --- Обработчик текстовых сообщений (с OpenAI) ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает текстовые сообщения.
    Если пользователь в процессе заполнения профиля, передает управление handle_profile_response.
    Иначе - использует OpenAI для ответа, учитывая роль и профиль.
    """
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)

    # Если пользователь в процессе заполнения профиля, обрабатываем его ответ
    if user_data[user_id]["profile_state"]:
        await handle_profile_response(update, context)
        return

    message_text = update.message.text.lower()
    
    # Обработка нажатий на кнопки
    if message_text == "заполнить профиль":
        await start_profile(update, context)
        return
    elif message_text == "выбрать роль":
        await set_role(update, context)
        return
    elif message_text == "дневник питания":
        await show_food_diary(update, context)
        return
    elif message_text == "мои баллы":
        await show_score(update, context)
        return
    elif message_text == "о чем говорят цифры?":
        await explain_bmi_vo2max_menu(update, context)
        return
    elif message_text == "составить меню":
        await create_personalized_menu(update, context)
        return
    elif message_text == "составить план тренировок":
        await create_workout_plan(update, context)
        return
    elif message_text == "к врачу":
        await contact_doctor(update, context)
        return
    elif message_text == "продолжить":
        await update.message.reply_text("Хорошо, чем еще могу помочь?", reply_markup=START_KEYBOARD)
        return
    elif message_text in ["что такое имт?", "рассчитать имт"]:
        await explain_bmi(update, context)
        return
    elif message_text in ["что такое мпк?", "рассчитать мпк (приблиз.)"]:
        await explain_vo2max(update, context)
        return
    
    # Если профиль не заполнен, но пользователь что-то пишет, напоминаем
    if not user_data[user_id]["profile_data"].get('goal') and \
       not message_text.startswith('/'):
        await update.message.reply_text(
            "Привет! Чтобы я мог быть максимально полезным, пожалуйста, заполни свой профиль, используя команду `/profile` или нажав кнопку 'Заполнить профиль'.",
            reply_markup=START_KEYBOARD
        )
        return

    # Иначе - обрабатываем как обычное сообщение
    user_message = update.message.text
    current_role_name = user_roles.get(user_id, "личный наставник")
    role_prompt = ROLES.get(current_role_name, ROLES["личный наставник"])
    user_full_name = f"{user_data[user_id].get('first_name', '')} {user_data[user_id].get('last_name', '')}".strip()

    personal_info_prompt = get_personal_prompt(user_data[user_id]["profile_data"], user_full_name)
    
    full_prompt_content = (
        f"Твоя текущая роль: {role_prompt}. "
        f"{personal_info_prompt} "
        f"Ответь на запрос пользователя, соблюдая свою роль. Запрос: {user_message}"
    )

    await update.message.reply_text("Думаю...", reply_markup=ReplyKeyboardRemove()) 

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Ты ИИ-помощник, который адаптирует свой стиль общения под заданную роль и учитывает профиль пользователя. Будь краток и по существу."},
                {"role": "user", "content": full_prompt_content}
            ],
            max_tokens=400,
            temperature=0.7 
        )
        ai_response = response.choices[0].message.content
        await update.message.reply_text(ai_response)
        logger.info(f"User {user_id} ({current_role_name} role) sent: {user_message}. AI response: {ai_response}")

    except Exception as e:
        logger.error(f"Error calling OpenAI API for text message: {e}")
        await update.message.reply_text(
            f"Извини, я не могу ответить сейчас как **{current_role_name.capitalize()}**. Произошла ошибка. Проверь логи или API ключ OpenAI."
        )


# --- Обработчик фотографий (с OpenAI Vision) ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает фотографии еды с использованием OpenAI Vision."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)

    photo_file = update.message.photo[-1]
    file_id = photo_file.file_id
    
    await update.message.reply_text("Позволь мне рассмотреть твою фотографию...", reply_markup=ReplyKeyboardRemove())

    try:
        file_obj = await context.bot.get_file(file_id)
        photo_bytes = await file_obj.download_as_bytes()
        
        base64_image = encode_image(photo_bytes)

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "content": "Что изображено на этой фотографии? Это еда? Если да, попытайся определить блюдо и кратко оценить его состав (например, 'салат с курицей', 'жареная картошка', 'фрукт'). Не пытайся считать калории, просто опиши еду. Если это не еда, так и скажи. Ответь кратко."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                    ],
                }
            ],
            max_tokens=150,
        )
        
        food_description = response.choices[0].message.content
        
        save_food_entry_to_db(user_id, food_description)

        await update.message.reply_text(f"Я думаю, это: *{food_description}*. Добавлено в твой дневник. Ты можешь посмотреть его, используя команду /diary.")
        logger.info(f"User {user_id} uploaded photo. AI described: {food_description}")

    except Exception as e:
        logger.error(f"Error calling OpenAI Vision API for photo: {e}")
        await update.message.reply_text(
            "Извини, я не смог проанализировать фотографию. Возможные причины:\n"
            "- Проблемы с интернет-соединением.\n"
            "- Закончился лимит или баланс на вашем аккаунте OpenAI.\n"
            "- Временные неполадки с API OpenAI.\n"
            "Пожалуйста, проверьте ваш API-ключ OpenAI и баланс на platform.openai.com."
        )

# --- Обработчики для системы баллов ---

async def add_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Добавляет баллы пользователю."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)
    try:
        score_to_add = int(context.args[0])
        user_scores[user_id] += score_to_add
        save_user_score_to_db(user_id, user_scores[user_id])
        await update.message.reply_text(
            f"Добавлено {score_to_add} баллов. Твой текущий счет: {user_scores[user_id]}."
        )
        logger.info(f"User {user_id} added {score_to_add} points. Total: {user_scores[user_id]}.")
    except (IndexError, ValueError):
        await update.message.reply_text("Пожалуйста, укажи, сколько баллов добавить. Например: `/add_score 10`")

async def show_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает текущее количество баллов пользователя."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)
    await update.message.reply_text(f"Твой текущий счет: {user_scores[user_id]} баллов.")

# --- Обработчики для ИМТ и МПК ---

async def explain_bmi_vo2max_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает кнопки для выбора ИМТ и МПК."""
    menu_keyboard = ReplyKeyboardMarkup([
        ["Что такое ИМТ?", "Рассчитать ИМТ"],
        ["Что такое МПК?", "Рассчитать МПК (приблиз.)"],
        ["Продолжить"]
    ], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Выбери, о чем хочешь узнать больше:", reply_markup=menu_keyboard)


async def explain_bmi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Объясняет что такое ИМТ."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)
    profile = user_data[user_id]["profile_data"]
    user_name = user_data[user_id].get("first_name", "друг")
    
    bmi_info_text = (
        f"Привет, {user_name}!\n\n"
        "**Индекс массы тела (ИМТ)** — это простой показатель, который используется для оценки нормы веса человека по отношению к его росту.\n\n"
        "**Формула ИМТ:** вес (кг) / (рост (м) * рост (м))\n"
        "**Интерпретация:**\n"
        "   - Менее 18.5: Недостаточная масса тела\n"
        "   - 18.5 - 24.9: Нормальная масса тела\n"
        "   - 25.0 - 29.9: Избыточная масса тела (предожирение)\n"
        "   - 30.0 и выше: Ожирение\n"
    )

    if 'height' in profile and 'weight' in profile and profile['height'] and profile['weight']:
        height_m = profile['height'] / 100
        bmi_value = profile['weight'] / (height_m ** 2)
        bmi_info_text += f"\nПо данным твоего профиля, твой ИМТ: **{bmi_value:.2f}**.\n"
        if bmi_value < 18.5: bmi_info_text += "Это указывает на недостаточную массу тела."
        elif 18.5 <= bmi_value < 24.9: bmi_info_text += "Это в пределах нормальной массы тела."
        elif 25 <= bmi_value < 29.9: bmi_info_text += "Это указывает на избыточную массу тела."
        else: bmi_info_text += "Это указывает на ожирение."
    else:
        bmi_info_text += "\nЧтобы я мог рассчитать твой ИМТ, пожалуйста, заполни свой профиль командой `/profile`."
    
    await update.message.reply_text(bmi_info_text, reply_markup=ReplyKeyboardRemove())

async def explain_vo2max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Объясняет что такое МПК (VO2max)."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)
    user_name = user_data[user_id].get("first_name", "друг")

    vo2max_info_text = (
        f"Привет, {user_name}!\n\n"
        "**Максимальное потребление кислорода (МПК или VO2max)** — это показатель максимального количества кислорода, которое ваше тело может использовать во время интенсивных физических нагрузок. Это один из лучших индикаторов аэробной выносливости и общей физической формы.\n\n"
        "**Почему это важно?**\n"
        "   - Высокий МПК означает, что ваше сердце, легкие и мышцы работают эффективно, доставляя кислород туда, где он нужен.\n"
        "   - Это показатель сердечно-сосудистого здоровья и выносливости.\n\n"
        "**Как измеряется?**\n"
        "Наиболее точные измерения проводятся в лаборатории, но регулярные кардиотренировки помогут его улучшить!"
    )
    await update.message.reply_text(vo2max_info_text, reply_markup=ReplyKeyboardRemove())

async def create_personalized_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Генерирует персональное меню на основе данных профиля."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)
    profile = user_data[user_id]["profile_data"]
    user_name = user_data[user_id].get("first_name", "пользователь")
    
    if not profile or not profile.get('goal'):
        await update.message.reply_text("Для составления меню мне нужен твой профиль. Пожалуйста, заполни его командой `/profile`.", reply_markup=START_KEYBOARD)
        return

    await update.message.reply_text("Отлично! Составляю для тебя примерное меню...", reply_markup=ReplyKeyboardRemove())

    personal_info_prompt = get_personal_prompt(profile, user_name)
    menu_prompt = (
        f"Ты — профессиональный нутрициолог. Используя данные профиля пользователя, "
        f"составь примерное меню на один день (завтрак, обед, ужин) с учетом цели пользователя и его пищевых ограничений. "
        f"{personal_info_prompt} "
        f"Включи в меню 2-3 конкретных блюда на каждый прием пищи, с указанием примерно БЖУ и калорийности. Меню должно быть разнообразным. "
        f"Ответь в дружелюбном, профессиональном тоне, не забывая обращаться по имени. "
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Ты — профессиональный нутрициолог, который дает четкие и понятные рекомендации по питанию."},
                {"role": "user", "content": menu_prompt}
            ],
            max_tokens=600,
            temperature=0.7 
        )
        menu_response = response.choices[0].message.content
        await update.message.reply_text(menu_response)
        await update.message.reply_text("Надеюсь, это поможет! Чем еще могу помочь?", reply_markup=START_KEYBOARD)
    except Exception as e:
        logger.error(f"Error generating menu with OpenAI: {e}")
        await update.message.reply_text("Извини, не смог составить меню. Проблемы с AI-сервисом.")

async def create_workout_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Генерирует персональный план тренировок."""
    user_id = update.effective_user.id
    load_user_data_from_db(user_id)
    profile = user_data[user_id]["profile_data"]
    user_name = user_data[user_id].get("first_name", "пользователь")

    if not profile or not profile.get('goal'):
        await update.message.reply_text("Для составления плана тренировок мне нужен твой профиль. Пожалуйста, заполни его командой `/profile`.", reply_markup=START_KEYBOARD)
        return

    await update.message.reply_text("Отлично! Составляю для тебя примерный план тренировок...", reply_markup=ReplyKeyboardRemove())

    personal_info_prompt = get_personal_prompt(profile, user_name)
    workout_prompt = (
        f"Ты — профессиональный фитнес-тренер. Используя данные профиля пользователя, "
        f"составь примерный план тренировок на неделю (3-4 тренировки) с учетом его цели и уровня активности. "
        f"{personal_info_prompt} "
        f"План должен включать тип тренировки (силовая, кардио), примерные упражнения и их количество подходов/повторений. "
        f"Ответь в дружелюбном, мотивирующем тоне. "
    )
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Ты — профессиональный фитнес-тренер, который дает четкие и безопасные рекомендации по тренировкам."},
                {"role": "user", "content": workout_prompt}
            ],
            max_tokens=800,
            temperature=0.7
        )
        workout_response = response.choices[0].message.content
        await update.message.reply_text(workout_response)
        await update.message.reply_text("Удачи с тренировками! Чем еще могу помочь?", reply_markup=START_KEYBOARD)
    except Exception as e:
        logger.error(f"Error generating workout plan with OpenAI: {e}")
        await update.message.reply_text("Извини, не смог составить план тренировок. Проблемы с AI-сервисом.")

async def contact_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выводит информацию для связи с реальным врачом."""
    message_text = (
        "Если тебе нужна персональная консультация или ты хочешь получить точный медицинский диагноз, "
        "важно обратиться к реальному специалисту.\n\n"
        "Мы рекомендуем обратиться к проверенным клиникам или онлайн-сервисам телемедицины, таким как:\n"
        "- [Название сервиса телемедицины 1](https://example.com/telemed1)\n"
        "- [Название клиники](https://example.com/clinic)\n\n"
        "Твой ИИ-консьерж не является заменой профессиональной медицинской консультации."
    )
    await update.message.reply_text(message_text, disable_web_page_preview=True, reply_markup=START_KEYBOARD)


# --- Основная функция запуска бота ---

def main() -> None:
    """Запускает бота."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не найден в .env файле. Бот не может быть запущен.")
        return
    
    if not DATABASE_URL:
        logger.error("DATABASE_URL не найден в переменных окружения. Бот не может быть запущен.")
        return

    # Инициализируем базу данных при запуске бота
    init_db()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("role", set_role))
    application.add_handler(CommandHandler("roles", show_roles))
    application.add_handler(CommandHandler("myrole", get_current_role))
    application.add_handler(CommandHandler("diary", show_food_diary))
    application.add_handler(CommandHandler("profile", start_profile))
    application.add_handler(CommandHandler("cancel_profile", cancel_profile))
    application.add_handler(CommandHandler("add_score", add_score))
    application.add_handler(CommandHandler("score", show_score))
    application.add_handler(CommandHandler("bmi", explain_bmi))
    application.add_handler(CommandHandler("vo2max", explain_vo2max))

    # Обработчик для выбора роли с кнопок
    role_pattern = r"^(" + "|".join([re.escape(label) for label in ROLE_BUTTON_LABELS]) + r")$"
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(role_pattern) & ~filters.COMMAND, handle_role_selection))

    # Обработчик для фотографий
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    
    # Обработчик текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


    logger.info("Bot started polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
