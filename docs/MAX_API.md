# Проверенные контракты MAX

Документация проверена 02.11.2026, до реализации. Использован официальный сайт разработчиков MAX. Старый адрес `platform-api.max.ru` не используется. SDK не подключается: официальная обзорная страница рекомендует библиотеки JavaScript и Go; для Python здесь выбран HTTPX и небольшой собственный адаптер.

| Назначение | Контракт и источник |
| --- | --- |
| Адрес и токен | `https://platform-api2.max.ru`, `Authorization: <access_token>`; [обзор](https://dev.max.ru/docs-api) |
| Проверка подключения | `GET /me`; [описание бота](https://dev.max.ru/docs-api/methods/GET/me/) |
| Получение событий | `POST /subscriptions`, JSON `url`, `update_types`, `secret`; [Webhook](https://dev.max.ru/docs-api/methods/POST/subscriptions/) |
| Подлинность события | Сравнение `X-Max-Bot-Api-Secret` с секретом подписки; обязательное в приложении, constant-time |
| Приватность | `message.recipient.chat_type == dialog`; действующий пользователь: `message.sender.user_id` или `callback.user.user_id`; `bot_started.user.user_id` — запуск в диалоге; [Update](https://dev.max.ru/docs-api/objects/Update/) |
| Ответ сотруднику | `POST /messages?user_id=...`, JSON `text`, `attachments`; [сообщения](https://dev.max.ru/docs-api/methods/POST/messages/) |
| Кнопки | `attachments: [{type: inline_keyboard, payload: {buttons: [[{type: callback, text: ..., payload: ...}]]}}]`; [клавиатура](https://dev.max.ru/docs-api/use-cases/sending-messages/keyboard/) |
| Ответ на нажатие | `POST /answers?callback_id=...`, JSON `notification`; [callback](https://dev.max.ru/docs-api/methods/POST/answers/) |
| CSV | `POST /uploads?type=file` → `url`; POST multipart `data` на полученный URL → `token`; `POST /messages` с `{type:file,payload:{token:...}}`; [загрузка](https://dev.max.ru/docs-api/methods/POST/uploads/), [пример файла](https://dev.max.ru/docs-api/use-cases/sending-messages/media/) |

Webhook требует HTTPS, доверенный сертификат, порт 443, ответ HTTP 200 в пределах 30 секунд. Приложение возвращает 200 после фиксации события в PostgreSQL, обработка идет отдельным worker. При ошибке БД успешного подтверждения нет: MAX может доставить событие повторно. Документация описывает до 10 повторов доставки; после 8 часов без успешного ответа подписка может быть автоматически удалена. Следите за доступностью endpoint и подписки.

Long Polling в актуальной документации предназначен для разработки и тестирования, не production. Поэтому реализован только production Webhook; локально события проверяются через TestClient и настоящую PostgreSQL. Публичный сервер для тестов бизнес-логики не нужен.

Ограничения: 30 API-запросов/секунду суммарно, не более 2 сообщений/секунду в один диалог. Один активный отправитель ограничен 20 API-запросами/секунду и интервалом 0,6 секунды между сообщениями одному пользователю. Callback отправляется после ответа с результатом и ограничивается той же общей очередью. При 429 учитывается числовой Retry-After, при 5xx и `attachment.not.ready` — возрастающая задержка. Транспортные ошибки безопасно повторяются, но MAX не документирует ключ идемпотентности отправки сообщений: при потере HTTP-ответа возможно повторное уведомление. Данные учета при этом не дублируются.

Для загрузки CSV используются только HTTPS-хосты из `MAX_UPLOAD_HOSTS`, по умолчанию имена, приведенные в официальных примерах. Токен бота не передается на сервер загрузки, редиректы не выполняются. Если MAX вернет новый хост, сначала проверьте его по актуальной официальной документации, затем добавьте точное имя в настройку. Отчет не публикуется по открытой ссылке. Ошибка загрузки остается в очереди и журналах как код без содержимого отчета.

TLS всегда проверяется. По актуальному уведомлению MAX может потребоваться сертификат Минцифры в доверенных; загрузите его только из официального доверенного источника и укажите PEM в `MAX_CA_BUNDLE`. Приложение добавляет его к системным CA и не использует `verify=False`.
