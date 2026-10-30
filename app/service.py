"""MAX-independent accounting service. Caller owns a PostgreSQL transaction."""

import secrets
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from app.db import BUSINESS_LOCK
from app.timecalc import RuleError, duration, local_datetime, period, totals

ACTIONS = [
    ("Начать работу", "/начать"),
    ("Начать перерыв", "/перерыв"),
    ("Закончить перерыв", "/вернуться"),
    ("Завершить работу", "/закончить"),
    ("Мой статус", "/статус"),
    ("Мои часы", "/часы"),
]
HELP = """Меню: /начать /перерыв /вернуться /закончить /статус /часы
/часы сегодня|неделя|месяц или /часы 2026-10-01 2026-10-31
/смены — последние 20 смен с ID.
/исправить ID | начало | окончание | перерывы | причина
Даты: ГГГГ-ММ-ДД ЧЧ:ММ. Перерывы: начало / конец; начало / конец, либо «-».
/мои_запросы — решения по исправлениям.
/мой_id — ваш стабильный MAX ID."""
ADMIN_HELP = """
Администратор:
/сотрудники [номер страницы] — по 20 записей
/одобрить MAX_ID • /блокировать MAX_ID
/назначить_админа MAX_ID — назначить одобренного сотрудника администратором
/активные [номер страницы] — открытые смены
/запросы [номер страницы] — ожидающие исправления
/запрос ID — подробности
/принять ID • /отклонить ID
/отчет сегодня|неделя|месяц [MAX_ID]
/отчет ГГГГ-ММ-ДД ГГГГ-ММ-ДД [MAX_ID]
/csv период [MAX_ID] — файл Excel CSV в личном диалоге."""


class Service:
    def __init__(self, conn, config, now=None):
        self.c, self.cfg = conn, config
        self.now = now or datetime.now(UTC)

    def stamp(self, value):
        return value.astimezone(ZoneInfo(self.cfg.org_timezone)).strftime("%d.%m.%Y %H:%M:%S %Z")

    def emit(self, uid, text, keyboard=False):
        # MAX limits each message to 4000 characters. Every chunk is durable.
        for offset in range(0, len(text), 3500):
            payload = {"text": text[offset : offset + 3500]}
            if keyboard and offset + 3500 >= len(text):
                token = secrets.token_hex(12)
                self.c.execute("UPDATE employees SET menu_token=%s WHERE max_id=%s", (token, uid))
                buttons = [
                    {"type": "callback", "text": label, "payload": f"{token}:{cmd}"} for label, cmd in ACTIONS
                ]
                payload["attachments"] = [
                    {
                        "type": "inline_keyboard",
                        "payload": {"buttons": [buttons[n : n + 2] for n in range(0, len(buttons), 2)]},
                    }
                ]
            self.queue(uid, "message", payload)

    def queue(self, uid, kind, payload):
        user = self.employee(uid)
        payload = {**payload, "_access": {"status": user["status"], "role": user["role"]}}
        self.c.execute(
            "INSERT INTO outbox(recipient_id,kind,payload) VALUES (%s,%s,%s)", (uid, kind, Jsonb(payload))
        )

    def employee(self, uid):
        return self.c.execute("SELECT * FROM employees WHERE max_id=%s", (uid,)).fetchone()

    def approved(self, user):
        if user["status"] != "approved":
            raise RuleError(
                "Учет недоступен: ожидайте одобрения администратора или обратитесь к нему при блокировке."
            )

    def admin(self, user):
        self.approved(user)
        if user["role"] != "admin":
            raise RuleError("Действие доступно только администратору.")

    def handle(self, event):
        """Normalized, authenticated private-dialog event, not a raw MAX JSON."""
        self.c.execute("SELECT pg_advisory_xact_lock(%s)", (BUSINESS_LOCK,))
        uid = event["user_id"]
        initial = uid == self.cfg.initial_admin_max_id
        self.c.execute(
            """INSERT INTO employees(max_id,role,status,full_name) VALUES (%s,%s,%s,%s)
                          ON CONFLICT (max_id) DO NOTHING""",
            (
                uid,
                "admin" if initial else "employee",
                "approved" if initial else "pending",
                "Администратор" if initial else "",
            ),
        )
        user = self.employee(uid)
        text = event["text"].strip()
        callback_id = event.get("callback_id")
        if callback_id:
            self.queue(uid, "answer", {"callback_id": callback_id, "notification": "Запрос получен"})
        try:
            if callback_id:
                token, sep, text = text.partition(":")
                if not sep or not secrets.compare_digest(token, user["menu_token"]) or not token:
                    raise RuleError("Это меню уже использовано. Выберите действие в новом меню.")
            # UI callbacks and commands ultimately use the same server-side permissions.
            text = dict(ACTIONS).get(text, text)
            cmd, _, args = text.partition(" ")
            cmd = cmd.lower()
            args = args.strip()
            if cmd == "/мой_id":
                self.emit(uid, f"Ваш MAX ID: {uid}")
                return
            if user["status"] == "blocked":
                raise RuleError("Доступ заблокирован. Обратитесь к администратору.")
            if cmd == "/start":
                self.emit(
                    uid,
                    "Введите ФИО одной строкой или /регистрация ФИО."
                    if not user["full_name"]
                    else self.welcome(user),
                    user["status"] == "approved",
                )
                return
            if cmd == "/регистрация" or (not user["full_name"] and not text.startswith("/")):
                name = args if cmd == "/регистрация" else text
                if not 5 <= len(name) <= 200 or len(name.split()) < 2 or any(ord(c) < 32 for c in name):
                    raise RuleError("Введите ФИО: 2–3 слова, от 5 до 200 символов.")
                if user["full_name"] and user["status"] == "approved":
                    raise RuleError("Вы уже зарегистрированы. Изменение ФИО согласуйте с администратором.")
                self.c.execute("UPDATE employees SET full_name=%s WHERE max_id=%s", (name, uid))
                self.emit(uid, "Заявка отправлена. Дождитесь одобрения администратора.")
                self.notify_admins(f"Заявка сотрудника: {name}, MAX ID {uid}. /одобрить {uid}")
                return
            self.approved(user)
            if cmd in ("/начать", "/перерыв", "/вернуться", "/закончить"):
                when = datetime.fromisoformat(event["at"])
                if when > self.now + timedelta(minutes=5):
                    raise RuleError("Время события находится в будущем. Проверьте часы сервера.")
                self.transition(user, cmd, when)
                return
            if cmd == "/статус":
                self.emit(uid, self.status(uid), True)
            elif cmd == "/часы":
                specs = [args] if args else ["сегодня", "неделя", "месяц"]
                texts = [
                    self.report_text(self.report(uid, *period(s, self.now, self.cfg.org_timezone)), s)
                    for s in specs
                ]
                self.emit(uid, "\n\n".join(texts), True)
            elif cmd == "/смены":
                shifts = self.c.execute(
                    "SELECT * FROM shifts WHERE employee_id=%s ORDER BY id DESC LIMIT 20", (uid,)
                ).fetchall()
                self.emit(
                    uid, "Последние смены:\n" + ("\n".join(self.shift_text(s) for s in shifts) or "Нет смен.")
                )
            elif cmd == "/исправить":
                self.request_correction(user, args)
            elif cmd == "/мои_запросы":
                rows = self.c.execute(
                    "SELECT id,status FROM corrections WHERE employee_id=%s ORDER BY id DESC LIMIT 20", (uid,)
                ).fetchall()
                labels = {"pending": "ожидает решения", "approved": "одобрено", "rejected": "отклонено"}
                self.emit(
                    uid, "\n".join(f"№{r['id']}: {labels[r['status']]}" for r in rows) or "Запросов нет."
                )
            elif cmd in (
                "/сотрудники",
                "/одобрить",
                "/блокировать",
                "/активные",
                "/запросы",
                "/запрос",
                "/принять",
                "/отклонить",
                "/отчет",
                "/csv",
                "/назначить_админа",
            ):
                self.admin(user)
                self.admin_command(user, cmd, args)
            else:
                self.emit(uid, self.welcome(user), True)
        except (RuleError, ValueError, OverflowError) as exc:
            text = (
                str(exc) if isinstance(exc, RuleError) else "Некорректные параметры. /помощь — список команд."
            )
            self.emit(uid, text, user["status"] == "approved")

    def welcome(self, user):
        return HELP + (ADMIN_HELP if user["role"] == "admin" else "")

    def notify_admins(self, text):
        for a in self.c.execute(
            "SELECT max_id FROM employees WHERE role='admin' AND status='approved'"
        ).fetchall():
            self.emit(a["max_id"], text)

    def open_shift(self, uid):
        return self.c.execute(
            "SELECT * FROM shifts WHERE employee_id=%s AND ended_at IS NULL", (uid,)
        ).fetchone()

    def status(self, uid):
        s = self.open_shift(uid)
        if not s:
            return "Статус: не работает."
        b = self.c.execute(
            "SELECT * FROM breaks WHERE shift_id=%s AND ended_at IS NULL", (s["id"],)
        ).fetchone()
        return (
            f"Статус: {'на перерыве' if b else 'работает'}.\n"
            f"Открытая смена №{s['id']} с {self.stamp(s['started_at'])}."
            + (f"\nПерерыв с {self.stamp(b['started_at'])}." if b else "")
            + "\nОткрытая смена не включена в итоги."
        )

    def transition(self, user, action, when):
        uid = user["max_id"]
        self.approved(user)
        s = self.open_shift(uid)
        b = (
            self.c.execute(
                "SELECT * FROM breaks WHERE shift_id=%s AND ended_at IS NULL", (s["id"],)
            ).fetchone()
            if s
            else None
        )
        if action == "/начать":
            if s:
                raise RuleError("Смена уже открыта.\n" + self.status(uid))
            last = self.c.execute(
                "SELECT max(ended_at) AS ended FROM shifts WHERE employee_id=%s", (uid,)
            ).fetchone()
            if last["ended"] and when < last["ended"]:
                raise RuleError(
                    "Событие пришло с опозданием и пересекает прежнюю смену. Создайте запрос исправления."
                )
            self.c.execute("INSERT INTO shifts(employee_id,started_at) VALUES (%s,%s)", (uid, when))
            label = "Работа начата"
        else:
            if not s:
                raise RuleError("Нет открытой смены.\nСтатус: не работает.")
            latest = self.c.execute(
                "SELECT max(COALESCE(ended_at,started_at)) AS at FROM breaks WHERE shift_id=%s", (s["id"],)
            ).fetchone()["at"]
            if when < max(s["started_at"], latest or s["started_at"]):
                raise RuleError("Событие старше последнего действия. Используйте запрос исправления.")
            if action == "/перерыв":
                if b:
                    raise RuleError("Перерыв уже начат.\n" + self.status(uid))
                self.c.execute("INSERT INTO breaks(shift_id,started_at) VALUES (%s,%s)", (s["id"], when))
                label = "Перерыв начат"
            elif action == "/вернуться":
                if not b:
                    raise RuleError("Нет активного перерыва.\n" + self.status(uid))
                self.c.execute("UPDATE breaks SET ended_at=%s WHERE id=%s", (when, b["id"]))
                label = "Перерыв завершен"
            else:
                if b:
                    self.c.execute("UPDATE breaks SET ended_at=%s WHERE id=%s", (when, b["id"]))
                self.c.execute("UPDATE shifts SET ended_at=%s WHERE id=%s", (when, s["id"]))
                label = "Работа завершена"
                # Exact shift totals, independent of any earlier overlapping reporting interval.
                parts = self.c.execute("SELECT * FROM breaks WHERE shift_id=%s", (s["id"],)).fetchall()
                gross = (when - s["started_at"]).total_seconds()
                paused = sum((x["ended_at"] - x["started_at"]).total_seconds() for x in parts)
                label += (
                    f"\nСмена №{s['id']}: {duration(gross)}, перерывы: {duration(paused)}, "
                    f"чистое время: {duration(gross - paused)}"
                )
            self.c.execute("UPDATE shifts SET version=version+1 WHERE id=%s", (s["id"],))
        self.emit(uid, f"{label}.\nВремя события: {self.stamp(when)}\n{self.status(uid)}", True)

    def shift_text(self, s):
        return f"№{s['id']}: {self.stamp(s['started_at'])} → " + (
            self.stamp(s["ended_at"]) if s["ended_at"] else "открыта"
        )

    def report(self, uid, start, end):
        employees = self.c.execute(
            "SELECT * FROM employees WHERE (%s::bigint IS NULL OR max_id=%s) "
            "AND full_name<>'' ORDER BY max_id",
            (uid, uid),
        ).fetchall()
        rows = []
        for employee in employees:
            shifts = self.c.execute(
                """SELECT * FROM shifts WHERE employee_id=%s
                AND started_at<%s AND (ended_at>=%s OR ended_at IS NULL)""",
                (employee["max_id"], end, start),
            ).fetchall()
            breaks = self.c.execute(
                "SELECT b.* FROM breaks b JOIN shifts s ON s.id=b.shift_id "
                "WHERE s.employee_id=%s AND s.started_at<%s AND s.ended_at>%s",
                (employee["max_id"], end, start),
            ).fetchall()
            # Open shifts shown irrespective of selected period, explicitly marked as current.
            opened = self.open_shift(employee["max_id"])
            rows.append(
                {
                    "max_id": employee["max_id"],
                    "full_name": employee["full_name"],
                    **totals(shifts, breaks, start, end),
                    "open_count": int(opened is not None),
                    "open_shift": self.shift_text(opened) if opened else "",
                }
            )
        return rows

    def report_text(self, rows, spec):
        result = [f"Период: {spec}. Часовой пояс: {self.cfg.org_timezone}."]
        for r in rows:
            result.append(
                f"{r['full_name']} (ID {r['max_id']}): смен {r['count']}; "
                f"длительность {duration(r['gross'])}; перерывы {duration(r['breaks'])}; "
                f"чистое время {duration(r['net'])}."
                + (f"\nСейчас открыта {r['open_shift']}; в итоги не входит." if r["open_count"] else "")
            )
        return "\n".join(result)

    def snapshot(self, s):
        parts = self.c.execute(
            "SELECT * FROM breaks WHERE shift_id=%s ORDER BY started_at,id", (s["id"],)
        ).fetchall()
        return {
            "start": s["started_at"].isoformat(),
            "end": s["ended_at"].isoformat() if s["ended_at"] else None,
            "breaks": [
                [b["started_at"].isoformat(), b["ended_at"].isoformat() if b["ended_at"] else None]
                for b in parts
            ],
        }

    def validate_correction(self, uid, sid, proposal):
        start, end = (datetime.fromisoformat(proposal[k]) for k in ("start", "end"))
        if end <= start or end > self.now:
            raise RuleError("Окончание должно быть после начала и не в будущем.")
        if self.c.execute(
            """SELECT id FROM shifts WHERE employee_id=%s AND id<>%s
            AND started_at<%s AND (ended_at IS NULL OR ended_at>%s) LIMIT 1""",
            (uid, sid, end, start),
        ).fetchone():
            raise RuleError("Исправленная смена пересекается с другой сменой.")
        last = start
        for a, b in proposal["breaks"]:
            a, b = datetime.fromisoformat(a), datetime.fromisoformat(b)
            if not start <= a < b <= end or a < last:
                raise RuleError("Перерывы должны идти по порядку, не пересекаться и находиться внутри смены.")
            last = b
        return start, end

    def request_correction(self, user, args):
        values = [v.strip() for v in args.split("|")]
        if len(values) != 5:
            raise RuleError(HELP)
        sid = int(values[0])
        s = self.c.execute(
            "SELECT * FROM shifts WHERE id=%s AND employee_id=%s", (sid, user["max_id"])
        ).fetchone()
        if not s:
            raise RuleError("Ваша смена с таким ID не найдена.")
        reason = values[4]
        if not 5 <= len(reason) <= 1000:
            raise RuleError("Укажите причину длиной 5–1000 символов.")
        proposal = {
            "start": local_datetime(values[1], self.cfg.org_timezone).isoformat(),
            "end": local_datetime(values[2], self.cfg.org_timezone).isoformat(),
            "breaks": [],
        }
        if values[3] != "-":
            for part in values[3].split(";"):
                a, b = part.split("/")
                proposal["breaks"].append(
                    [local_datetime(x, self.cfg.org_timezone).isoformat() for x in (a, b)]
                )
        if len(proposal["breaks"]) > 50:
            raise RuleError("Не более 50 перерывов в одной смене.")
        self.validate_correction(user["max_id"], sid, proposal)
        row = self.c.execute(
            """INSERT INTO corrections(employee_id,shift_id,reason,old_values,new_values,shift_version)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (user["max_id"], sid, reason, Jsonb(self.snapshot(s)), Jsonb(proposal), s["version"]),
        ).fetchone()
        self.emit(user["max_id"], f"Запрос №{row['id']} отправлен. Учет пока не изменен.")
        self.notify_admins(f"Запрос исправления №{row['id']}, смена №{sid}. /запрос {row['id']}")

    def audit(self, author, action, entity, reason, old, new):
        self.c.execute(
            """INSERT INTO audit_log(author_id,action,entity_id,reason,old_values,new_values)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (author, action, entity, reason, Jsonb(old), Jsonb(new)),
        )

    def decide(self, user, request_id, approve):
        row = self.c.execute("SELECT * FROM corrections WHERE id=%s", (request_id,)).fetchone()
        if not row:
            raise RuleError("Запрос не найден.")
        if row["status"] != "pending":
            raise RuleError("По этому запросу уже принято решение.")
        if row["employee_id"] == user["max_id"]:
            raise RuleError("Нельзя рассматривать собственное исправление. Требуется другой администратор.")
        if approve:
            target = self.employee(row["employee_id"])
            self.approved(target)
            s = self.c.execute("SELECT * FROM shifts WHERE id=%s", (row["shift_id"],)).fetchone()
            if s["version"] != row["shift_version"]:
                raise RuleError(
                    "Смена изменилась после заявки. Отклоните запрос; сотрудник должен создать новый."
                )
            start, end = self.validate_correction(row["employee_id"], s["id"], row["new_values"])
            self.c.execute(
                "UPDATE shifts SET started_at=%s,ended_at=%s,version=version+1 WHERE id=%s",
                (start, end, s["id"]),
            )
            self.c.execute("DELETE FROM breaks WHERE shift_id=%s", (s["id"],))
            for a, b in row["new_values"]["breaks"]:
                self.c.execute(
                    "INSERT INTO breaks(shift_id,started_at,ended_at) VALUES (%s,%s,%s)", (s["id"], a, b)
                )
        status = "approved" if approve else "rejected"
        self.c.execute(
            "UPDATE corrections SET status=%s,decided_by=%s,decided_at=%s WHERE id=%s",
            (status, user["max_id"], self.now, request_id),
        )
        self.audit(
            user["max_id"],
            f"correction_{status}",
            request_id,
            row["reason"],
            row["old_values"],
            row["new_values"],
        )
        message = f"Запрос №{request_id}: {'одобрен, время исправлено' if approve else 'отклонен'}."
        self.emit(user["max_id"], message)
        self.emit(row["employee_id"], message)

    def admin_command(self, user, cmd, args):
        uid = user["max_id"]
        if cmd == "/назначить_админа":
            target = self.employee(int(args))
            if not target or target["status"] != "approved":
                raise RuleError("Сначала одобрите регистрацию сотрудника.")
            self.c.execute(
                "UPDATE employees SET role='admin',menu_token='' WHERE max_id=%s", (target["max_id"],)
            )
            self.audit(uid, "grant_admin", target["max_id"], cmd, {"role": target["role"]}, {"role": "admin"})
            self.emit(uid, "Администратор назначен.")
            self.emit(target["max_id"], "Вам назначена роль администратора. /помощь — команды.")
        elif cmd in ("/одобрить", "/блокировать"):
            target = self.employee(int(args))
            if not target or not target["full_name"]:
                raise RuleError("Сотрудник не найден или не ввел ФИО.")
            if target["role"] == "admin":
                raise RuleError("Эта команда не изменяет администраторов.")
            status = "approved" if cmd == "/одобрить" else "blocked"
            self.c.execute(
                "UPDATE employees SET status=%s,menu_token='' WHERE max_id=%s", (status, target["max_id"])
            )
            self.audit(
                uid,
                "employee_status",
                target["max_id"],
                cmd,
                {"status": target["status"]},
                {"status": status},
            )
            self.emit(uid, "Статус сотрудника обновлен.")
            self.emit(
                target["max_id"],
                "Регистрация одобрена. /помощь — меню."
                if status == "approved"
                else "Ваш доступ заблокирован.",
                status == "approved",
            )
        elif cmd in ("/принять", "/отклонить"):
            self.decide(user, int(args), cmd == "/принять")
        elif cmd in ("/отчет", "/csv"):
            parts = args.split()
            if not parts:
                raise RuleError("Укажите период. Например: /отчет месяц или /csv 2026-10-01 2026-10-31")
            named = parts[0] in ("сегодня", "день", "неделя", "месяц")
            n = 1 if named else 2
            if len(parts) not in (n, n + 1):
                raise RuleError("Укажите период и необязательный MAX ID сотрудника.")
            spec = " ".join(parts[:n])
            target = int(parts[n]) if len(parts) > n else None
            if target is not None and not self.employee(target):
                raise RuleError("Сотрудник не найден.")
            start, end = period(spec, self.now, self.cfg.org_timezone)
            if cmd == "/csv":
                # Query again at dispatch, checking the role and current permissions before upload.
                self.queue(
                    uid,
                    "csv",
                    {"target": target, "start": start.isoformat(), "end": end.isoformat(), "spec": spec},
                )
                self.emit(uid, "CSV поставлен в очередь отправки в этот личный диалог.")
            else:
                self.emit(uid, self.report_text(self.report(target, start, end), spec))
        elif cmd == "/запрос":
            r = self.c.execute("SELECT * FROM corrections WHERE id=%s", (int(args),)).fetchone()
            if not r:
                raise RuleError("Запрос не найден.")

            def pretty(v):
                lines = [
                    f"{self.stamp(datetime.fromisoformat(v['start']))} → "
                    + (self.stamp(datetime.fromisoformat(v["end"])) if v["end"] else "открыта")
                ]
                for a, b in v["breaks"]:
                    lines.append(
                        "Перерыв: "
                        + self.stamp(datetime.fromisoformat(a))
                        + " → "
                        + (self.stamp(datetime.fromisoformat(b)) if b else "открыт")
                    )
                return "\n".join(lines)

            self.emit(
                uid,
                f"Запрос №{r['id']}, сотрудник {r['employee_id']}, смена {r['shift_id']}\n"
                f"Причина: {r['reason']}\nБыло:\n{pretty(r['old_values'])}\nСтало:\n{pretty(r['new_values'])}\n"
                f"/принять {r['id']} или /отклонить {r['id']}",
            )
        else:
            page = int(args or "1")
            if not 1 <= page <= 100000:
                raise RuleError("Номер страницы должен быть положительным.")
            offset = (page - 1) * 20
            if cmd == "/сотрудники":
                rows = self.c.execute(
                    "SELECT * FROM employees ORDER BY max_id LIMIT 20 OFFSET %s", (offset,)
                ).fetchall()
                labels = {"pending": "ожидает одобрения", "approved": "одобрен", "blocked": "заблокирован"}
                text = "\n".join(
                    f"{r['max_id']}: {r['full_name'] or 'ФИО не введено'} — {labels[r['status']]}"
                    for r in rows
                )
            elif cmd == "/активные":
                rows = self.c.execute(
                    "SELECT * FROM shifts WHERE ended_at IS NULL ORDER BY id LIMIT 20 OFFSET %s", (offset,)
                ).fetchall()
                text = "\n".join(
                    f"Сотрудник {r['employee_id']}: {self.status(r['employee_id'])}" for r in rows
                )
            else:
                rows = self.c.execute(
                    "SELECT * FROM corrections WHERE status='pending' ORDER BY id LIMIT 20 OFFSET %s",
                    (offset,),
                ).fetchall()
                text = "\n".join(
                    f"№{r['id']}: сотрудник {r['employee_id']}, смена {r['shift_id']}. /запрос {r['id']}"
                    for r in rows
                )
            self.emit(uid, f"Страница {page}. Следующая: {cmd} {page + 1}\n" + (text or "Записей нет."))
