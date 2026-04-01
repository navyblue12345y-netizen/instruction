import asyncio
import csv
import time
from playwright.async_api import async_playwright

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

##############################################
# МЕНЯЙ ЭТО ЧИСЛО ПРИ ПЕРЕЗАПУСКЕ:
START_FROM = 93  # продолжаем с 94-го города
BATCH_SIZE = 19  # каналов за раз
BATCH_PAUSE = 420  # секунд паузы (7 минут)
##############################################


async def close_error_dialog(page):
    """Закрывает диалог ошибки если он есть. Возвращает True если закрыл."""
    try:
        # Ищем кнопку закрытия ошибки
        for label in ["Закрыть", "OK", "Ок", "Понятно"]:
            try:
                btn = page.get_by_role("button", name=label)
                if await btn.count() > 0:
                    await btn.click()
                    await page.wait_for_timeout(500)
                    return True
            except Exception:
                pass
        # Также пробуем по aria-label
        try:
            btn = await page.query_selector("[aria-label='Закрыть']")
            if btn and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(500)
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False


async def wait_for_limit_reset(page):
    """Ждёт сброса лимита MAX — проверяет каждые 30 сек."""
    print(f"\n🚫 Лимит MAX — жду сброса (до 7 минут)...")
    await close_error_dialog(page)
    for minute in range(14):
        print(f"   ожидание... {(minute+1)*30} сек", end="\r")
        await page.wait_for_timeout(30000)
        # Проверяем — пробуем кликнуть кнопку создания
        try:
            btn = await page.query_selector("[aria-label='Начать общение']")
            if btn and await btn.is_visible():
                print(f"\n✅ Лимит сброшен, продолжаю!")
                return True
        except Exception:
            pass
    print("\n⚠️ Продолжаю несмотря на лимит...")
    return False


async def create_channel(page, name):
    """Создаёт один канал. Возвращает (success, url)."""
    # Закрываем любые открытые диалоги
    await close_error_dialog(page)
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(500)

    # 1. Кликаем "Начать общение"
    btn = await page.wait_for_selector("[aria-label='Начать общение']", timeout=10000)
    await btn.click()
    await page.wait_for_timeout(1000)

    # 2. Ждём меню "Создать приватный канал"
    menu_item = None
    try:
        menu_item = await page.wait_for_selector("text='Создать приватный канал'", timeout=5000)
    except Exception:
        # Попробуем через JS
        try:
            el = await page.evaluate_handle("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        if (el.children.length === 0 && el.innerText && el.innerText.trim() === 'Создать приватный канал') {
                            return el;
                        }
                    }
                    return null;
                }
            """)
            if el:
                menu_item = el
        except Exception:
            pass

    if not menu_item:
        await page.keyboard.press("Escape")
        return False, ""

    await menu_item.click()
    await page.wait_for_timeout(1500)

    # 3. Вводим название
    name_input = await page.wait_for_selector("input[placeholder='Название']", timeout=5000)
    await name_input.click()
    await page.keyboard.press("Control+a")
    await name_input.fill(name)
    await page.wait_for_timeout(500)

    # 4. Нажимаем "Создать"
    create_btn = await page.wait_for_selector("[aria-label='Создать']", timeout=5000)
    await create_btn.click()
    await page.wait_for_timeout(2500)

    # Проверяем ошибку лимита
    try:
        err_text = await page.evaluate("""
            () => document.body.innerText.includes('too-many-chat-created') ||
                  document.body.innerText.includes('Превышен лимит') ||
                  document.body.innerText.includes('слишком много')
        """)
        if err_text:
            return None, ""  # None = лимит
    except Exception:
        pass

    # 5. Нажимаем "Пропустить"
    for _ in range(10):
        await page.wait_for_timeout(500)
        found = False
        try:
            btns = await page.query_selector_all("button")
            for b in btns:
                try:
                    if (await b.inner_text()).strip() == "Пропустить":
                        await b.click()
                        await page.wait_for_timeout(1000)
                        found = True
                        break
                except Exception:
                    pass
        except Exception:
            pass
        if found:
            break

    return True, page.url


async def main():
    results = []
    created_in_batch = 0

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            "./max_session",
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://max.ru/im")
        await page.wait_for_timeout(3000)

        print("Войди в аккаунт MAX в браузере.")
        input("После входа нажми Enter: ")

        for i, name in enumerate(CHANNELS):
            if i < START_FROM:
                print(f"[{i+1}/{len(CHANNELS)}] пропускаем")
                continue

            print(f"[{i+1}/{len(CHANNELS)}] {name} ... ", end="", flush=True)

            retry_count = 0
            while retry_count < 3:
                try:
                    success, url = await create_channel(page, name)

                    if success is None:
                        # Лимит MAX
                        await wait_for_limit_reset(page)
                        created_in_batch = 0
                        retry_count += 1
                        continue
                    elif success:
                        results.append({"name": name, "url": url, "status": "OK"})
                        print("✅")
                        created_in_batch += 1
                        break
                    else:
                        results.append({"name": name, "url": "", "status": "NO_MENU"})
                        print("❌ меню не найдено")
                        break

                except Exception as e:
                    err = str(e)
                    if "Timeout" in err or "timeout" in err:
                        print(f"\n⏸️ таймаут — возможно лимит, жду 7 мин...")
                        await wait_for_limit_reset(page)
                        created_in_batch = 0
                        retry_count += 1
                    else:
                        results.append({"name": name, "url": "", "status": f"ERR: {err[:60]}"})
                        print(f"❌ {err[:40]}")
                        break

            # Пауза между каналами
            await page.wait_for_timeout(2000)

        await ctx.close()

    with open("channels_created.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["name", "url", "status"])
        w.writeheader()
        w.writerows(results)

    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\nГотово! Создано: {ok}/{len(CHANNELS) - START_FROM}")
    print("Результаты в channels_created.csv")


asyncio.run(main())
