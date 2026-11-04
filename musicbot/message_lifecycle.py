# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

"""Track every authorized message and expire disposable texts after a delay."""
import asyncio
import logging

LOG=logging.getLogger('musicbot')

async def observe_message(update, context):
    service=context.application.bot_data['service']
    user,chat,message=update.effective_user,update.effective_chat,update.effective_message
    if not user or not chat or chat.type!='private' or user.id not in service.allowed or message is None:
        return
    if not isinstance(getattr(message,'message_id',None),int):return
    service.history.record_message(user.id,message,kind='command')
    if getattr(message,'text',None):
        service.history.schedule(user.id,chat.id,message.message_id,30)

async def _expire_loop(app):
    while True:
        try:
            await app.bot_data['service'].history.expire_due(app.bot)
        except asyncio.CancelledError:raise
        except Exception as exc:
            LOG.warning('message_expiration_failed type=%s',type(exc).__name__)
        await asyncio.sleep(5)

async def start_lifecycle(app):
    if '_message_expiration' not in app.bot_data:
        app.bot_data['_message_expiration']=asyncio.create_task(_expire_loop(app))

async def stop_lifecycle(app):
    task=app.bot_data.pop('_message_expiration',None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
