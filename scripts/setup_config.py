"""Interactive first-run setup. Uses only Python's standard library."""
import getpass
import json
import os
from pathlib import Path
import secrets


def create_config(path, token):
    if not token or any(char in token for char in ('\r', '\n', '\0')):
        raise ValueError('Токен пуст или содержит недопустимый перенос строки. Файл не создан.')
    password = secrets.token_hex(24)
    values = {
        'MAX_BOT_TOKEN': token,
        'ADMIN_MAX_ID': '9223372036854775807',  # Unassigned bootstrap administrator.
        'MAX_WEBHOOK_SECRET': secrets.token_urlsafe(32),
        'ORG_TIMEZONE': 'Asia/Omsk',
        'PUBLIC_BASE_URL': 'https://bot.mayachok-detstva.ru',
        'POSTGRES_DB': 'maxbot',
        'POSTGRES_USER': 'maxbot',
        'POSTGRES_PASSWORD': password,
        'DATABASE_URL': f'postgresql://maxbot:{password}@db:5432/maxbot',
    }
    # O_EXCL preserves any existing configuration, including a symbolic link.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write('\n'.join(f'{key}={json.dumps(value, ensure_ascii=False)}' for key, value in values.items()) + '\n')


def main():
    path = Path(__file__).resolve().parent.parent / '.env'
    if path.exists() or path.is_symlink():
        print('Файл настроек уже существует. Он сохранен без изменений.')
        return
    token = getpass.getpass('Вставьте токен MAX и нажмите Enter (символы скрыты): ').strip()
    try:
        create_config(path, token)
    except (ValueError, FileExistsError) as error:
        print(str(error) if isinstance(error, ValueError) else 'Файл настроек уже существует. Он сохранен без изменений.')
        return
    print('Настройки сохранены. Токен не выводился на экран.')
    print('Администратор пока не назначен: следующим этапом получим ваш MAX ID.')


if __name__ == '__main__':
    main()
