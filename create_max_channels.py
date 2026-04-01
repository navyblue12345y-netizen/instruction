"""
Скрипт создания каналов в MAX через браузер (Playwright).

Установка:
    pip install playwright
    playwright install chromium

Запуск:
    python create_max_channels.py
"""

import asyncio
import csv
import getpass
import json
import time
from playwright.async_api import async_playwright

CHANNELS = [
    "Абакан и Хакасия в MAX",
    "Альметьевск в MAX",
    "Ангарск в MAX",
    "Арзамас в MAX",
    "Армавир в MAX",
    "Артём в MAX",
    "Архангельск в MAX",
    "Астрахань в MAX",
    "Ачинск в MAX",
    "Балаково в MAX",
    "Балашиха в MAX",
    "Барнаул в MAX",
    "Батайск в MAX",
    "Белгород в MAX",
    "Березники в MAX",
    "Бийск в MAX",
    "Благовещенск в MAX",
    "Братск в MAX",
    "Брянск в MAX",
    "Великие Луки в MAX",
    "Великий Новгород в MAX",
    "Видное в MAX",
    "Владивосток в MAX",
    "Владикавказ в MAX",
    "Владимир в MAX",
    "Волгоград в MAX",
    "Волгодонск в MAX",
    "Волжский в MAX",
    "Вологда в MAX",
    "Воронеж в MAX",
    "Воткинск в MAX",
    "Гатчина в MAX",
    "Глазов в MAX",
    "Губкин в MAX",
    "Дзержинск в MAX",
    "Домодедово в MAX",
    "Донецк в MAX",
    "Екатеринбург в MAX",
    "Елец в MAX",
    "Ессентуки в MAX",
    "Железногорск в MAX",
    "Жуковский в MAX",
    "Зеленодольск в MAX",
    "Златоуст в MAX",
    "Иваново в MAX",
    "Ижевск в MAX",
    "Иркутск в MAX",
    "Йошкар-Ола в MAX",
    "Казань в MAX",
    "Калининград в MAX",
    "Калуга в MAX",
    "Каменск-Уральский в MAX",
    "Камышин в MAX",
    "Кемерово в MAX",
    "Киров в MAX",
    "Кисловодск в MAX",
    "Климовск в MAX",
    "Ковров в MAX",
    "Коломна в MAX",
    "Комсомольск-на-Амуре в MAX",
    "Копейск в MAX",
    "Королёв в MAX",
    "Кострома в MAX",
    "Красногорск в MAX",
    "Краснодар в MAX",
    "Красноярск в MAX",
    "Крым в MAX",
    "Курган в MAX",
    "Курск и область в MAX",
    "Кызыл в MAX",
    "Ленинск-Кузнецкий в MAX",
    "Липецк в MAX",
    "Люберцы в MAX",
    "Магнитогорск в MAX",
    "Майкоп в MAX",
    "Междуреченск в MAX",
    "Миасс в MAX",
    "Михайловск в MAX",
    "Москва в MAX",
    "Мурино в MAX",
    "Мурманск в MAX",
    "Муром в MAX",
    "Мытищи в MAX",
    "Набережные Челны в MAX",
    "Нальчик в MAX",
    "Находка в MAX",
    "Невинномысск в MAX",
    "Нефтекамск в MAX",
    "Нефтеюганск в MAX",
    "Нижневартовск в MAX",
    "Нижнекамск в MAX",
    "Нижний Новгород в MAX",
    "Нижний Тагил в MAX",
    "Новокузнецк в MAX",
    "Новокуйбышевск в MAX",
    "Новомосковск в MAX",
    "Новороссийск в MAX",
    "Новосибирск в MAX",
    "Новочебоксарск в MAX",
    "Новочеркасск в MAX",
    "Новый Уренгой в MAX",
    "Ногинск в MAX",
    "Норильск в MAX",
    "Ноябрьск в MAX",
    "Обнинск в MAX",
    "Одинцово в MAX",
    "Октябрьский в MAX",
    "Омск в MAX",
    "Орел в MAX",
    "Оренбург в MAX",
    "Орехово-Зуево в MAX",
    "Орск в MAX",
    "Пенза и область в MAX",
    "Первоуральск в MAX",
    "Пермь в MAX",
    "Петрозаводск в MAX",
    "Петропавловск-Камчатский в MAX",
    "Подольск в MAX",
    "Прокопьевск в MAX",
    "Псков в MAX",
    "Пушкино в MAX",
    "Пятигорск в MAX",
    "Раменское в MAX",
    "Реутов в MAX",
    "Ростов-на-Дону в MAX",
    "Рубцовск в MAX",
    "Рыбинск в MAX",
    "Рязань в MAX",
    "Салават в MAX",
    "Самара в MAX",
    "Санкт-Петербург в MAX",
    "Саранск в MAX",
    "Сарапул в MAX",
    "Саратов | Энгельс в MAX",
    "Саров в MAX",
    "Севастополь в MAX",
    "Северодвинск в MAX",
    "Северск в MAX",
    "Сергиев Посад в MAX",
    "Серпухов в MAX",
    "Сочи в MAX",
    "Ставрополь в MAX",
    "Старый Оскол в MAX",
    "Стерлитамак в MAX",
    "Сургут в MAX",
    "Сызрань в MAX",
    "Сыктывкар в MAX",
    "Таганрог в MAX",
    "Тамбов в MAX",
    "Тверь и область в MAX",
    "Тобольск в MAX",
    "Тольятти в MAX",
    "Томск в MAX",
    "Тула в MAX",
    "Тюмень в MAX",
    "Улан-Удэ в MAX",
    "Ульяновск в MAX",
    "Уссурийск в MAX",
    "Уфа в MAX",
    "Хабаровск в MAX",
    "Ханты-Мансийск в MAX",
    "Химки в MAX",
    "Чебоксары в MAX",
    "Челябинск в MAX",
    "Череповец в MAX",
    "Черкесск в MAX",
    "Чита в MAX",
    "Шахты в MAX",
    "Щёлково в MAX",
    "Электросталь в MAX",
    "Элиста в MAX",
    "Южно-Сахалинск в MAX",
    "Якутск в MAX",
    "Ярославль в MAX",
]


async def create_channels():
    results = []

    async with async_playwright() as p:
        # Используем persistent context — сессия сохраняется между навигациями
        context = await p.chromium.launch_persistent_context(
            user_data_dir="./max_session",
            headless=False,
            args=["--start-maximized"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        print("Открываю MAX...")
        await page.goto("https://max.ru")
        await page.wait_for_timeout(3000)

        print("\nВойди в свой аккаунт MAX в открывшемся браузере.")
        print("После входа вернись сюда и нажми Enter.")
        input(">>> Нажми Enter когда войдёшь в аккаунт: ")

        # Проверяем что залогинены
        current_url = page.url
        print(f"Текущая страница: {current_url}")
        print("Начинаю создание каналов...\n")

        for i, name in enumerate(CHANNELS):
            print(f"[{i+1}/{len(CHANNELS)}] {name}", end=" ... ", flush=True)
            try:
                # Остаёмся на той же странице, ищем кнопку создания
                # Пробуем разные способы найти кнопку создания канала
                
                # Способ 1: через меню/кнопку
                found = False
                
                # Ищем кнопку с текстом "Создать" или иконку
                buttons = await page.query_selector_all("button, [role='button']")
                for btn in buttons:
                    text = await btn.inner_text()
                    if "создать" in text.lower() or "new" in text.lower():
                        await btn.click()
                        await page.wait_for_timeout(1000)
                        found = True
                        break

                if not found:
                    # Пробуем через URL напрямую
                    await page.goto("https://max.ru/channel/create")
                    await page.wait_for_timeout(2000)

                # Ищем поле названия канала
                name_input = None
                for selector in ["input[name='title']", "input[placeholder*='Название']", 
                                  "input[placeholder*='название']", "input[type='text']"]:
                    try:
                        name_input = await page.wait_for_selector(selector, timeout=3000)
                        if name_input:
                            break
                    except Exception:
                        continue

                if name_input:
                    await name_input.clear()
                    await name_input.fill(name)
                    await page.wait_for_timeout(500)
                    
                    # Ищем кнопку подтверждения
                    for selector in ["button[type='submit']", "button:has-text('Создать')", 
                                     "button:has-text('Далее')", "button:has-text('Готово')"]:
                        try:
                            submit = await page.query_selector(selector)
                            if submit:
                                await submit.click()
                                await page.wait_for_timeout(2000)
                                break
                        except Exception:
                            continue

                    url = page.url
                    results.append({"name": name, "status": "OK", "url": url})
                    print(f"✅")
                else:
                    results.append({"name": name, "status": "NO_INPUT", "url": page.url})
                    print(f"⚠️ поле не найдено")
                    # Пауза чтобы посмотреть что происходит
                    await page.wait_for_timeout(2000)

            except Exception as e:
                results.append({"name": name, "status": "ERROR", "url": str(e)[:80]})
                print(f"❌ {str(e)[:60]}")

            await page.wait_for_timeout(1000)

        await context.close()

    # Сохраняем результаты
    with open("channels_created.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "status", "url"])
        writer.writeheader()
        writer.writerows(results)

    ok = sum(1 for r in results if r["status"] == "OK")
    err = sum(1 for r in results if r["status"] == "ERROR")
    print(f"\n✅ Готово! Успешно: {ok}, Ошибок: {err}")
    print("Результаты сохранены в channels_created.csv")


if __name__ == "__main__":
    print("=== Создание каналов в MAX ===")
    print(f"Каналов в списке: {len(CHANNELS)}\n")
    asyncio.run(create_channels())
