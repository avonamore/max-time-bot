"""Business operations. Caller owns the transaction; no MAX API calls here."""
import csv
import io
import json
from datetime import datetime, timezone
from psycopg.types.json import Jsonb
from .timecalc import RuleError, aware, totals, validate_intervals, duration, period
from .reporting import HEADERS, interval_row, interval_text

ACTIONS = {'Начать работу': 'start', 'Начать перерыв': 'pause',
           'Закончить перерыв': 'resume', 'Завершить работу': 'stop',
           'Мой статус': 'status', 'Мои часы': 'hours'}
HELP = '''Меню: /menu
Мои часы: /hours сегодня | неделя | месяц | 2026-11-01 2026-11-30
Мои смены и их ID: /shifts
Исправление (только завершенная смена):
/correct ID НАЧАЛО КОНЕЦ ПЕРЕРЫВЫ | причина
Даты ISO с часовым поясом; перерывы: начало/конец,начало/конец или -.
Пример: /correct 12 2026-11-02T09:00+06:00 2026-11-02T18:00+06:00 2026-11-02T13:00+06:00/2026-11-02T14:00+06:00 | Забыл завершить смену
Мои заявки: /requests'''
ADMIN_HELP = '''Администратор:
/users — сотрудники и заявки регистрации
/approve MAX_ID — одобрить; /block MAX_ID — блокировать
/active — открытые смены
/requests — заявки исправления
/review ID — полные старые и новые значения
/accept ID — применить; /reject ID — отклонить
/report сегодня|неделя|месяц|ДАТА ДАТА [all|MAX_ID]
/csv те же параметры — CSV-файл'''


def positive_id(value):
    result = int(value)
    if not 0 < result < 2**63:
        raise RuleError('ID должен быть положительным целым числом в диапазоне MAX.')
    return result


def utcnow():
    return datetime.now(timezone.utc)


class Service:
    def __init__(self, db, config, now=None):
        self.db, self.cfg, self.now = db, config, now or utcnow()

    def employee(self, uid):
        # Serializes registration, role changes, accounting, and approvals across workers.
        self.db.execute('SELECT pg_advisory_xact_lock(5100922)')
        self.db.execute('INSERT INTO employees(max_id) VALUES(%s) ON CONFLICT DO NOTHING', (uid,))
        user = self.db.execute('SELECT * FROM employees WHERE max_id=%s FOR UPDATE', (uid,)).fetchone()
        if uid == self.cfg.admin_max_id and user['role'] != 'admin':
            user = self.db.execute("UPDATE employees SET role='admin', status='approved', full_name='Администратор' WHERE max_id=%s RETURNING *", (uid,)).fetchone()
        return user

    def require(self, user, admin=False):
        if user['status'] != 'approved':
            raise RuleError('Доступ закрыт: ожидайте одобрения администратора или обратитесь к нему при блокировке.')
        if admin and user['role'] != 'admin':
            raise RuleError('Действие доступно только администратору.')

    def stamp(self, value):
        from zoneinfo import ZoneInfo
        return value.astimezone(ZoneInfo(self.cfg.org_timezone)).strftime('%d.%m.%Y %H:%M:%S %Z')

    def current(self, uid):
        return self.db.execute('SELECT * FROM shifts WHERE employee_id=%s AND ended_at IS NULL', (uid,)).fetchone()

    def token(self, uid):
        row = self.db.execute('SELECT id,version FROM shifts WHERE employee_id=%s ORDER BY id DESC LIMIT 1', (uid,)).fetchone()
        return f"{row['id']}.{row['version']}" if row else '0.0'

    def menu(self, uid):
        buttons = []
        for label, action in ACTIONS.items():
            buttons.append([{'type': 'callback', 'text': label, 'payload': f'{action}|{self.token(uid)}'}])
        return {'type': 'inline_keyboard', 'payload': {'buttons': buttons}}

    def status(self, uid):
        shift = self.current(uid)
        if not shift:
            return 'Не работаете. Открытой смены нет.'
        rest = self.db.execute('SELECT * FROM breaks WHERE shift_id=%s AND ended_at IS NULL', (shift['id'],)).fetchone()
        return f"{'На перерыве' if rest else 'Работаете'}. Смена №{shift['id']}, начало: {self.stamp(shift['started_at'])}." + (f" Перерыв с {self.stamp(rest['started_at'])}." if rest else '')

    def transition(self, uid, action):
        shift = self.current(uid)
        if action == 'start':
            if shift:
                return 'Смена уже открыта.'
            last = self.db.execute('SELECT max(ended_at) AS t FROM shifts WHERE employee_id=%s', (uid,)).fetchone()['t']
            if last and self.now < last:
                raise RuleError('Время события раньше окончания предыдущей смены.')
            self.db.execute('INSERT INTO shifts(employee_id,started_at) VALUES(%s,%s)', (uid, self.now))
            return 'Работа начата.'
        if not shift:
            raise RuleError('Сначала начните работу.')
        rest = self.db.execute('SELECT * FROM breaks WHERE shift_id=%s AND ended_at IS NULL', (shift['id'],)).fetchone()
        latest = self.db.execute('SELECT max(t) AS t FROM (SELECT started_at t FROM breaks WHERE shift_id=%s UNION ALL SELECT ended_at t FROM breaks WHERE shift_id=%s) x', (shift['id'], shift['id'])).fetchone()['t']
        if self.now < max(shift['started_at'], latest or shift['started_at']):
            raise RuleError('Событие пришло не по порядку. Отправьте актуальное действие или запрос исправления.')
        if action == 'pause':
            if rest:
                return 'Перерыв уже начат.'
            self.db.execute('INSERT INTO breaks(shift_id,started_at) VALUES(%s,%s)', (shift['id'], self.now))
            result = 'Перерыв начат.'
        elif action == 'resume':
            if not rest:
                return 'Активного перерыва нет.'
            self.db.execute('UPDATE breaks SET ended_at=%s WHERE id=%s', (self.now, rest['id']))
            result = 'Перерыв завершен.'
        else:
            if self.now <= shift['started_at']:
                raise RuleError('Завершение должно быть позже начала смены.')
            if rest:
                self.db.execute('UPDATE breaks SET ended_at=%s WHERE id=%s', (self.now, rest['id']))
            self.db.execute('UPDATE shifts SET ended_at=%s WHERE id=%s', (self.now, shift['id']))
            intervals = self.breaks(shift['id'])
            gross, pause, net = totals(shift['started_at'], self.now, intervals, shift['started_at'], self.now)
            result = f'Смена №{shift["id"]} завершена. Длительность: {duration(gross)}, перерывы: {duration(pause)}, чистое время: {duration(net)}.'
        self.db.execute('UPDATE shifts SET version=version+1 WHERE id=%s', (shift['id'],))
        return result

    def breaks(self, sid):
        return [(x['started_at'], x['ended_at']) for x in self.db.execute('SELECT * FROM breaks WHERE shift_id=%s ORDER BY started_at', (sid,)).fetchall()]

    def snapshot(self, shift):
        return {'start': shift['started_at'].isoformat(), 'end': shift['ended_at'].isoformat(),
                'breaks': [[a.isoformat(), b.isoformat()] for a, b in self.breaks(shift['id'])]}

    def request(self, uid, args):
        spec, sep, reason = args.partition('|')
        parts = spec.split()
        if not sep or not reason.strip() or len(reason.strip()) > 1000 or len(parts) != 4:
            raise RuleError('Укажите ID, начало, конец, перерывы и | причину (до 1000 символов). /help — пример.')
        sid = positive_id(parts[0])
        shift = self.db.execute('SELECT * FROM shifts WHERE id=%s AND employee_id=%s', (sid, uid)).fetchone()
        if not shift or not shift['ended_at']:
            raise RuleError('Можно исправить только свою завершенную смену.')
        start, end = aware(parts[1]), aware(parts[2])
        intervals = [] if parts[3] == '-' else [tuple(aware(x) for x in item.split('/')) for item in parts[3].split(',')]
        if any(len(x) != 2 for x in intervals):
            raise RuleError('Перерывы: начало/конец,начало/конец либо -.')
        validate_intervals(start, end, intervals)
        if end > self.now:
            raise RuleError('Нельзя указывать будущее окончание.')
        proposal = {'start': start.isoformat(), 'end': end.isoformat(), 'breaks': [[a.isoformat(), b.isoformat()] for a, b in sorted(intervals)]}
        row = self.db.execute('INSERT INTO corrections(shift_id,author_id,reason,old_values,new_values,base_version) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id', (sid, uid, reason.strip(), Jsonb(self.snapshot(shift)), Jsonb(proposal), shift['version'])).fetchone()
        return f'Заявка №{row["id"]} отправлена. Время изменится после одобрения администратора.'

    def review(self, uid, cid, approve):
        request = self.db.execute('SELECT * FROM corrections WHERE id=%s FOR UPDATE', (cid,)).fetchone()
        if not request or request['status'] != 'pending':
            raise RuleError('Заявка не найдена или уже рассмотрена.')
        if approve:
            shift = self.db.execute('SELECT * FROM shifts WHERE id=%s FOR UPDATE', (request['shift_id'],)).fetchone()
            if shift['version'] != request['base_version']:
                raise RuleError('Смена уже изменена. Отклоните устаревшую заявку и запросите новую.')
            data = request['new_values']
            start, end = aware(data['start']), aware(data['end'])
            intervals = [(aware(a), aware(b)) for a, b in data['breaks']]
            validate_intervals(start, end, intervals)
            if self.db.execute('SELECT 1 FROM shifts WHERE employee_id=%s AND id<>%s AND started_at<%s AND (ended_at IS NULL OR ended_at>%s)', (request['author_id'], shift['id'], end, start)).fetchone():
                raise RuleError('Исправление пересекает другую смену сотрудника.')
            self.db.execute('UPDATE shifts SET started_at=%s,ended_at=%s,version=version+1 WHERE id=%s', (start, end, shift['id']))
            self.db.execute('DELETE FROM breaks WHERE shift_id=%s', (shift['id'],))
            for a, b in intervals:
                self.db.execute('INSERT INTO breaks(shift_id,started_at,ended_at) VALUES(%s,%s,%s)', (shift['id'], a, b))
            self.db.execute('INSERT INTO audit(correction_id,author_id,reviewer_id,reason,old_values,new_values) VALUES(%s,%s,%s,%s,%s,%s)', (cid, request['author_id'], uid, request['reason'], Jsonb(request['old_values']), Jsonb(data)))
        self.db.execute('UPDATE corrections SET status=%s,reviewer_id=%s,reviewed_at=%s WHERE id=%s', ('approved' if approve else 'rejected', uid, self.now, cid))
        # Notification is authorization-checked again before actual delivery.
        self.enqueue(request['author_id'], f'Заявка №{cid}: {"одобрена" if approve else "отклонена"}.', sensitive=True)
        return f'Заявка №{cid} {"одобрена" if approve else "отклонена"}.'

    def report(self, user, args, admin=False, csv_export=False):
        self.require(user, admin)
        parts = args.split() or ['сегодня']
        kind = parts.pop(0)
        dates = ()
        if kind not in ('сегодня', 'неделя', 'месяц'):
            if not parts:
                raise RuleError('Укажите две даты периода.')
            dates = (kind, parts.pop(0))
        left, right = period(kind, self.now, self.cfg.org_timezone, dates)
        target = parts.pop(0) if parts and admin else str(user['max_id']) if not admin else 'all'
        if parts or (not admin and target != str(user['max_id'])):
            raise RuleError('Некорректные параметры отчета.')
        users = self.db.execute('SELECT * FROM employees WHERE (%s OR max_id=%s) ORDER BY lower(full_name), max_id', (target == 'all', positive_id(target) if target != 'all' else 0)).fetchall()
        rows, lines = [], [f'Период: {self.stamp(left)} — {self.stamp(right)} (конец не включен).']
        for employee in users:
            uid = employee['max_id']
            shifts = self.db.execute('SELECT * FROM shifts WHERE employee_id=%s AND ended_at IS NOT NULL AND started_at<%s AND ended_at>%s ORDER BY started_at,id', (uid, right, left)).fetchall()
            gross = rest = net = 0
            for shift in shifts:
                a, b, c = totals(shift['started_at'], shift['ended_at'], self.breaks(shift['id']), left, right)
                gross += a; rest += b; net += c
            opens = self.db.execute('SELECT * FROM shifts WHERE employee_id=%s AND ended_at IS NULL', (uid,)).fetchall()
            label = employee['full_name'] or f'ID {uid}'
            lines.append(f'{label} (ID {uid}): смен {len(shifts)}, длительность {duration(gross)}, перерывы {duration(rest)}, чистое {duration(net)}. Незакрытых: {len(opens)} (в итог не включены).')
            for shift in opens:
                lines.append(f'Открыта №{shift["id"]} с {self.stamp(shift["started_at"])}.')
            visible_opens = [shift for shift in opens if shift['started_at'] < right and self.now >= left]
            for shift in sorted(shifts + visible_opens, key=lambda item: (item['started_at'], item['id'])):
                rows.append(interval_row(label, shift, self.breaks(shift['id']) if shift['ended_at'] else [],
                                         left, right, self.cfg.org_timezone))
        if admin:
            lines = [lines[0], f'Время: {self.cfg.org_timezone}. Одна строка — один интервал начала и окончания работы.']
            lines.extend(interval_text(row) for row in rows)
            if not rows:
                lines.append('За выбранный период интервалов работы нет.')
        if csv_export:
            out = io.StringIO(newline='')
            writer = csv.writer(out, delimiter=';')
            writer.writerow(HEADERS)
            for row in rows:
                writer.writerow([("'" + x) if isinstance(x, str) and x.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else x for x in row])
            return '\n'.join(lines), out.getvalue().encode('utf-8-sig')
        return '\n'.join(lines), None

    def enqueue(self, uid, text, csv_data=None, sensitive=False, menu=False, callback_id=None):
        # Messages obey the documented 4000-character limit; CSV never appears in a URL.
        for offset in range(0, len(text), 3500):
            body = {'text': text[offset:offset + 3500]}
            if menu and offset == 0:
                body['attachments'] = [self.menu(uid)]
            self.db.execute('INSERT INTO outbox(recipient,body,csv_data,sensitive,callback_id) VALUES(%s,%s,%s,%s,%s)', (uid, Jsonb(body), csv_data if offset == 0 else None, sensitive, callback_id if offset == 0 else None))

    def handle(self, uid, text, button=False):
        user = self.employee(uid)
        if text.strip() == '/id':
            return f'Ваш MAX user_id: {uid}', None
        if user['status'] == 'blocked':
            raise RuleError('Доступ заблокирован. Обратитесь к администратору.')
        if user['status'] == 'pending':
            return 'Заявка уже отправлена. Ожидайте одобрения администратора.', None
        if user['status'] == 'new':
            if text.startswith('/register '):
                name = text.removeprefix('/register ').strip()
            elif not text.startswith('/') and text not in ACTIONS and not button:
                name = text.strip()
            else:
                return 'Введите ФИО одним сообщением (или /register Фамилия Имя Отчество). После этого потребуется одобрение администратора.', None
            if not 5 <= len(name) <= 200 or len(name.split()) < 2 or '\x00' in name:
                raise RuleError('Введите ФИО: от 5 до 200 символов, минимум два слова.')
            self.db.execute("UPDATE employees SET full_name=%s,status='pending' WHERE max_id=%s", (name, uid))
            # Persist the notification in the same transaction as registration.
            # Only the new -> pending transition notifies, so repeated requests do not.
            admins = self.db.execute("SELECT max_id FROM employees WHERE role='admin' AND status='approved'").fetchall()
            for admin in admins:
                self.enqueue(admin['max_id'],
                             f'Новая заявка на регистрацию\nФИО: {name}\nMAX ID: {uid}\n'
                             f'Для одобрения отправьте:\n/approve {uid}\n'
                             f'Для блокировки: /block {uid}', sensitive=True)
            return 'Заявка регистрации отправлена. Ожидайте одобрения администратора.', None
        self.require(user)
        cmd, _, args = text.strip().partition(' ')
        if button:
            cmd, _, expected = text.partition('|')
            if cmd in ('start', 'pause', 'resume', 'stop') and expected != self.token(uid):
                raise RuleError('Эта кнопка устарела. Откройте /menu и проверьте текущий статус.')
        else:
            cmd = ACTIONS.get(text.strip(), cmd.lstrip('/'))
        if cmd in ('start', 'pause', 'resume', 'stop'):
            # /start is also the standard welcome command: it never clocks in.
            if text == '/start' and not button:
                return HELP + ('\n' + ADMIN_HELP if user['role'] == 'admin' else ''), None
            result = self.transition(uid, cmd)
            return f'{result}\nВремя события: {self.stamp(self.now)}\n{self.status(uid)}', None
        if cmd == 'status':
            return f'{self.stamp(self.now)}\n{self.status(uid)}', None
        if cmd in ('help', 'menu'):
            return HELP + ('\n' + ADMIN_HELP if user['role'] == 'admin' else ''), None
        if cmd == 'hours':
            if args:
                return self.report(user, args)
            return '\n\n'.join(self.report(user, kind)[0] for kind in ('сегодня', 'неделя', 'месяц')) + '\nДиапазон: /hours ГГГГ-ММ-ДД ГГГГ-ММ-ДД', None
        if cmd == 'shifts':
            rows = self.db.execute('SELECT * FROM shifts WHERE employee_id=%s ORDER BY id DESC LIMIT 30', (uid,)).fetchall()
            return '\n'.join(f'№{r["id"]}: {self.stamp(r["started_at"])} — {self.stamp(r["ended_at"]) if r["ended_at"] else "открыта"}' for r in rows) or 'Смен пока нет.', None
        if cmd == 'correct':
            return self.request(uid, args), None
        if cmd == 'requests':
            rows = self.db.execute("SELECT c.*,e.full_name FROM corrections c JOIN employees e ON e.max_id=c.author_id WHERE (%s OR author_id=%s) ORDER BY c.id DESC LIMIT 50", (user['role'] == 'admin', uid)).fetchall()
            statuses = {'pending': 'ожидает решения', 'approved': 'одобрена', 'rejected': 'отклонена'}
            return '\n'.join(f'№{r["id"]}, смена №{r["shift_id"]}, {r["full_name"]}: {statuses[r["status"]]}; причина: {r["reason"]}' for r in rows) or 'Заявок нет.', None
        self.require(user, admin=True)
        if cmd in ('approve', 'block'):
            target = positive_id(args)
            if target == self.cfg.admin_max_id:
                raise RuleError('Нельзя изменить доступ первого администратора.')
            row = self.db.execute("UPDATE employees SET status=%s WHERE max_id=%s AND status<> 'new' RETURNING max_id", ('approved' if cmd == 'approve' else 'blocked', target)).fetchone()
            if not row:
                raise RuleError('Сотрудник не найден или не ввел ФИО.')
            if cmd == 'approve':
                self.enqueue(target, 'Регистрация одобрена. Откройте /menu.', sensitive=True)
            return 'Доступ сотрудника обновлен.', None
        if cmd == 'users':
            rows = self.db.execute('SELECT * FROM employees ORDER BY max_id').fetchall()
            labels = {'new':'не ввел ФИО','pending':'ожидает одобрения','approved':'доступ разрешен','blocked':'заблокирован'}
            return '\n'.join(f'{r["max_id"]}: {r["full_name"]} — {labels[r["status"]]}' for r in rows), None
        if cmd == 'active':
            rows = self.db.execute('SELECT s.*,e.full_name FROM shifts s JOIN employees e ON e.max_id=s.employee_id WHERE ended_at IS NULL ORDER BY started_at').fetchall()
            return '\n'.join(f'{r["full_name"]} (ID {r["employee_id"]}): {self.status(r["employee_id"])}' for r in rows) or 'Открытых смен нет.', None
        if cmd in ('report', 'csv'):
            return self.report(user, args, admin=True, csv_export=cmd == 'csv')
        if cmd == 'review':
            row = self.db.execute('SELECT * FROM corrections WHERE id=%s', (positive_id(args),)).fetchone()
            if not row:
                raise RuleError('Заявка не найдена.')
            return f'Заявка №{row["id"]}; автор MAX ID {row["author_id"]}; дата {self.stamp(row["created_at"])}; причина: {row["reason"]}\nБыло (UTC): {json.dumps(row["old_values"], ensure_ascii=False)}\nПредложено (UTC): {json.dumps(row["new_values"], ensure_ascii=False)}', None
        if cmd in ('accept', 'reject'):
            return self.review(uid, positive_id(args), cmd == 'accept'), None
        raise RuleError('Неизвестная команда. Откройте /help.')
