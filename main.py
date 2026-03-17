import os
import telebot
from flask import Flask, request, abort
from telebot import types
from datetime import datetime, timedelta

# --- НАСТРОЙКИ БОТА И WEBHOOK ---
TOKEN = '6399918831:AAGtVsAxmXTtmnKzF_iNpe3vcVfQz5Q1cnA'
WEBHOOK_URL = 'https://shedule-bj3d.onrender.com'

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

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
    week_number = date_obj.isocalendar()[1]
    return "upper" if week_number % 2 == 1 else "lower"

def format_schedule(day_name, schedule_data, week_type_ru):
    if isinstance(schedule_data, str):
        return f"📅 {day_name} ({week_type_ru} неделя):\n\n{schedule_data}"
    if not schedule_data:
        return f"📅 {day_name} ({week_type_ru} неделя):\n\nПар нет! Отдыхаем 🥳"
    text = f"📅 {day_name} ({week_type_ru} неделя):\n\n"
    text += "\n\n".join(schedule_data)
    return text

# --- ИНЛАЙН КЛАВИАТУРА ---
def get_inline_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_today = types.InlineKeyboardButton('Сегодня', callback_data='day_today')
    btn_tomorrow = types.InlineKeyboardButton('Завтра', callback_data='day_tomorrow')
    btn_pn = types.InlineKeyboardButton('Пн', callback_data='day_Понедельник')
    btn_vt = types.InlineKeyboardButton('Вт', callback_data='day_Вторник')
    btn_sr = types.InlineKeyboardButton('Ср', callback_data='day_Среда')
    btn_ch = types.InlineKeyboardButton('Чт', callback_data='day_Четверг')
    btn_pt = types.InlineKeyboardButton('Пт', callback_data='day_Пятница')
    
    markup.row(btn_today, btn_tomorrow)
    markup.row(btn_pn, btn_vt, btn_sr)
    markup.row(btn_ch, btn_pt)
    return markup

# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ ---
@bot.message_handler(commands=['start'])
def start_message(message):
    bot.send_message(
        message.chat.id,
        "Привет! Я бот с расписанием. Выбери день на клавиатуре ниже 👇",
        reply_markup=get_inline_keyboard()
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('day_'))
def handle_schedule_query(call):
    action = call.data.split('_')[1]
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
        day_name = action 

    week_type = get_week_type(target_date)
    week_type_ru = "Верхняя" if week_type == "upper" else "Нижняя"

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

    bot.answer_callback_query(call.id, text=f"Загружено: {day_name} 🗓")

# --- РОУТЫ FLASK ДЛЯ WEBHOOK ---
@app.route('/', methods=['GET'])
def index():
    return "Бот жив и работает через Webhook! 🤖"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    else:
        abort(403)
        
# --- ЗАПУСК СЕРВЕРА ---
if __name__ == '__main__':
    # Убрали запросы к Telegram, чтобы сервер стартовал моментально
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
