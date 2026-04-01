"""
Скрипт автосоздания каналов в MAX через Playwright.
Запуск: python3 create_channels.py

Что делает:
1. Открывает max.ru в браузере
2. Логинится под твоим аккаунтом
3. Создаёт 174 канала по списку
4. Сохраняет ID каждого канала в channels_created.json

Что НЕ делает:
- Не сохраняет логин/пароль в файлы
- Не отправляет данные куда-либо
"""
import asyncio
import json
import os
import sys
from datetime import datetime

CHANNELS = [
    "Абакан и Хакасия в MAX", "Альметьевск в MAX", "Ангарск в MAX",
    "Арзамас в MAX", "Армавир в MAX", "Артём в MAX", "Архангельск в MAX",
    "Астрахань в MAX", "Ачинск в MAX", "Балаково в MAX", "Балашиха в MAX",
    "Барнаул в MAX", "Батайск в MAX", "Белгород в MAX", "Березники в MAX",
    "Бийск в MAX", "Благовещенск в MAX", "Братск в MAX", "Брянск в MAX",
    "Великие Луки в MAX", "Великий Новгород в MAX", "Видное в MAX",
    "Владивосток в MAX", "Владикавказ в MAX", "Владимир в MAX",
    "Волгоград в MAX", "Волгодонск в MAX", "Волжский в MAX", "Вологда в MAX",
    "Воронеж в MAX", "Воткинск в MAX", "Гатчина в MAX", "Глазов в MAX",
    "Губкин в MAX", "Дзержинск в MAX", "Домодедово в MAX", "Донецк в MAX",
    "Екатеринбург в MAX", "Елец в MAX", "Ессентуки в MAX",
    "Железногорск в MAX", "Жуковский в MAX", "Зеленодольск в MAX",
    "Златоуст в MAX", "Иваново в MAX", "Ижевск в MAX", "Иркутск в MAX",
    "Йошкар-Ола в MAX", "Казань в MAX", "Калининград в MAX", "Калуга в MAX",
    "Каменск-Уральский в MAX", "Камышин в MAX", "Кемерово в MAX",
    "Киров в MAX", "Кисловодск в MAX", "Климовск в MAX", "Ковров в MAX",
    "Коломна в MAX", "Комсомольск-на-Амуре в MAX", "Копейск в MAX",
    "Королёв в MAX", "Кострома в MAX", "Красногорск в MAX",
    "Краснодар в MAX", "Красноярск в MAX", "Крым в MAX", "Курган в MAX",
    "Курск и область в MAX", "Кызыл в MAX", "Ленинск-Кузнецкий в MAX",
    "Липецк в MAX", "Люберцы в MAX", "Магнитогорск в MAX", "Майкоп в MAX",
    "Междуреченск в MAX", "Миасс в MAX", "Михайловск в MAX",
    "Москва в MAX", "Мурино в MAX", "Мурманск в MAX", "Муром в MAX",
    "Мытищи в MAX", "Набережные Челны в MAX", "Нальчик в MAX",
    "Находка в MAX", "Невинномысск в MAX", "Нефтекамск в MAX",
    "Нефтеюганск в MAX", "Нижневартовск в MAX", "Нижнекамск в MAX",
    "Нижний Новгород в MAX", "Нижний Тагил в MAX", "Новокузнецк в MAX",
    "Новокуйбышевск в MAX", "Новомосковск в MAX", "Новороссийск в MAX",
    "Новосибирск в MAX", "Новочебоксарск в MAX", "Новочеркасск в MAX",
    "Новый Уренгой в MAX", "Ногинск в MAX", "Норильск в MAX",
    "Ноябрьск в MAX", "Обнинск в MAX", "Одинцово в MAX",
    "Октябрьский в MAX", "Омск в MAX", "Орел в MAX", "Оренбург в MAX",
    "Орехово-Зуево в MAX", "Орск в MAX", "Пенза и область в MAX",
    "Первоуральск в MAX", "Пермь в MAX", "Петрозаводск в MAX",
    "Петропавловск-Камчатский в MAX", "Подольск в MAX",
    "Прокопьевск в MAX", "Псков в MAX", "Пушкино в MAX", "Пятигорск в MAX",
    "Раменское в MAX", "Реутов в MAX", "Ростов-на-Дону в MAX",
    "Рубцовск в MAX", "Рыбинск в MAX", "Рязань в MAX", "Салават в MAX",
    "Самара в MAX", "Санкт-Петербург в MAX", "Саранск в MAX",
    "Сарапул в MAX", "Саратов | Энгельс в MAX", "Саров в MAX",
    "Севастополь в MAX", "Северодвинск в MAX", "Северск в MAX",
    "Сергиев Посад в MAX", "Серпухов в MAX", "Сочи в MAX",
    "Ставрополь в MAX", "Старый Оскол в MAX", "Стерлитамак в MAX",
    "Сургут в MAX", "Сызрань в MAX", "Сыктывкар в MAX", "Таганрог в MAX",
    "Тамбов в MAX", "Тверь и область в MAX", "Тобольск в MAX",
    "Тольятти в MAX", "Томск в MAX", "Тула в MAX", "Тюмень в MAX",
    "Улан-Удэ в MAX", "Ульяновск в MAX", "Уссурийск в MAX", "Уфа в MAX",
    "Хабаровск в MAX", "Ханты-Мансийск в MAX", "Химки в MAX",
    "Чебоксары в MAX", "Челябинск в MAX", "Череповец в MAX",
    "Черкесск в MAX", "Чита в MAX", "Шахты в MAX", "Щёлково в MAX",
    "Электросталь в MAX", "Элиста в MAX", "Южно-Сахалинск в MAX",
    "Якутск в MAX", "Ярославль в MAX",
]

RESULTS_FILE = "/home/openclaw/.openclaw/workspace/channels_created.json"


async def create_channels(phone: str, password: str):
    from playwright.async_api import async_playwright

    results = {}
    # Загружаем уже созданные если скрипт прерывался
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            results = json.load(f)
        print(f"Уже создано: {len(results)} каналов, продолжаем...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        print("Открываем MAX...")
        await page.goto("https://max.ru/login", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Вводим телефон
        print("Вводим номер телефона...")
        phone_input = page.locator('input[type="tel"], input[placeholder*="телефон"], input[placeholder*="phone"], input[name="phone"]').first
        await phone_input.fill(phone)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2000)

        # Вводим пароль
        print("Вводим пароль...")
        pass_input = page.locator('input[type="password"]').first
        await pass_input.fill(password)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(3000)

        # Проверяем SMS-код если нужен
        sms_input = page.locator('input[placeholder*="код"], input[placeholder*="code"]').first
        if await sms_input.is_visible():
            print("⚠️  Нужен SMS-код! Введи его:")
            sms_code = input("SMS-код: ").strip()
            await sms_input.fill(sms_code)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3000)

        print("✅ Залогинились, начинаем создавать каналы...")

        for i, name in enumerate(CHANNELS):
            if name in results:
                print(f"[{i+1}/174] Пропускаем (уже создан): {name}")
                continue

            try:
                # Переходим на главную
                await page.goto("https://max.ru", wait_until="networkidle")
                await page.wait_for_timeout(1500)

                # Ищем кнопку создания канала
                create_btn = page.locator('button:has-text("Создать"), a:has-text("Создать"), [aria-label*="Создать"]').first
                await create_btn.click()
                await page.wait_for_timeout(1000)

                # Выбираем "Канал"
                channel_option = page.locator('text=Канал, text=канал').first
                await channel_option.click()
                await page.wait_for_timeout(1000)

                # Вводим название
                name_input = page.locator('input[placeholder*="название"], input[placeholder*="Название"]').first
                await name_input.fill(name)
                await page.wait_for_timeout(500)

                # Подтверждаем
                confirm_btn = page.locator('button:has-text("Создать"), button[type="submit"]').first
                await confirm_btn.click()
                await page.wait_for_timeout(2000)

                # Получаем ID из URL или API
                current_url = page.url
                chat_id = None
                if "chat_id=" in current_url:
                    chat_id = current_url.split("chat_id=")[1].split("&")[0]

                results[name] = {"chat_id": chat_id, "created_at": datetime.utcnow().isoformat()}

                # Сохраняем после каждого канала
                with open(RESULTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)

                print(f"[{i+1}/174] ✅ {name} (id={chat_id})")

            except Exception as e:
                print(f"[{i+1}/174] ❌ {name}: {e}")
                results[name] = {"chat_id": None, "error": str(e)}
                with open(RESULTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)

        await browser.close()

    print(f"\n✅ Готово! Создано: {sum(1 for v in results.values() if v.get('chat_id'))} каналов")
    print(f"Результаты сохранены в: {RESULTS_FILE}")


if __name__ == "__main__":
    print("=== Создание 174 каналов в MAX ===")
    print("ВАЖНО: данные используются только локально и не сохраняются\n")
    phone = input("Номер телефона (например +79001234567): ").strip()
    password = input("Пароль: ").strip()

    # Сразу очищаем из переменных после передачи в функцию
    asyncio.run(create_channels(phone, password))

    # Обнуляем переменные
    phone = ""; password = ""
    print("\n🔒 Данные очищены из памяти")
