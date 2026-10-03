"""Local browser-test fixture. Uses a fake Telegram client and a temporary database."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from aiohttp import web
from booking.access import Access
from booking.domain import MSK, OWNER_ID
from booking.service import Service
from booking.store import Store
from booking.web import make_app

if __name__=='__main__':
    with tempfile.TemporaryDirectory() as tmp:
        store=Store(Path(tmp)/'test.sqlite3')
        bot=SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')),set_chat_menu_button=AsyncMock())
        service=Service(store,bot,now=lambda:datetime(2026,10,3,15,tzinfo=MSK))
        service.register(-10042,'Синий Кот · команда',OWNER_ID)
        for table,name,start,guests,comment in [(1,'Анна Смирнова','19:00',4,'День рождения. Торт принесут с собой.'),(3,'Алексей Иванов','20:30',6,''),(3,'Мария Петрова','23:00',3,'Будут чуть позже, на связи.'),(7,'Дмитрий Соколов','01:00',2,'После полуночи'),(12,'Елена Волкова','21:00',5,'Поближе к музыке')]:
            service.mutate(OWNER_ID,'create',dict(shift_date='2026-10-03',table_no=table,guest_name=name,guest_link='https://t.me/example',guests=guests,start_time=start,end_time=None,comment=comment),str(uuid4()))
        web.run_app(make_app(service,Access(store,bot,'https://example.org'),'123456:test-secret'),host='127.0.0.1',port=8765,access_log=None)
