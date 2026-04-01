import asyncio
import csv
import json
import time
import requests
from playwright.async_api import async_playwright

TOKEN = "f9LHodD0cOITXuE1vu6eSimAhJUdtmqC00k1Rd10FzHWQwBwK68jPOqfAze4EcRy1jh_5TO04Egv3EXQlWYz"
BOT_NAME = "Контентщик в Макс"

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
    "Междуреченск в MAX", "Миасс в MAX", "Михайловск в MAX", "Москва в MAX",
    "Мурино в MAX", "Мурманск в MAX", "Муром в MAX", "Мытищи в MAX",
    "Набережные Челны в MAX", "Нальчик в MAX", "Находка в MAX",
    "Невинномысск в MAX", "Нефтекамск в MAX", "Нефтеюганск в MAX",
    "Нижневартовск в MAX", "Нижнекамск в MAX", "Нижний Новгород в MAX",
    "Нижний Тагил в MAX", "Новокузнецк в MAX", "Новокуйбышевск в MAX",
    "Новомосковск в MAX", "Новороссийск в MAX", "Новосибирск в MAX",
    "Новочебоксарск в MAX", "Новочеркасск в MAX", "Новый Уренгой в MAX",
    "Ногинск в MAX", "Норильск в MAX", "Ноябрьск в MAX", "Обнинск в MAX",
    "Одинцово в MAX", "Октябрьский в MAX", "Омск в MAX", "Орел в MAX",
    "Оренбург в MAX", "Орехово-Зуево в MAX", "Орск в MAX",
    "Пенза и область в MAX", "Первоуральск в MAX", "Пермь в MAX",
    "Петрозаводск в MAX", "Петропавловск-Камчатский в MAX", "Подольск в MAX",
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


def get_bot_chats():
    """Получаем все чаты где уже есть бот."""
    try:
        r = requests.get(
            "https://botapi.max.ru/chats",
            params={"access_token": TOKEN, "count": 200},
            timeout=10
        )
        return {c["title"]: c["chat_id"] for c in r.json().get("chats", [])}
    except Exception:
        return {}


async def main():
    results = []

    # Получаем уже известные чаты
    print("Получаем текущий список чатов бота...")
    known = get_bot_chats()
    print(f"Бот уже в {len(known)} чатах")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            "./max_session",
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://max.ru/im")
        await page.wait_for_timeout(3000)

        print("Войди в аккаунт MAX.")
        input("После входа нажми Enter: ")

        for i, name in enumerate(CHANNELS):
            print(f"[{i+1}/{len(CHANNELS)}] {name} ... ", end="", flush=True)

            # Если бот уже в канале — пропускаем
            if name in known:
                results.append({"name": name, "chat_id": known[name], "status": "KNOWN"})
                print(f"✅ уже есть (ID: {known[name]})")
                continue

            try:
                # ШАГ 1: Ищем канал
                search = await page.wait_for_selector("input[placeholder='Найти']", timeout=5000)
                await search.click()
                await page.keyboard.press("Control+a")
                await search.fill(name)
                await page.wait_for_timeout(1500)

                # Кликаем на канал в разделе "Новые" (class="cell ...")
                # Элемент: div.cell содержащий нужное название
                clicked = await page.evaluate(f"""
                    () => {{
                        // Ищем div.cell которые содержат название канала
                        const cells = document.querySelectorAll('div[class*="cell"]');
                        for (const cell of cells) {{
                            if (cell.innerText && cell.innerText.includes('{name}') && cell.offsetParent !== null) {{
                                cell.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}
                """)

                if not clicked:
                    print("❌ канал не найден")
                    results.append({"name": name, "chat_id": "", "status": "NOT_FOUND"})
                    await page.keyboard.press("Escape")
                    continue

                await page.wait_for_timeout(1500)

                # ШАГ 2: Кликаем на шапку канала
                # Шапка — элемент содержащий название канала И текст "подписчик"
                header_clicked = await page.evaluate(f"""
                    () => {{
                        const all = document.querySelectorAll('*');
                        for (const el of all) {{
                            if (el.offsetParent !== null &&
                                el.innerText &&
                                el.innerText.includes('{name}') &&
                                el.innerText.includes('подписчик') &&
                                el.childElementCount > 0) {{
                                el.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}
                """)
                if not header_clicked:
                    # Запасной: кликаем по координатам верхней панели чата
                    await page.mouse.click(750, 45)
                await page.wait_for_timeout(1500)

                # Скриншот для отладки (только первый канал)
                if i == 0:
                    await page.screenshot(path="debug_step2.png")
                    print("📸 debug_step2.png")

                # ШАГ 3: Кликаем "Подписчики" — ищем по тексту среди всех кликабельных
                sub_clicked = await page.evaluate("""
                    () => {
                        const all = document.querySelectorAll('button, div[role="button"], a, li');
                        for (const el of all) {
                            if (el.offsetParent !== null && el.innerText && el.innerText.trim() === 'Подписчики') {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)

                if not sub_clicked:
                    if i == 0:
                        await page.screenshot(path="debug_no_subscribers.png")
                        print("📸 debug_no_subscribers.png")
                    print("❌ не нашёл 'Подписчики'")
                    results.append({"name": name, "chat_id": "", "status": "NO_SUBSCRIBERS_BTN"})
                    await page.keyboard.press("Escape")
                    await page.keyboard.press("Escape")
                    continue

                await page.wait_for_timeout(1000)

                # ШАГ 4: Кликаем "Добавить участников" (button.cell--clickable с таким текстом)
                add_clicked = await page.evaluate("""
                    () => {
                        const btns = document.querySelectorAll('button[class*="cell--clickable"]');
                        for (const btn of btns) {
                            if (btn.innerText && btn.innerText.includes('Добавить участников')) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)

                if not add_clicked:
                    print("❌ не нашёл 'Добавить участников'")
                    results.append({"name": name, "chat_id": "", "status": "NO_ADD_BTN"})
                    await page.keyboard.press("Escape")
                    await page.keyboard.press("Escape")
                    continue

                await page.wait_for_timeout(1000)

                # ШАГ 5: Выбираем "Контентщик в Макс" из списка (div.item с текстом)
                bot_clicked = await page.evaluate(f"""
                    () => {{
                        const items = document.querySelectorAll('div[class*="item"]');
                        for (const item of items) {{
                            if (item.innerText && item.innerText.includes('{BOT_NAME}')) {{
                                item.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}
                """)

                if not bot_clicked:
                    print("❌ не нашёл бота в списке")
                    results.append({"name": name, "chat_id": "", "status": "NO_BOT"})
                    await page.keyboard.press("Escape")
                    await page.keyboard.press("Escape")
                    continue

                await page.wait_for_timeout(500)

                # ШАГ 6: Нажимаем кнопку "Добавить" (aria-label="Добавить")
                add_btn_clicked = await page.evaluate("""
                    () => {
                        const btn = document.querySelector('button[aria-label="Добавить"]');
                        if (btn) { btn.click(); return true; }
                        // запасной — по тексту span внутри кнопки
                        const btns = document.querySelectorAll('button');
                        for (const b of btns) {
                            if (b.innerText && b.innerText.trim() === 'Добавить') {
                                b.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                await page.wait_for_timeout(2000)

                # Закрываем панели
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)

                # Получаем chat_id через API
                await page.wait_for_timeout(1500)
                fresh = get_bot_chats()
                chat_id = fresh.get(name, "")
                known.update(fresh)

                if chat_id:
                    results.append({"name": name, "chat_id": chat_id, "status": "OK"})
                    print(f"✅ ID: {chat_id}")
                else:
                    results.append({"name": name, "chat_id": "", "status": "NO_ID"})
                    print("⚠️ добавлен, но ID не получен пока")

            except Exception as e:
                results.append({"name": name, "chat_id": "", "status": f"ERR: {str(e)[:60]}"})
                print(f"❌ {str(e)[:50]}")
                await page.keyboard.press("Escape")

            time.sleep(1)

        await ctx.close()

    # Сохраняем
    with open("channels_ids.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["name", "chat_id", "status"])
        w.writeheader()
        w.writerows(results)

    with open("channels_ids.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    ok = sum(1 for r in results if r["chat_id"])
    print(f"\nГотово! Получено ID: {ok}/{len(CHANNELS)}")
    print("Файлы: channels_ids.csv и channels_ids.json")


asyncio.run(main())
