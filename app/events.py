from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Event:
    key: str
    uid: int
    text: str
    at: datetime
    callback_id: str | None = None


def parse(update):
    """Reject group/channel actions and callbacks lacking original dialog context."""
    kind = update.get('update_type')
    if kind not in ('bot_started', 'message_created', 'message_callback'):
        return None
    at = datetime.fromtimestamp(update['timestamp'] / 1000, timezone.utc)
    if kind == 'bot_started':
        user = update['user']
        if user.get('is_bot'):
            return None
        return Event(f'start:{user["user_id"]}:{update["timestamp"]}', int(user['user_id']), '/menu', at)
    msg = update.get('message')
    if not msg or msg.get('recipient', {}).get('chat_type') != 'dialog':
        return None
    if kind == 'message_created':
        sender = msg['sender']
        if sender.get('is_bot'):
            return None
        return Event('message:' + msg['body']['mid'], int(sender['user_id']), msg['body'].get('text') or '', at)
    callback = update['callback']
    if callback['user'].get('is_bot'):
        return None
    uid = int(callback['user']['user_id'])
    # Identity comes from callback.user, never the bot author in message.sender.
    return Event(f'callback:{callback["callback_id"]}:{uid}:{callback.get("timestamp", update["timestamp"])}', uid, callback.get('payload', ''), at, callback['callback_id'])
