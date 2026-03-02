import telebot
import threading
from flask import Flask
from telebot import types
from datetime import datetime, timedelta

# --- ВЕБ-СЕРВЕР ДЛЯ ОБХОДА СНА ХОСТИНГА ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот жив и работает! 🤖"

def run_web_server():
    # Сервер будет работать на порту 8080
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    """Запускает веб-сервер в отдельном потоке, чтобы он не мешал боту"""
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

# СЮДА ВСТАВЬ ТОКЕН ОТ BOTFATHER
TOKEN = '6399918831:AAGtVsAxmXTtmnKzF_iNpe3vcVfQz5Q1cnA'
bot = telebot.TeleBot(TOKEN)

# --- ТВОИ ДАННЫЕ РАСПИСАНИЯ ---
upper_week_schedule = {
    'Понедельник': ['1. Иностранный язык (Пр.) Ауд. 11.243, 244   08:00-09:35',
                    '2. Моделирование узлов КС (Лек.) Ауд. 2.234 +КИ-24    09:55-11:30',
                    '3. Организация БД (Лек.) Ауд. 2.234 +КИ-24, КСЦ-24    11:50-13:25',
                    '4. ООП (Лаб.) Ауд. 4.037 2 подгруппа    13:45-15:20'],
    'Вторник': ['1. Философия (Лек.) Ауд. 1.301 + ИИ-24, КИ-24, КСЦ-24   08:00-09:35',
                '2. Архитектура компьютеров (Лек.) Ауд. 2.234 + КИ-24, КСЦ-24    09:55-11:30',
                '3. Анал. схемотехника (Лаб.) Ауд. 4.019 1 подгруппа  11:50-13:25'],

    'Среда': ['1. * 08:00-09:35',
              '2. Выч. мат. (Пр.) Ауд. 4.003а    09:55-11:30',
              '3. Выч. мат. (Лек.) Ауд. 2.234 + КИ-24, КСЦ-24    11:50-13:25',
              '4. Организация БД (Лаб.) Ауд. 4.019    13:45-15:20'],
    'Четверг': ['1. * 08:00-09:35',
                '2. ООП (Лек.) Ауд. 2.234 + КИ-24    09:55-11:30',
                '3. Моделирование узлов КС (Лаб.) Ауд. 4.014 1 подгруппа    11:50-13:25',
                '4. Моделирование узлов КС (Лаб.) Ауд. 4.014 2 подгруппа    13:45-15:20'],
    'Пятница': ['1. Анал. схемотехника (Лаб.) Ауд. 4.019 2 подгруппа    08:00-09:35',
                '2. Конструирование КС (Лек.) Ауд. 2.234 + КИ-24, КСЦ-24    09:55-11:30',
                '3. Цифр. схемотехника (Лек.) Ауд. 2.234 + КИ-24    11:50-13:25',
                '4. ООП (Лаб.) Ауд. 4.037 1 подгруппа    13:45-15:20',
                '5. КП "Архитектура компьютеров" (Конс.) Ауд. 4.014а    15:30-17:05']
}

lower_week_schedule = {
    'Понедельник': ['1. Иностранный язык (Пр.) Ауд. 11.243, 244    08:00-09:35',
                    '2. Конструирование КС (Лаб.) Ауд. 4.024 2 подгруппа    09:55-11:30',
                    '3. Организация БД (Лек.) Ауд. 2.234 +КИ-24, КСЦ-24    11:50-13:25',
                    '4. ООП (Лаб.) Ауд. 4.037 2 подгруппа    13:45-15:20'],
    'Вторник': ['1. Анал. схемотехника (Лаб.) Ауд. 4.019 1 подгруппа    08:00-09:35',
                '2. Архитектура компьютеров (Лек.) Ауд. 2.234 + КИ-24, КСЦ-24    09:55-11:30',
                '3. Философия (Лаб.) Ауд. 1.411    11:50-13:25',
                '4. Цифр. схемотехника (Лаб.) Ауд. 4.040 2 подгруппа    13:45-15:20'],
    'Среда': ['1. Анал. схемотехника (Лаб.) Ауд. 4.019 1 подгруппа    08:00-09:35',
              '2. Выч. мат. (Пр.) Ауд. 4.003а    09:55-11:30',
              '3. Выч. мат. (Лек.) Ауд. 2.234 + КИ-24, КСЦ-24    11:50-13:25',
              '4. Организация БД (Лаб.) Ауд. 4.019    13:45-15:20'],
    'Четверг': ['1. * 08:00-09:35',
                '2. ООП (Лек.) Ауд. 2.234 + КИ-24    09:55-11:30',
                '3. Архитектура компьютеров (Лаб.) Ауд. 4.014а 2 подгруппа    11:50-13:25',
                '4. Архитектура компьютеров (Лаб.) Ауд. 4.014а 1 подгруппа    13:45-15:20'],
    'Пятница': [
        '1. Анал. схемотехника (Лаб.) 2 подгруппа Ауд. 4.019  / ККС (Лаб.) 1 подгруппа Ауд. 4.024    08:00-09:35',
        '2. Цифр. схемотехника (Лаб.) Ауд. 4.040 1 подгруппа    09:55-11:30',
        '3. Анал. схемотехника (Лек.) Ауд. 2.234 + КИ-24   11:50-13:25',
        '4. ООП (Лаб.) Ауд. 4.037 1 подгруппа    13:45-15:20']
}

days_of_week = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']


# --- ФУНКЦИИ ЛОГИКИ ---
def get_week_type(date_obj):
    """Определяет тип недели (верхняя/нижняя) для конкретной даты"""
    week_number = date_obj.isocalendar()[1]
    return "upper" if week_number % 2 == 1 else "lower"


def format_schedule(day_name, schedule_data, week_type_ru):
    """Красиво форматирует расписание для отправки в Телеграм"""
    if isinstance(schedule_data, str):
        # Если это строка (например, выходной)
        return f"📅 {day_name} ({week_type_ru} неделя):\n\n{schedule_data}"

    if not schedule_data:
        return f"📅 {day_name} ({week_type_ru} неделя):\n\nПар нет! Отдыхаем 🥳"

    # Если это список пар, склеиваем их с переносом строки
    text = f"📅 {day_name} ({week_type_ru} неделя):\n\n"
    text += "\n\n".join(schedule_data)
    return text

# --- ИНЛАЙН КЛАВИАТУРА ---
def get_inline_keyboard():
    # Создаем клавиатуру
    markup = types.InlineKeyboardMarkup()

    # Создаем кнопки. Параметр callback_data - это то, что бот получит "под капотом" при нажатии
    btn_today = types.InlineKeyboardButton('Сегодня', callback_data='day_today')
    btn_tomorrow = types.InlineKeyboardButton('Завтра', callback_data='day_tomorrow')

    btn_pn = types.InlineKeyboardButton('Пн', callback_data='day_Понедельник')
    btn_vt = types.InlineKeyboardButton('Вт', callback_data='day_Вторник')
    btn_sr = types.InlineKeyboardButton('Ср', callback_data='day_Среда')
    btn_ch = types.InlineKeyboardButton('Чт', callback_data='day_Четверг')
    btn_pt = types.InlineKeyboardButton('Пт', callback_data='day_Пятница')

    # Компонуем по рядам (1 ряд: сегодня/завтра, 2 ряд: Пн-Ср, 3 ряд: Чт-Пт)
    markup.row(btn_today, btn_tomorrow)
    markup.row(btn_pn, btn_vt, btn_sr)
    markup.row(btn_ch, btn_pt)

    return markup


# --- ОБРАБОТЧИКИ ---

@bot.message_handler(commands=['start'])
def start_message(message):
    bot.send_message(
        message.chat.id,
        "Привет! Я бот с расписанием. Выбери день на клавиатуре ниже 👇",
        reply_markup=get_inline_keyboard()
    )


# Специальный обработчик для inline-кнопок
# Специальный обработчик для inline-кнопок
@bot.callback_query_handler(func=lambda call: call.data.startswith('day_'))
def handle_schedule_query(call):
    # Получаем то, что было после 'day_' (например, 'today' или 'Понедельник')
    action = call.data.split('_')[1]

    # ПЕЧАТАЕМ В КОНСОЛЬ PYCHARM ДЛЯ ПРОВЕРКИ
    print(f"Пользователь нажал кнопку: {action}")

    today_date = datetime.today()

    if action == 'today':
        target_date = today_date
        day_name = days_of_week[target_date.weekday()]
    elif action == 'tomorrow':
        target_date = today_date + timedelta(days=1)
        day_name = days_of_week[target_date.weekday()]
    else:
        target_date = today_date
        day_name = action  # 'Понедельник', 'Вторник' и т.д.

    # Определяем тип недели
    week_type = get_week_type(target_date)
    week_type_ru = "Верхняя" if week_type == "upper" else "Нижняя"

    # Получаем нужное расписание
    if week_type == "upper":
        schedule_data = upper_week_schedule.get(day_name, [])
    else:
        schedule_data = lower_week_schedule.get(day_name, [])

    response_text = format_schedule(day_name, schedule_data, week_type_ru)

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=response_text,
            reply_markup=get_inline_keyboard()
        )
    except telebot.apihelper.ApiTelegramException:
        pass

    # ДОБАВИЛИ ВСПЛЫВАЮЩУЮ ПОДСКАЗКУ В ТЕЛЕГРАМЕ
    bot.answer_callback_query(call.id, text=f"Загружено: {day_name} 🗓")


# --- ЗАПУСК БОТА ---
if __name__ == '__main__':
    keep_alive() # Сначала запускаем веб-сервер
    print("Бот запущен и готов к работе!")
    bot.polling(none_stop=True) # Затем запускаем самого бота