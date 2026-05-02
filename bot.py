import yaml
import os
import requests
import argparse
import functions_framework

from datetime import datetime, timedelta, timezone
from stellar_sdk import Server
import time
from decimal import Decimal, ROUND_DOWN

# --- Инициализация и утилиты ---

def load_config():
    with open("config.yaml", "r") as file:
        return yaml.safe_load(file)


def send_telegram_message(text, config):
    token = config['telegram']['bot_token']
    chat_id = config['telegram']['chat_id']
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Ошибка отправки в Telegram: {e}")


# --- Расчётные функции ---

def get_schedule_start(config):
    return datetime.strptime(config['schedule_start'], "%Y-%m-%d").date()


def get_first_payment_date(config):
    """Первая плановая дата выплаты = schedule_start + payment_delay_months месяцев."""
    start = get_schedule_start(config)
    delay = int(config.get('payment_delay_months', 1))
    month = start.month + delay
    year  = start.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return start.replace(year=year, month=month, day=1)


def calculate_rate(config, for_year=None):
    """
    Ставка = base_rate_pct * (years_since_start + 1).
    2026 → 0.1%, 2027 → 0.2%, 2028 → 0.3% и т.д.
    """
    start_date = get_schedule_start(config)
    if for_year is None:
        for_year = datetime.now(timezone.utc).year
    base_pct      = Decimal(str(config.get('base_rate_pct', 0.1)))  # 0.1
    years_elapsed = for_year - start_date.year                      # 0, 1, 2 …
    rate          = base_pct * (years_elapsed + 1) / Decimal('100') # 0.001, 0.002 …
    return rate


def get_pfin_holders(config):
    """
    Возвращает {address: Decimal(balance)} для всех холдеров PFIN,
    исключая кошелёк-эмитент выплат.
    """
    base_asset  = config['stellar']['base_asset']
    issuer      = config['stellar']['asset_issuer']
    target_acct = config['stellar']['target_account']
    horizon_url = config['stellar']['horizon_url'].rstrip('/')

    holders = {}
    url = f"{horizon_url}/accounts?asset={base_asset}:{issuer}&limit=200&order=asc"

    while url:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        for acc in data.get('_embedded', {}).get('records', []):
            addr = acc['id']
            if addr == target_acct:
                continue
            for bal in acc.get('balances', []):
                if bal.get('asset_code') == base_asset and bal.get('asset_issuer') == issuer:
                    amount = Decimal(bal['balance'])
                    if amount > 0:
                        holders[addr] = amount
                    break

        next_href = data.get('_links', {}).get('next', {}).get('href', '')
        url = next_href if next_href and next_href != url else None
        if url:
            time.sleep(0.3)

    return holders


def calculate_monthly_obligations(config):
    """Считает ожидаемые суммы выплат и выкупа на текущий месяц."""
    print("[*] Запрашиваем холдеров PFIN из блокчейна...")
    holders    = get_pfin_holders(config)
    total_pfin = sum(holders.values())
    rate       = calculate_rate(config)

    total_payment_usdm = (total_pfin * rate).quantize(Decimal('0.0000001'), rounding=ROUND_DOWN)
    total_buyback_usdm = total_payment_usdm   # 1:1, та же сумма

    return {
        'rate':               rate,
        'rate_pct':           float(rate * 100),
        'holder_count':       len(holders),
        'total_pfin':         total_pfin,
        'total_payment_usdm': total_payment_usdm,
        'total_buyback_usdm': total_buyback_usdm,
    }


# --- Модуль E: Расчёт ожидаемых выплат ---

def run_calculate(config):
    """Считает и отправляет в Telegram ожидаемые суммы на следующую дату выплат."""
    payment_asset    = config['stellar']['payment_asset']
    base_asset       = config['stellar']['base_asset']
    today            = datetime.now(timezone.utc).date()
    first_pay        = get_first_payment_date(config)

    # Ближайшая плановая дата выплаты >= today
    if today < first_pay:
        next_payment = first_pay
    elif today.day == 1 and today >= first_pay:
        next_payment = today
    else:
        next_payment = _next_month(today.replace(day=1))
        if next_payment < first_pay:
            next_payment = first_pay

    print(f"[*] Следующая дата выплаты: {next_payment}")
    try:
        d = calculate_monthly_obligations(config)
        first_pay = get_first_payment_date(config)
        msg = (
            f"🧮 <b>Расчёт выплат на {next_payment.strftime('%d.%m.%Y')}</b>\n"
            f"<i>Начало обязательства: {get_schedule_start(config).strftime('%d.%m.%Y')} "
            f"| первая выплата: {first_pay.strftime('%d.%m.%Y')}</i>\n\n"
            f"📈 Ставка: <b>{d['rate_pct']:.1f}%</b> ({today.year} год)\n"
            f"👥 Холдеров {base_asset}: <b>{d['holder_count']}</b>\n"
            f"🪙 {base_asset} в обращении: <b>{d['total_pfin']:.7f}</b>\n\n"
            f"💸 К выплате холдерам: <b>{d['total_payment_usdm']:.7f} {payment_asset}</b>\n"
            f"🔄 На ордер выкупа:    <b>{d['total_buyback_usdm']:.7f} {payment_asset}</b>\n"
            f"📊 Итого из кошелька:  <b>{(d['total_payment_usdm'] + d['total_buyback_usdm']):.7f} {payment_asset}</b>"
        )
        send_telegram_message(msg, config)
        print("[✓] Расчёт отправлен в Telegram.")
    except Exception as e:
        print(f"Ошибка расчёта: {e}")


# --- Модуль С: Аудит (разбивка по датам, выплаты + выкупы вместе, зачёт переплаты) ---

def _next_month(d):
    """Возвращает дату первого числа следующего месяца."""
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1, day=1)
    return d.replace(month=d.month + 1, day=1)


def run_weekly_audit(config):  # FIFO-атрибуция
    server        = Server(config['stellar']['horizon_url'])
    account       = config['stellar']['target_account']
    payment_asset = config['stellar']['payment_asset']
    base_asset    = config['stellar']['base_asset']
    today         = datetime.now(timezone.utc).date()
    start_date    = get_schedule_start(config)
    first_pay     = get_first_payment_date(config)

    print("[*] Запрашиваем холдеров PFIN...")
    holders    = get_pfin_holders(config)
    total_pfin = sum(holders.values())
    print(f"[*] Холдеров: {len(holders)}, PFIN: {total_pfin:.7f}")

    # 1. Строим список периодов начиная с первой плановой даты выплаты
    periods = []
    cur = first_pay
    while cur <= today:
        nxt  = _next_month(cur)
        rate = calculate_rate(config, for_year=cur.year)
        exp  = (total_pfin * rate).quantize(Decimal('0.0000001'), rounding=ROUND_DOWN)
        periods.append({'date': cur, 'next': nxt, 'rate': rate, 'exp_pay': exp, 'exp_buy': exp})
        cur = nxt

    # Блокчейн сканируем с даты начала обязательства (могут быть ранние платежи)
    earliest_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)

    # 2. Собираем ВСЕ операции в плоские списки (без разбивки по периодам)
    all_pay_ops = []   # {'date': date, 'amount': Decimal}
    all_buy_ops = []

    try:
        cursor = None
        while True:
            req = server.operations().for_account(account).order(desc=True).limit(200)
            if cursor:
                req.cursor(cursor)
            response = req.call()
            records  = response['_embedded']['records']
            if not records:
                break

            stop = False
            for record in records:
                created_at = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
                if created_at < earliest_dt:
                    stop = True
                    break
                if record.get('transaction_successful') is False:
                    continue

                op_type    = record['type']
                sender     = record.get('from', '')
                record_day = created_at.date()

                if op_type in ('payment', 'path_payment_strict_send', 'path_payment_strict_receive'):
                    asset = record.get('asset_code') or record.get('destination_asset_code', '')
                    if asset == payment_asset and sender == account:
                        all_pay_ops.append({'date': record_day, 'amount': Decimal(record['amount'])})

                elif op_type in ('manage_sell_offer', 'manage_buy_offer', 'create_passive_sell_offer'):
                    selling = record.get('selling_asset_code', 'XLM')
                    buying  = record.get('buying_asset_code',  'XLM')
                    amount  = Decimal(record.get('amount') or '0')
                    price   = Decimal(record.get('price')  or '1')
                    if selling == payment_asset and buying == base_asset:
                        all_buy_ops.append({'date': record_day, 'amount': amount})
                    elif selling == base_asset and buying == payment_asset and price > 0:
                        usdm = (amount / price).quantize(Decimal('0.0000001'), rounding=ROUND_DOWN)
                        all_buy_ops.append({'date': record_day, 'amount': usdm})

            if stop:
                break
            cursor = records[-1]['paging_token']
            time.sleep(1)

        # 3. FIFO-атрибуция:
        #    платежи от старых к новым закрывают самое старое обязательство первым.
        #    Если платёж пришёл в феврале, а долг есть с января — он идёт в январь.
        def fifo_attribute(ops_list, exp_key):
            for p in periods:
                p[f'cov_{exp_key}']      = Decimal('0')
                p[f'contribs_{exp_key}'] = []   # [{'date', 'amount'}]

            ops = sorted(({'date': o['date'], 'amount': o['amount']} for o in ops_list),
                         key=lambda x: x['date'])   # от старого к новому

            for period in periods:
                need = period[exp_key]
                while need > 0 and ops:
                    op   = ops[0]
                    take = min(op['amount'], need)
                    period[f'cov_{exp_key}']      += take
                    period[f'contribs_{exp_key}'].append({'date': op['date'], 'amount': take})
                    op['amount'] -= take
                    need         -= take
                    if op['amount'] == 0:
                        ops.pop(0)

            return sum(o['amount'] for o in ops)   # остаток = кредит на будущее

        fifo_attribute(all_pay_ops, 'exp_pay')
        fifo_attribute(all_buy_ops, 'exp_buy')

        # 4. Формируем строки по периодам (выплата + выкуп вместе)
        period_lines = []

        for p in periods:
            date_str   = p['date'].strftime('%d.%m.%Y')
            is_current = (p['date'].year == today.year and p['date'].month == today.month)
            rate_str   = f"{float(p['rate']*100):.1f}%"

            def fmt_row(exp, cov_key, contribs_key, label, emoji):
                cov             = p[cov_key]
                contribs        = p[contribs_key]
                overdue_now     = (today - p['date']).days   # дней с плановой даты

                if is_current:
                    icon = "⏳"
                elif cov >= exp:
                    icon = "✅"
                else:
                    icon = "🚨"

                row = f"  {emoji} {label}: {cov:.4f} / {exp:.4f} {payment_asset}  {icon}"

                if contribs:
                    first_date       = min(c['date'] for c in contribs)
                    last_date        = max(c['date'] for c in contribs)
                    first_delay      = (first_date - p['date']).days
                    completion_delay = (last_date  - p['date']).days

                    if cov >= exp:
                        # Выполнено — показываем максимальную просрочку (по дате завершения)
                        if completion_delay > 0:
                            row += f"\n    ⏰ Просрочка исполнения: +{completion_delay} дн."
                            row += f" (завершено: {last_date.strftime('%d.%m.%Y')}"
                            if first_date != last_date and first_delay > 0:
                                row += f", первый платёж: {first_date.strftime('%d.%m.%Y')} +{first_delay} дн."
                            row += ")"
                    else:
                        # Частично оплачено
                        if first_delay > 0:
                            row += f"\n    ⏰ Первый платёж: {first_date.strftime('%d.%m.%Y')} (+{first_delay} дн.)"
                        row += f"\n    ⚠️ Дефицит: {exp - cov:.4f} {payment_asset}"
                        if not is_current:
                            row += f"  |  текущая просрочка: +{overdue_now} дн."
                        elif overdue_now > 0:
                            row += f"  |  уже просрочено: +{overdue_now} дн."

                else:
                    # Ничего не оплачено
                    if not is_current:
                        row += f"\n    ❌ Не исполнено  |  просрочка: +{overdue_now} дн."
                    elif overdue_now > 0:
                        row += f"\n    ⏳ Ещё не оплачено, просрочено: +{overdue_now} дн."

                return row

            pay_row = fmt_row(p['exp_pay'], 'cov_exp_pay', 'contribs_exp_pay', 'Выплата', '💸')
            buy_row = fmt_row(p['exp_buy'], 'cov_exp_buy', 'contribs_exp_buy', 'Выкуп  ', '🔄')
            period_lines.append(f"📅 <b>{date_str}</b>  ставка {rate_str}\n{pay_row}\n{buy_row}")

        # 5. Итоговая задолженность (net = paid − expected)
        total_exp_pay  = sum(p['exp_pay'] for p in periods)
        total_exp_buy  = sum(p['exp_buy'] for p in periods)
        total_paid_pay = sum(o['amount'] for o in all_pay_ops)
        total_paid_buy = sum(o['amount'] for o in all_buy_ops)
        net_pay        = total_paid_pay - total_exp_pay   # > 0 кредит, < 0 долг
        net_buy        = total_paid_buy - total_exp_buy

        def net_line(label, net, asset):
            if net < 0:
                return f"{label}: ❌ <b>долг {abs(net):.4f} {asset}</b>"
            elif net > 0:
                return f"{label}: ✅ <b>переплата +{net:.4f} {asset}</b> (зачёт в следующем периоде)"
            return f"{label}: ✅ без задолженности"

        rate_now = calculate_rate(config)
        msg = (
            f"📊 <b>Аудит PFIN</b>\n"
            f"📌 Начало обязательства: {start_date.strftime('%d.%m.%Y')} "
            f"| первая выплата: {first_pay.strftime('%d.%m.%Y')}\n"
            f"🪙 PFIN в обращении: {total_pfin:.4f} | Холдеров: {len(holders)}\n"
            f"📈 Ставка сейчас: {float(rate_now * 100):.1f}%/мес\n\n"
            + "\n\n".join(period_lines)
            + f"\n\n━━━ <b>Итоговая задолженность</b> ━━━\n"
            + net_line("💸 Выплаты", net_pay, payment_asset) + "\n"
            + net_line("🔄 Выкуп  ", net_buy, payment_asset)
        )

        if len(msg) > 4000:
            msg = msg[:3990] + "\n…(обрезано)"

        send_telegram_message(msg, config)
        print("[✓] Аудит отправлен.")

    except Exception as e:
        error_msg = f"⚠️ Ошибка аудита Stellar: {e}"
        print(error_msg)
        send_telegram_message(error_msg, config)



# --- Модуль В: Ежедневные напоминания ---

def run_daily_reminder(config):
    today         = datetime.now(timezone.utc).date()
    tomorrow      = today + timedelta(days=1)
    payment_asset = config['stellar']['payment_asset']
    base_asset    = config['stellar']['base_asset']
    first_pay     = get_first_payment_date(config)

    if tomorrow.day != 1 or tomorrow < first_pay:
        print(f"[—] Завтра {tomorrow} — не день выплат, напоминание не нужно.")
        return

    print("[*] Завтра 1-е число — считаем ожидаемые суммы...")
    try:
        d = calculate_monthly_obligations(config)
        msg = (
            f"⚠️ <b>Напоминание: завтра ({tomorrow.strftime('%d.%m.%Y')}) — день выплат!</b>\n\n"
            f"📈 Ставка: <b>{d['rate_pct']:.1f}%</b> ({tomorrow.year} год)\n"
            f"👥 Холдеров {base_asset}: <b>{d['holder_count']}</b>\n"
            f"🪙 {base_asset} в обращении: <b>{d['total_pfin']:.7f}</b>\n\n"
            f"💸 К выплате холдерам: <b>{d['total_payment_usdm']:.7f} {payment_asset}</b>\n"
            f"🔄 На ордер выкупа:    <b>{d['total_buyback_usdm']:.7f} {payment_asset}</b>"
        )
        send_telegram_message(msg, config)
        print("[✓] Напоминание отправлено.")
    except Exception as e:
        print(f"Ошибка расчёта для напоминания: {e}")


# --- Модуль А: Мониторинг новых событий (15-минутное окно) ---

def run_interval_monitor(config):
    server        = Server(config['stellar']['horizon_url'])
    account       = config['stellar']['target_account']
    base_asset    = config['stellar']['base_asset']
    payment_asset = config['stellar']['payment_asset']

    now          = datetime.now(timezone.utc)
    window_min   = int(config.get('monitor_window_minutes', 15))
    window_start = now - timedelta(minutes=window_min)

    print(f"[*] Окно: {window_min} мин | {window_start.strftime('%Y-%m-%d %H:%M:%S')} — {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    payment_total   = Decimal('0')
    payment_wallets = set()
    orders_found    = []
    cursor = None

    try:
        while True:
            req = server.operations().for_account(account).order(desc=True).limit(200)
            if cursor:
                req.cursor(cursor)
            response = req.call()
            records  = response['_embedded']['records']
            if not records:
                break

            stop_pagination = False
            for record in records:
                created_at = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
                if created_at < window_start:
                    print(f"[-] Предел окна: {created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    stop_pagination = True
                    break
                if record.get('transaction_successful') is False:
                    continue

                op_type = record['type']
                sender  = record.get('from', '')

                if op_type in ('payment', 'path_payment_strict_send', 'path_payment_strict_receive'):
                    asset = record.get('asset_code') or record.get('destination_asset_code', '')
                    if asset == payment_asset and sender == account:
                        recipient = record.get('to', '')
                        payment_total += Decimal(record['amount'])
                        if recipient:
                            payment_wallets.add(recipient)

                elif op_type in ('manage_sell_offer', 'manage_buy_offer', 'create_passive_sell_offer'):
                    selling = record.get('selling_asset_code', 'XLM')
                    buying  = record.get('buying_asset_code',  'XLM')
                    if ((selling == base_asset and buying == payment_asset) or
                            (selling == payment_asset and buying == base_asset)):
                        orders_found.append({
                            'action':  "ВЫКУП" if buying == base_asset else "ПРОДАЖА",
                            'amount':  record.get('amount', '?'),
                            'selling': selling,
                            'buying':  buying,
                            'price':   record.get('price', '?'),
                            'time':    created_at,
                        })

            if stop_pagination:
                break
            cursor = records[-1]['paging_token']
            time.sleep(0.5)

        parts = []
        if payment_total > 0:
            parts.append(
                f"💸 <b>Выплаты ({payment_asset})</b>\n"
                f"Общая сумма: <b>{payment_total:.7f} {payment_asset}</b>\n"
                f"Кошельков получателей: <b>{len(payment_wallets)}</b>"
            )
        if orders_found:
            lines = [
                f"  • {o['action']}: {o['amount']} {o['selling']} "
                f"по {o['price']} {o['buying']} [{o['time'].strftime('%d.%m %H:%M')}]"
                for o in orders_found
            ]
            parts.append(f"📊 <b>Ордера ({base_asset}/{payment_asset})</b>\n" + "\n".join(lines))

        if parts:
            period = f"{window_start.strftime('%d.%m.%Y %H:%M')} — {now.strftime('%d.%m.%Y %H:%M')} UTC"
            msg = f"📋 <b>Отчёт монитора</b>\n<i>{period}</i>\n\n" + "\n\n".join(parts)
            send_telegram_message(msg, config)
            print("[✓] Сообщение отправлено.")
        else:
            print("[—] Новых событий в окне не обнаружено.")

    except Exception as e:
        print(f"Ошибка Horizon API: {e}")


# --- Модуль D: Диагностика ---

def run_debug_dump(config):
    server        = Server(config['stellar']['horizon_url'])
    account       = config['stellar']['target_account']
    now           = datetime.now(timezone.utc)
    window_start  = now - timedelta(days=30)

    print(f"\n{'='*70}")
    print(f"ДАМП: {window_start.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')} UTC")
    print(f"Аккаунт: {account}")
    print(f"{'='*70}\n")

    cursor = None
    total  = 0

    while True:
        req = server.operations().for_account(account).order(desc=True).limit(200)
        if cursor:
            req.cursor(cursor)
        response = req.call()
        records  = response['_embedded']['records']
        if not records:
            break

        stop = False
        for record in records:
            created_at = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
            if created_at < window_start:
                stop = True
                break
            total += 1
            op    = record['type']
            line  = f"тип={op} | ok={record.get('transaction_successful','?')} | {created_at.strftime('%Y-%m-%d %H:%M:%S')}"
            if op in ('payment', 'path_payment_strict_send', 'path_payment_strict_receive'):
                asset = record.get('asset_code', record.get('destination_asset_code', '?'))
                line += f" | {asset} {record.get('amount','?')} от=…{record.get('from','?')[-8:]} → …{record.get('to','?')[-8:]}"
            elif op in ('manage_sell_offer', 'manage_buy_offer', 'create_passive_sell_offer'):
                line += f" | {record.get('selling_asset_code','XLM')}→{record.get('buying_asset_code','XLM')} {record.get('amount','?')} цена={record.get('price','?')}"
            elif op == 'create_claimable_balance':
                line += f" | {record.get('asset','?')} {record.get('amount','?')} получ.={len(record.get('claimants',[]))}"
            print(f"  #{total:04d} | {line}")

        if stop:
            break
        cursor = records[-1]['paging_token']
        time.sleep(0.3)

    print(f"\n{'='*70}\nВсего операций за 30 дней: {total}\n{'='*70}\n")


@functions_framework.http
def handle_request(request):
    """Точка входа для Cloud Functions"""
    request_json = request.get_json(silent=True)

    if request_json and 'task' in request_json:
        task = request_json['task']
    else:
        return 'Отсутствует параметр task', 400

    config = load_config()

    # Применяем переменные окружения (заданы в deploy.yml через secrets)
    if os.environ.get('TELEGRAM_BOT_TOKEN'):
        config['telegram']['bot_token'] = os.environ['TELEGRAM_BOT_TOKEN']
    if os.environ.get('TELEGRAM_CHAT_ID'):
        config['telegram']['chat_id'] = os.environ['TELEGRAM_CHAT_ID']

    try:
        if task == 'reminder':
            run_daily_reminder(config)
            return 'Напоминания отработали', 200
        elif task == 'monitor':
            run_interval_monitor(config)
            return 'Мониторинг отработал', 200
        elif task == 'audit':
            run_weekly_audit(config)
            return 'Аудит отработал', 200
        elif task == 'calculate':
            run_calculate(config)
            return 'Расчёт отработал', 200
        else:
            return f'Неизвестная задача: {task}', 400
    except Exception as e:
        print(f"Критическая ошибка выполнения {task}: {e}")
        return 'Внутренняя ошибка', 500
# --- Точка входа ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stellar PFIN Monitor Bot")
    parser.add_argument(
        '--task',
        choices=['reminder', 'monitor', 'audit', 'calculate', 'debug'],
        required=True,
        help='reminder=напоминание накануне | monitor=отчёт окна | '
             'audit=план vs факт | calculate=расчёт сумм | debug=дамп операций'
    )
    parser.add_argument('--token',   default=None, help='Telegram bot token (переопределяет config.yaml)')
    parser.add_argument('--chat-id', default=None, dest='chat_id',
                        help='Telegram chat_id (переопределяет config.yaml)')
    args   = parser.parse_args()
    config = load_config()

    # Приоритет: CLI-аргументы → переменные окружения → config.yaml
    if args.token:
        config['telegram']['bot_token'] = args.token
    elif os.environ.get('TELEGRAM_BOT_TOKEN'):
        config['telegram']['bot_token'] = os.environ['TELEGRAM_BOT_TOKEN']

    if args.chat_id:
        config['telegram']['chat_id'] = args.chat_id
    elif os.environ.get('TELEGRAM_CHAT_ID'):
        config['telegram']['chat_id'] = os.environ['TELEGRAM_CHAT_ID']

    if args.task == 'reminder':
        run_daily_reminder(config)
    elif args.task == 'monitor':
        run_interval_monitor(config)
    elif args.task == 'audit':
        run_weekly_audit(config)
    elif args.task == 'calculate':
        run_calculate(config)
    elif args.task == 'debug':
        run_debug_dump(config)

