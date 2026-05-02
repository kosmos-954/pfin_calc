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


def get_total_pfin_issued(config):
    """Суммарный выпуск PFIN из /assets (все состояния трастлайнов + claimable + LP)."""
    base_asset  = config['stellar']['base_asset']
    issuer      = config['stellar']['asset_issuer']
    horizon_url = config['stellar']['horizon_url'].rstrip('/')

    resp = requests.get(
        f"{horizon_url}/assets?asset_code={base_asset}&asset_issuer={issuer}&limit=1",
        timeout=20
    )
    resp.raise_for_status()
    records = resp.json().get('_embedded', {}).get('records', [])
    if not records:
        raise ValueError(f"Актив {base_asset}:{issuer} не найден в Horizon")

    rec = records[0]
    bal = rec.get('balances', {})
    return (
        Decimal(bal.get('authorized',                          '0')) +
        Decimal(bal.get('authorized_to_maintain_liabilities', '0')) +
        Decimal(bal.get('unauthorized',                       '0')) +
        Decimal(rec.get('claimable_balances_amount',          '0')) +
        Decimal(rec.get('liquidity_pools_amount',             '0')) +
        Decimal(rec.get('contracts_amount',                   '0'))
    )


def get_pfin_circulation(config):
    """
    Текущее количество PFIN в обращении:
    total_issued − target_balance
    (все выпущенные токены минус токены на кошельке выплат/выкупа)
    """
    base_asset  = config['stellar']['base_asset']
    issuer      = config['stellar']['asset_issuer']
    target_acct = config['stellar']['target_account']
    horizon_url = config['stellar']['horizon_url'].rstrip('/')

    total_issued = get_total_pfin_issued(config)

    resp = requests.get(f"{horizon_url}/accounts/{target_acct}", timeout=20)
    resp.raise_for_status()
    target_balance = Decimal('0')
    for b in resp.json().get('balances', []):
        if b.get('asset_code') == base_asset and b.get('asset_issuer') == issuer:
            target_balance = Decimal(b['balance'])
            break

    circulation = total_issued - target_balance
    print(f"[*] PFIN выпущено: {total_issued:.7f}, на кошельке выплат: {target_balance:.7f}, "
          f"в обращении: {circulation:.7f}")
    return circulation


def get_target_pfin_balance_at_period_dates(config, period_dates):
    """
    Реконструирует баланс PFIN на target_account на начало каждой из дат period_dates.
    Использует /accounts/{id}/effects, идёт от текущего состояния назад во времени.

    Возвращает {date: Decimal(баланс на начало дня)}.
    """
    base_asset  = config['stellar']['base_asset']
    issuer      = config['stellar']['asset_issuer']
    target_acct = config['stellar']['target_account']
    horizon_url = config['stellar']['horizon_url'].rstrip('/')
    today       = datetime.now(timezone.utc).date()

    # Текущий баланс
    resp = requests.get(f"{horizon_url}/accounts/{target_acct}", timeout=20)
    resp.raise_for_status()
    current_balance = Decimal('0')
    for b in resp.json().get('balances', []):
        if b.get('asset_code') == base_asset and b.get('asset_issuer') == issuer:
            current_balance = Decimal(b['balance'])
            break

    # Сортируем даты от новых к старым
    dates_desc = sorted(period_dates, reverse=True)
    result     = {}
    balance    = current_balance

    # Для будущих дат оставляем текущий баланс как best-effort.
    # Для сегодняшней даты тоже нужен баланс на начало дня, поэтому
    # её обрабатываем через "размотку" effects вместе с прошлыми датами.
    remaining = []
    for d in dates_desc:
        if d > today:
            result[d] = balance
        else:
            remaining.append(d)

    if not remaining:
        return result

    oldest_needed = remaining[-1]
    oldest_dt     = datetime.combine(oldest_needed, datetime.min.time()).replace(tzinfo=timezone.utc)

    # Проходим effects от новых к старым, "разматывая" баланс назад
    url = f"{horizon_url}/accounts/{target_acct}/effects?limit=200&order=desc"

    while url and remaining:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data    = resp.json()
        effects = data.get('_embedded', {}).get('records', [])
        if not effects:
            break

        for effect in effects:
            effect_dt   = datetime.fromisoformat(effect['created_at'].replace('Z', '+00:00'))
            effect_date = effect_dt.date()

            # Присваиваем баланс датам периодов, которые СТРОГО ПОЗЖЕ этого эффекта.
            # Условие > (не >=): эффекты НА дату периода ещё не входят в «начало дня»,
            # поэтому сначала отматываем все эффекты этого дня, потом присваиваем.
            while remaining and remaining[0] > effect_date:
                result[remaining.pop(0)] = balance
                if not remaining:
                    break
            if not remaining:
                break

            # "Отматываем" эффект назад
            e_type = effect['type']
            if (effect.get('asset_code') == base_asset and
                    effect.get('asset_issuer') == issuer):
                amount = Decimal(effect.get('amount', '0'))
                if e_type == 'account_credited':
                    balance -= amount   # зачисление → в прошлом баланс был меньше
                elif e_type == 'account_debited':
                    balance += amount   # списание → в прошлом баланс был больше

        if not remaining:
            break

        # Если дошли до нужной глубины — выходим
        last_dt = datetime.fromisoformat(effects[-1]['created_at'].replace('Z', '+00:00'))
        if last_dt < oldest_dt:
            break

        next_href = data.get('_links', {}).get('next', {}).get('href', '')
        url = next_href if next_href and next_href != url else None
        time.sleep(0.3)

    # Оставшиеся (самые старые) даты — присвоить отмотанный баланс
    for d in remaining:
        result[d] = balance

    return result


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
    print("[*] Запрашиваем данные PFIN из блокчейна...")
    total_pfin = get_pfin_circulation(config)
    holders    = get_pfin_holders(config)   # нужен только для подсчёта кошельков
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
    issuer        = config['stellar']['asset_issuer']
    today         = datetime.now(timezone.utc).date()
    start_date    = get_schedule_start(config)
    first_pay     = get_first_payment_date(config)

    print("[*] Запрашиваем данные PFIN из блокчейна...")
    holders          = get_pfin_holders(config)
    total_issued_now = get_total_pfin_issued(config)
    print(f"[*] PFIN выпущено сейчас: {total_issued_now:.7f} | Холдеров: {len(holders)}")

    # 1. Строим список периодов (exp_pay/exp_buy добавим после скана операций)
    periods = []
    cur = first_pay
    while cur <= today:
        nxt  = _next_month(cur)
        rate = calculate_rate(config, for_year=cur.year)
        periods.append({'date': cur, 'next': nxt, 'rate': rate})
        cur = nxt

    # 2. Исторический баланс target_account на начало каждого периода
    print("[*] Реконструируем исторический баланс PFIN на кошельке выплат...")
    target_balances = get_target_pfin_balance_at_period_dates(config, [p['date'] for p in periods])

    # Блокчейн сканируем с даты начала обязательства
    earliest_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)

    # 3. Собираем ВСЕ операции в плоские списки
    all_pay_ops  = []   # USDM-выплаты холдерам                  {'date', 'amount'}
    all_buy_ops  = []   # ордера выкупа PFIN                      {'date', 'amount'}
    all_burn_ops = []   # PFIN сожжён target→issuer               {'date', 'amount'}
    all_mint_ops = []   # PFIN допвыпуск issuer→target            {'date', 'amount'}

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
                record_day = created_at.date()

                if op_type in ('payment', 'path_payment_strict_send', 'path_payment_strict_receive'):
                    asset   = record.get('asset_code') or record.get('destination_asset_code', '')
                    op_from = record.get('from', '')
                    op_to   = record.get('to',   '')
                    amt     = Decimal(record.get('amount', '0'))

                    # USDM-выплаты из кошелька выплат холдерам
                    if asset == payment_asset and op_from == account:
                        all_pay_ops.append({'date': record_day, 'amount': amt})

                    # PFIN-сжигание: target → issuer (уменьшает total_issued)
                    if asset == base_asset and op_from == account and op_to == issuer:
                        all_burn_ops.append({'date': record_day, 'amount': amt})
                        print(f"  [burn] {record_day} — {amt:.7f} {base_asset} (target→issuer)")

                    # PFIN-допэмиссия: issuer → target (увеличивает total_issued)
                    if asset == base_asset and op_from == issuer and op_to == account:
                        all_mint_ops.append({'date': record_day, 'amount': amt})
                        print(f"  [mint] {record_day} — {amt:.7f} {base_asset} (issuer→target)")

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

        # 4. Расчёт обращения и ожидаемых сумм:
        #    - hist_issued = total_issued_now + сожжено_после_даты − выпущено_после_даты
        #      (Stellar /assets уже учитывает сжигания, добавляем сожжённые ПОСЛЕ даты обратно)
        #    - circulation = hist_issued − target_balance_at_date
        #      (исключаем токены на кошельке выплат: нераспределённые + выкупленные)
        print(f"[*] Найдено сжиганий: {len(all_burn_ops)}, допэмиссий: {len(all_mint_ops)}")
        for p in periods:
            target_bal  = target_balances[p['date']]
            burns_after = sum(
                (b['amount'] for b in all_burn_ops if b['date'] >= p['date']),
                Decimal('0')
            )
            mints_after = sum(
                (m['amount'] for m in all_mint_ops if m['date'] >= p['date']),
                Decimal('0')
            )
            hist_issued = total_issued_now + burns_after - mints_after
            circulation = hist_issued - target_bal
            exp = (circulation * p['rate']).quantize(Decimal('0.0000001'), rounding=ROUND_DOWN)
            p['exp_pay']     = exp
            p['exp_buy']     = exp
            p['circulation'] = circulation
            p['target_bal']  = target_bal
            p['hist_issued'] = hist_issued
            print(f"  {p['date']} | issued={hist_issued:.4f} "
                  f"(+burn {burns_after:.4f} -mint {mints_after:.4f}) "
                  f"| target={target_bal:.4f} | обр={circulation:.4f} | ожид={exp:.4f}")

        total_pfin_current = periods[-1]['circulation'] if periods else Decimal('0')
        print(f"[*] Текущее обращение (последний период): {total_pfin_current:.7f}")

        # 5. FIFO-атрибуция: платежи от старых к новым закрывают самое старое обязательство.
        def fifo_attribute(ops_list, exp_key):
            for p in periods:
                p[f'cov_{exp_key}']      = Decimal('0')
                p[f'contribs_{exp_key}'] = []

            ops = sorted(({'date': o['date'], 'amount': o['amount']} for o in ops_list),
                         key=lambda x: x['date'])

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

        # 6. Формируем строки по периодам (выплата + выкуп вместе)
        period_lines = []

        for p in periods:
            date_str   = p['date'].strftime('%d.%m.%Y')
            is_current = (p['date'].year == today.year and p['date'].month == today.month)
            rate_str   = f"{float(p['rate']*100):.1f}%"

            def fmt_row(exp, cov_key, contribs_key, label, emoji):
                cov         = p[cov_key]
                contribs    = p[contribs_key]
                overdue_now = (today - p['date']).days

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
                        if completion_delay > 0:
                            row += f"\n    ⏰ Просрочка исполнения: +{completion_delay} дн."
                            row += f" (завершено: {last_date.strftime('%d.%m.%Y')}"
                            if first_date != last_date and first_delay > 0:
                                row += f", первый платёж: {first_date.strftime('%d.%m.%Y')} +{first_delay} дн."
                            row += ")"
                    else:
                        if first_delay > 0:
                            row += f"\n    ⏰ Первый платёж: {first_date.strftime('%d.%m.%Y')} (+{first_delay} дн.)"
                        row += f"\n    ⚠️ Дефицит: {exp - cov:.4f} {payment_asset}"
                        if not is_current:
                            row += f"  |  текущая просрочка: +{overdue_now} дн."
                        elif overdue_now > 0:
                            row += f"  |  уже просрочено: +{overdue_now} дн."
                else:
                    if not is_current:
                        row += f"\n    ❌ Не исполнено  |  просрочка: +{overdue_now} дн."
                    elif overdue_now > 0:
                        row += f"\n    ⏳ Ещё не оплачено, просрочено: +{overdue_now} дн."

                return row

            pay_row  = fmt_row(p['exp_pay'], 'cov_exp_pay', 'contribs_exp_pay', 'Выплата', '💸')
            buy_row  = fmt_row(p['exp_buy'], 'cov_exp_buy', 'contribs_exp_buy', 'Выкуп  ', '🔄')
            circ_str = f"  <i>обращение: {p['circulation']:.4f}</i>"
            period_lines.append(f"📅 <b>{date_str}</b>  ставка {rate_str}{circ_str}\n{pay_row}\n{buy_row}")

        # 7. Итоговая задолженность (net = paid − expected)
        total_exp_pay  = sum(p['exp_pay'] for p in periods)
        total_exp_buy  = sum(p['exp_buy'] for p in periods)
        total_paid_pay = sum(o['amount'] for o in all_pay_ops)
        total_paid_buy = sum(o['amount'] for o in all_buy_ops)
        net_pay        = total_paid_pay - total_exp_pay
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
            f"🪙 PFIN в обращении сейчас: {total_pfin_current:.4f} | Холдеров: {len(holders)}\n"
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

    # Находим предыдущий завершённый период, выровненный по часу.
    # Пример: интервал 15 мин, время 00:49 → текущий слот 00:45-01:00
    #                                          предыдущий слот 00:30-00:45
    current_slot = now.minute // window_min   # индекс текущего слота в часе
    if current_slot == 0:
        # Предыдущий период в предыдущем часе
        window_end   = now.replace(minute=0, second=0, microsecond=0)
        window_start = window_end - timedelta(minutes=window_min)
    else:
        end_minute   = current_slot * window_min
        start_minute = end_minute - window_min
        window_end   = now.replace(minute=end_minute,   second=0, microsecond=0)
        window_start = now.replace(minute=start_minute, second=0, microsecond=0)

    print(f"[*] Период: {window_start.strftime('%Y-%m-%d %H:%M')} — {window_end.strftime('%H:%M')} UTC  (интервал {window_min} мин)")

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

                # Записи новее window_end — ещё не наш период, пропускаем
                if created_at >= window_end:
                    continue

                # Записи старше window_start — период закончился, стоп
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
            period = f"{window_start.strftime('%d.%m.%Y %H:%M')} — {window_end.strftime('%H:%M')} UTC"
            msg = f"📋 <b>Отчёт монитора</b>\n<i>{period}</i>\n\n" + "\n\n".join(parts)
            send_telegram_message(msg, config)
            print("[✓] Сообщение отправлено.")
        else:
            print("[—] Новых событий в окне не обнаружено.")

    except Exception as e:
        print(f"Ошибка Horizon API: {e}")


# --- Модуль F: Проверка расчётов обращения ---

def run_circulation_check(config):
    """
    Выводит «сырые» данные для ручной проверки формулы:
        circulation = total_issued − target_balance
    а также историческую реконструкцию баланса target_account по периодам.
    """
    base_asset  = config['stellar']['base_asset']
    issuer      = config['stellar']['asset_issuer']
    target_acct = config['stellar']['target_account']
    horizon_url = config['stellar']['horizon_url'].rstrip('/')
    today       = datetime.now(timezone.utc).date()
    first_pay   = get_first_payment_date(config)

    SEP = '=' * 72

    # 1. Данные из /assets
    print(f"\n{SEP}")
    print("1. ДАННЫЕ /assets (суммарный выпуск)")
    print(SEP)
    resp = requests.get(
        f"{horizon_url}/assets?asset_code={base_asset}&asset_issuer={issuer}&limit=1",
        timeout=20
    )
    resp.raise_for_status()
    records = resp.json().get('_embedded', {}).get('records', [])
    if not records:
        print(f"Актив {base_asset}:{issuer} НЕ НАЙДЕН")
        return
    rec = records[0]
    bal = rec.get('balances', {})
    print(f"  asset_code    : {rec.get('asset_code')}")
    print(f"  asset_issuer  : {rec.get('asset_issuer')}")
    print(f"  balances.authorized                       : {bal.get('authorized', '—')}")
    print(f"  balances.authorized_to_maintain_liabilities: {bal.get('authorized_to_maintain_liabilities', '—')}")
    print(f"  balances.unauthorized                     : {bal.get('unauthorized', '—')}")
    print(f"  claimable_balances_amount : {rec.get('claimable_balances_amount', '—')}")
    print(f"  liquidity_pools_amount    : {rec.get('liquidity_pools_amount', '—')}")
    print(f"  contracts_amount          : {rec.get('contracts_amount', '—')}")
    total_issued = (
        Decimal(bal.get('authorized', '0')) +
        Decimal(bal.get('authorized_to_maintain_liabilities', '0')) +
        Decimal(bal.get('unauthorized', '0')) +
        Decimal(rec.get('claimable_balances_amount', '0')) +
        Decimal(rec.get('liquidity_pools_amount', '0')) +
        Decimal(rec.get('contracts_amount', '0'))
    )
    print(f"\n  → ИТОГО ВЫПУЩЕНО: {total_issued:.7f} {base_asset}")

    # 2. Баланс target_account прямо сейчас
    print(f"\n{SEP}")
    print("2. ТЕКУЩИЙ БАЛАНС target_account")
    print(SEP)
    resp2 = requests.get(f"{horizon_url}/accounts/{target_acct}", timeout=20)
    resp2.raise_for_status()
    target_balance = Decimal('0')
    for b in resp2.json().get('balances', []):
        if b.get('asset_code') == base_asset and b.get('asset_issuer') == issuer:
            target_balance = Decimal(b['balance'])
            buying_liab    = Decimal(b.get('buying_liabilities', '0'))
            selling_liab   = Decimal(b.get('selling_liabilities', '0'))
            print(f"  balance              : {target_balance:.7f}")
            print(f"  buying_liabilities   : {buying_liab:.7f}  (в единицах {base_asset})")
            print(f"  selling_liabilities  : {selling_liab:.7f}  (в единицах {base_asset})")
            break
    print(f"\n  → ТЕКУЩЕЕ ОБРАЩЕНИЕ: {total_issued:.7f} − {target_balance:.7f} = "
          f"{total_issued - target_balance:.7f} {base_asset}")

    print(f"\n{SEP}")
    print("3. ИСТОРИЧЕСКАЯ РЕКОНСТРУКЦИЯ ПО ПЕРИОДАМ")
    print(f"   circulation = hist_total_issued − target_balance_at_date")
    print(f"   hist_issued = total_issued_now + burns_after_date − mints_after_date")
    print(SEP)

    periods = []
    cur = first_pay
    while cur <= today:
        periods.append(cur)
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1, day=1)
        else:
            cur = cur.replace(month=cur.month + 1, day=1)

    print(f"[*] Реконструируем эффекты target_account...")
    try:
        target_bals = get_target_pfin_balance_at_period_dates(config, periods)
    except Exception as e:
        print(f"Ошибка при реконструкции target_balance: {e}")
        return

    print(f"[*] Историческая реконструкция total_issued недоступна в этой диагностике; "
          f"используем текущее total_issued для всех дат.")
    hist_issued_map = {d: total_issued for d in periods}

    for period_date in periods:
        tbal        = target_bals.get(period_date, Decimal('0'))
        hist_issued = hist_issued_map.get(period_date, total_issued)
        circ        = hist_issued - tbal
        rate        = calculate_rate(config, for_year=period_date.year)
        exp         = (circ * rate).quantize(Decimal('0.0000001'), rounding=ROUND_DOWN)
        is_current  = (period_date.year == today.year and period_date.month == today.month)
        mark        = " ◄ текущий" if is_current else ""
        delta_issued = hist_issued - total_issued
        delta_str    = f" (Δ={delta_issued:+.4f})" if delta_issued != 0 else ""
        print(f"  {period_date.strftime('%d.%m.%Y')} | "
              f"issued={hist_issued:.4f}{delta_str} | "
              f"target={tbal:.4f} | "
              f"обращение={circ:.4f} | "
              f"ставка={float(rate*100):.2f}% | "
              f"ожид={exp:.4f}{mark}")

    print(f"\n{SEP}")
    print("ИТОГ: circulation = hist_total_issued − target_balance_at_date")
    print("      hist_issued учитывает сжигания: если токены сожжены ПОСЛЕ даты периода,")
    print("      они добавляются обратно (на ту дату они ещё не были сожжены).")
    print(SEP)

    # 4. Полный лог PFIN-эффектов на target_account (хронологически)
    print(f"\n{SEP}")
    print(f"4. ВСЕ PFIN-ЭФФЕКТЫ target_account с {get_schedule_start(config)} по сегодня")
    print(f"   (credit = PFIN пришёл на кошелёк, debit = PFIN ушёл с кошелька)")
    print(SEP)

    start_dt   = datetime.combine(get_schedule_start(config), datetime.min.time()).replace(tzinfo=timezone.utc)
    raw_effects = []
    url_eff = f"{horizon_url}/accounts/{target_acct}/effects?limit=200&order=desc"
    while url_eff:
        r = requests.get(url_eff, timeout=20)
        r.raise_for_status()
        data_eff = r.json()
        effects  = data_eff.get('_embedded', {}).get('records', [])
        if not effects:
            break
        stop_eff = False
        for eff in effects:
            eff_dt = datetime.fromisoformat(eff['created_at'].replace('Z', '+00:00'))
            if eff_dt < start_dt:
                stop_eff = True
                break
            if eff.get('asset_code') == base_asset and eff.get('asset_issuer') == issuer:
                raw_effects.append({
                    'dt':     eff_dt,
                    'type':   eff['type'],
                    'amount': Decimal(eff.get('amount', '0')),
                })
        if stop_eff:
            break
        next_href = data_eff.get('_links', {}).get('next', {}).get('href', '')
        url_eff = next_href if next_href and next_href != url_eff else None
        time.sleep(0.2)

    # Сортируем хронологически (старые → новые) и выводим с накопленным балансом
    raw_effects.sort(key=lambda x: x['dt'])
    running = target_balance   # текущий баланс как отправная точка — пойдём от конца
    # Лучше пересчитать с нуля вперёд; для этого восстановим «начальный» баланс
    credits = sum(e['amount'] for e in raw_effects if e['type'] == 'account_credited')
    debits  = sum(e['amount'] for e in raw_effects if e['type'] == 'account_debited')
    # balance_before_period = current + debits - credits (отматываем все эффекты)
    balance_before = target_balance + debits - credits
    print(f"  Расчётный баланс ДО начала периода ({get_schedule_start(config)}): {balance_before:.7f}")
    running = balance_before
    for e in raw_effects:
        direction = '+' if e['type'] == 'account_credited' else '-'
        if e['type'] == 'account_credited':
            running += e['amount']
        else:
            running -= e['amount']
        etype_short = 'CREDIT' if e['type'] == 'account_credited' else 'DEBIT '
        print(f"  {e['dt'].strftime('%Y-%m-%d %H:%M')} UTC | {etype_short} {direction}{e['amount']:.7f} {base_asset} | баланс→ {running:.7f}")

    if not raw_effects:
        print("  (нет PFIN-эффектов в этом периоде)")

    print(f"\n  Итог за период: credit={credits:.7f}, debit={debits:.7f}, "
          f"изменение баланса={credits - debits:+.7f}")
    print(f"  Текущий баланс: {target_balance:.7f}  "
          f"(проверка: {balance_before:.7f} + {credits - debits:.7f} = {balance_before + credits - debits:.7f})")
    print(SEP)


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
        elif task == 'circulation':
            run_circulation_check(config)
            return 'Проверка обращения отработала', 200
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
        choices=['reminder', 'monitor', 'audit', 'calculate', 'debug', 'circulation'],
        required=True,
        help='reminder=напоминание накануне | monitor=отчёт окна | '
             'audit=план vs факт | calculate=расчёт сумм | '
             'debug=дамп операций | circulation=проверка расчётов обращения'
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
    elif args.task == 'circulation':
        run_circulation_check(config)

