import hashlib
import hmac
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from booking.access import Access, validate_init_data
from booking.domain import MSK, OWNER_ID, Problem, current_shift, digest_texts, validate_booking
from booking.service import Service
from booking.store import Store
from booking.web import make_app

TOKEN='123456:test-secret'
def signed(uid=OWNER_ID,timestamp=None,**extra):
    data={'user':json.dumps({'id':uid,'first_name':'Тест','is_bot':False}),'auth_date':str(int(time.time() if timestamp is None else timestamp)),**extra}
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    data['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(data.items())).encode(),hashlib.sha256).hexdigest()
    return urlencode(data)
def booking(**changes):
    return dict(shift_date='2026-10-02',table_no=17,guest_name='Иван <Кот>',guest_link='@ivan',guests=4,start_time='19:00',end_time=None,comment='У окна')|changes

class DomainTests(unittest.TestCase):
    def test_weekend_boundary(self):
        for dt,expected in [('2026-10-03T01:00','2026-10-02'),('2026-10-03T02:59','2026-10-02'),('2026-10-03T03:00','2026-10-03'),('2026-10-03T19:00','2026-10-03'),('2026-10-04T02:59','2026-10-03'),('2026-10-05T00:00','2026-10-05')]:
            with self.subTest(dt=dt):self.assertEqual(str(current_shift(datetime.fromisoformat(dt).replace(tzinfo=MSK))),expected)
    def test_optional_fields_and_overnight(self):
        b=validate_booking(booking(start_time='01:00',guest_link=''));self.assertIsNone(b['end_time']);self.assertEqual(b['guest_link'],'')
        self.assertEqual(validate_booking(booking())['guest_link'],'https://t.me/ivan')
        self.assertEqual(validate_booking(booking(start_time='23:00',end_time='03:00'))['end_time'],'03:00')
    def test_invalid_data(self):
        for changes in ({'guests':0},{'guests':True},{'guests':1.5},{'table_no':18},{'guest_name':''},{'guest_link':'javascript:alert(1)'},{'start_time':'03:00'},{'start_time':'17:59'},{'end_time':'18:00'},{'shift_date':'2026-10-04','start_time':'01:00'},{'shift_date':'bad'},{'comment':'x'*1001}):
            with self.subTest(changes=changes),self.assertRaises(Problem):validate_booking(booking(**changes))
    def test_auth(self):
        self.assertEqual(validate_init_data(signed(),TOKEN)['id'],OWNER_ID)
        for raw in [signed()+'x',signed(timestamp=time.time()-43201),signed(timestamp=time.time()+60),signed()+'&auth_date=1',signed(user=json.dumps({'id':5,'is_bot':True})), '']:
            with self.assertRaises(Problem):validate_init_data(raw,TOKEN)
    def test_digest_unicode_size(self):
        parts=digest_texts(datetime(2026,10,2).date(),[validate_booking(booking(comment='😀'*1000,guest_name='😀'*160)) for _ in range(17)])
        self.assertGreater(len(parts),1);self.assertTrue(all(len(p.encode('utf-16-le'))//2<=4096 for p in parts));self.assertEqual(sum(p.count('Стол №') for p in parts),17)

class Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'b.sqlite3');self.now=datetime(2026,10,2,15,tzinfo=MSK)
        self.bot=SimpleNamespace(send_message=AsyncMock(),get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')),set_chat_menu_button=AsyncMock())
        self.s=Service(self.store,self.bot,now=lambda:self.now);self.s.register(-10042,'Персонал',OWNER_ID)
    async def asyncTearDown(self):self.tmp.cleanup()
    def create(self,**kwargs):return self.s.mutate(OWNER_ID,'create',booking(**kwargs),str(uuid4()))['booking']
    def texts(self):
        with self.store.connect() as db:return [r['text'] for r in db.execute('SELECT text FROM outbox ORDER BY id')]

class ServiceTests(Fixture):
    async def test_digest_once_after_restart(self):
        self.create();self.assertEqual(self.texts(),[]);self.now=self.now.replace(hour=16);self.s.schedule();self.s.schedule()
        self.s=Service(Store(self.store.path),self.bot,now=lambda:self.now);self.s.schedule();self.assertEqual(len(self.texts()),1);self.assertIn('броней — 1',self.texts()[0])
    async def test_empty_digest_then_new(self):
        self.now=self.now.replace(hour=16);self.s.schedule();self.assertEqual(self.texts(),[]);self.now+=timedelta(minutes=1);self.create()
        self.assertEqual(len(self.texts()),1);self.assertTrue(self.texts()[0].startswith('Новая бронь сегодня'))
    async def test_late_first_create(self):
        self.now=self.now.replace(hour=19);self.create();self.s.schedule();self.assertEqual(len(self.texts()),1);self.assertTrue(self.texts()[0].startswith('Новая бронь сегодня'))
    async def test_night_close(self):
        self.now=datetime(2026,10,3,2,59,tzinfo=MSK);self.create(start_time='02:59');self.assertTrue(self.texts()[-1].startswith('Новая бронь сегодня'))
        self.now+=timedelta(minutes=1);self.create(start_time='02:00');self.assertEqual(len(self.texts()),1)
    async def test_future_digest(self):
        self.now=self.now.replace(hour=19);self.create(shift_date='2026-10-03');self.assertEqual(self.texts(),[])
        self.now=datetime(2026,10,3,16,tzinfo=MSK);self.s.schedule();self.assertEqual(len(self.texts()),1)
    async def test_overlapping_and_sort(self):
        for start in ['01:00','19:00','19:00']:self.create(start_time=start)
        self.assertEqual([b['start_time'] for b in self.s.list_bookings('2026-10-02')],['19:00','19:00','01:00'])
    async def test_update_delete_conflict(self):
        b=self.create();self.now=self.now.replace(hour=17);self.s.schedule();payload=booking(table_no=2,version=1)
        updated=self.s.mutate(55,'update',payload,str(uuid4()),b['id'])['booking'];self.assertIn('Бронь изменена',self.texts()[-1]);self.assertIn('Стол №2',self.texts()[-1])
        with self.assertRaises(Problem) as e:self.s.mutate(55,'update',payload,str(uuid4()),b['id'])
        self.assertEqual(e.exception.status,409);self.s.mutate(55,'delete',{'version':updated['version']},str(uuid4()),b['id'])
        self.assertEqual(self.s.list_bookings('2026-10-02'),[]);self.assertIn('Бронь отменена',self.texts()[-1])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM audit WHERE booking_id=?',(b['id'],)).fetchone()[0],3)
    async def test_move_to_future(self):
        b=self.create();self.now=self.now.replace(hour=18);self.s.mutate(OWNER_ID,'update',booking(shift_date='2026-10-03',version=1),str(uuid4()),b['id'])
        self.assertIn('перенесена на другую смену',self.texts()[-1]);self.assertEqual(len(self.texts()),2)
    async def test_idempotency(self):
        key=str(uuid4());one=self.s.mutate(OWNER_ID,'create',booking(),key);self.assertEqual(self.s.mutate(OWNER_ID,'create',booking(),key),one);self.assertEqual(len(self.s.list_bookings('2026-10-02')),1)
        with self.assertRaises(Problem):self.s.mutate(OWNER_ID,'create',booking(guests=8),key)
    async def test_manual_and_changed_time(self):
        self.create();self.assertEqual(self.s.manual_digest(OWNER_ID,'2026-10-02')['queued'],1);self.s.change_time(OWNER_ID,'17:00')
        self.now=self.now.replace(hour=16);self.s.schedule();self.assertEqual(len(self.texts()),1)
        self.now=self.now.replace(hour=17);self.s.schedule();self.assertEqual(len(self.texts()),2)
        self.s.manual_digest(OWNER_ID,'2026-10-02');self.assertEqual(len(self.texts()),3)
        self.s.change_time(OWNER_ID,'18:00');self.now=self.now.replace(hour=18);self.s.schedule();self.assertEqual(len(self.texts()),3)
    async def test_admin_owner_only(self):
        for action in [lambda:self.s.register(-1,'x',55),lambda:self.s.manual_digest(55,'2026-10-02'),lambda:self.s.change_time(55,'12:00'),lambda:self.s.block(55,10,True),lambda:self.s.admin_data(55)]:
            with self.assertRaises(Problem) as e:action()
            self.assertEqual(e.exception.status,403)
        with self.assertRaises(Problem):self.s.block(OWNER_ID,OWNER_ID,True)
    async def test_access_revocation_membership_and_bot(self):
        a=Access(self.store,self.bot,'https://example.org');u={'id':55,'first_name':'Иван'};await a.require(u)
        self.s.block(OWNER_ID,55,True);self.assertFalse(await a.allowed(u));self.s.block(OWNER_ID,55,False);self.assertTrue(await a.allowed(u))
        self.bot.get_chat_member.return_value=SimpleNamespace(status='left');self.assertFalse(await a.allowed(u));self.assertTrue(await a.allowed({'id':OWNER_ID}));self.assertFalse(await a.allowed({'id':OWNER_ID,'is_bot':True}))
        self.bot.get_chat_member.side_effect=TelegramForbiddenError(method=SendMessage(chat_id=1,text='x'),message='Forbidden')
        with self.assertRaises(Problem) as e:await a.require(u)
        self.assertEqual(e.exception.status,503)
    async def test_personal_menu(self):
        a=Access(self.store,self.bot,'https://example.org');await a.menu({'id':55,'first_name':'Иван'})
        self.assertEqual(self.bot.set_chat_menu_button.call_args.kwargs['chat_id'],55);self.assertEqual(self.bot.set_chat_menu_button.call_args.kwargs['menu_button'].type,'web_app')
        self.s.block(OWNER_ID,55,True);await a.menu({'id':55});self.assertEqual(self.bot.set_chat_menu_button.call_args.kwargs['menu_button'].type,'commands')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT name FROM people WHERE id=55').fetchone()[0],'Иван')
    async def test_visits_unique_session(self):
        key=str(uuid4());self.s.visit(OWNER_ID,key);self.s.visit(OWNER_ID,key);self.s.visit(55,str(uuid4()));m=self.s.admin_data(OWNER_ID)['metrics'][0];self.assertEqual((m['opens'],m['people']),(2,2))
    async def test_delivery_retry_restart(self):
        self.now=self.now.replace(hour=17);self.create();self.bot.send_message.side_effect=TelegramForbiddenError(method=SendMessage(chat_id=1,text='x'),message='Forbidden')
        await self.s.deliver();self.assertEqual(self.s.admin_data(OWNER_ID)['pending'],1);self.now+=timedelta(minutes=1);self.bot.send_message.side_effect=None
        self.s=Service(Store(self.store.path),self.bot,now=lambda:self.now)
        with patch('booking.service.asyncio.sleep',new_callable=AsyncMock):await self.s.deliver()
        self.assertEqual(self.s.admin_data(OWNER_ID)['pending'],0);await self.s.deliver();self.assertEqual(self.bot.send_message.await_count,2);self.assertIsNone(self.bot.send_message.call_args.kwargs['parse_mode'])
    async def test_reregister(self):
        self.now=self.now.replace(hour=17);self.create();self.s.register(-10099,'Другой чат',OWNER_ID);self.assertEqual(self.texts(),[]);self.s.schedule();self.assertEqual(len(self.texts()),1)

class BanTests(Fixture):
    def ban(self, **changes):
        return self.s.add_ban(OWNER_ID, dict(shift_date='2026-10-02', start_time='20:00', end_time='22:00', reason='Закрытое мероприятие') | changes)['ban']

    async def test_boundaries_and_missing_end(self):
        self.ban()
        self.create(start_time='18:00', end_time='20:00')
        self.create(start_time='22:00')
        for start, end in [('19:00', None), ('19:00','21:00'), ('20:00','21:00'), ('21:00','22:00'), ('18:00','23:00')]:
            with self.subTest(start=start,end=end), self.assertRaisesRegex(Problem, 'Закрытое мероприятие'):
                self.create(start_time=start,end_time=end)
        self.assertEqual(len(self.s.list_bookings('2026-10-02')),2)

    async def test_night_intervals_and_other_shift(self):
        self.ban(start_time='23:00',end_time='01:00')
        self.create(start_time='01:00')
        self.create(shift_date='2026-10-03',start_time='00:30')
        with self.assertRaises(Problem): self.create(start_time='00:30',end_time='02:00')
        self.ban(start_time='01:00',end_time='03:00')
        with self.assertRaises(Problem): self.create(start_time='02:59')

    async def test_existing_locked_delete_allowed(self):
        b=self.create()
        ban=self.ban()
        row=self.s.day_view('2026-10-02')['bookings'][0]
        self.assertIn('Закрытое мероприятие',row['restriction'])
        for actor in [55, OWNER_ID]:
            with self.assertRaises(Problem):
                self.s.mutate(actor,'update',booking(shift_date='2026-10-03',version=1),str(uuid4()),b['id'])
        self.s.remove_ban(OWNER_ID,ban['id'])
        self.assertEqual(self.s.day_view('2026-10-02')['bookings'][0]['restriction'],'')
        self.s.mutate(55,'update',booking(guests=5,version=1),str(uuid4()),b['id'])
        self.ban()
        self.s.mutate(55,'delete',{'version':2},str(uuid4()),b['id'])
        self.assertEqual(self.s.list_bookings('2026-10-02'),[])

    async def test_changed_target_and_stale_form(self):
        b=self.create(start_time='22:00')
        self.ban()
        with self.assertRaises(Problem):
            self.s.mutate(55,'update',booking(version=1),str(uuid4()),b['id'])
        self.assertEqual(self.s.list_bookings('2026-10-02')[0]['version'],1)

    async def test_validation_authorization_full_shift(self):
        for changes in [{'reason':''},{'reason':' '*3},{'reason':'x'*501},{'end_time':None},{'start_time':'17:00'},{'end_time':'04:00'},{'end_time':'19:00'},{'shift_date':'bad'},{'shift_date':'2026-10-04','end_time':'01:00'}]:
            with self.subTest(changes=changes), self.assertRaises(Problem): self.ban(**changes)
        for action in [lambda:self.s.add_ban(55,{}), lambda:self.s.remove_ban(55,1)]:
            with self.assertRaises(Problem) as e: action()
            self.assertEqual(e.exception.status,403)
        self.ban(start_time='18:00',end_time='03:00')
        for time in ['18:00','23:59','00:00','02:59']:
            with self.assertRaises(Problem):self.create(start_time=time)

    async def test_persistence_dedup_and_audit(self):
        b=self.create(start_time='22:00')
        ban=self.ban();self.assertEqual(self.ban()['id'],ban['id'])
        self.s=Service(Store(self.store.path),self.bot,now=lambda:self.now)
        self.assertEqual(len(self.s.day_view('2026-10-02')['bans']),1)
        self.assertEqual(self.s.list_bookings('2026-10-02')[0]['id'],b['id'])
        with self.assertRaises(Problem):self.create()
        self.s.remove_ban(OWNER_ID,ban['id']);self.s.remove_ban(OWNER_ID,ban['id'])
        self.create()
        with self.store.connect() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT action FROM audit WHERE action LIKE 'ban_%'")],['ban_create','ban_delete'])

class HTTPTests(Fixture):
    async def asyncSetUp(self):
        await super().asyncSetUp();self.access=Access(self.store,self.bot,'https://example.org');self.client=TestClient(TestServer(make_app(self.s,self.access,TOKEN)));await self.client.start_server()
    async def asyncTearDown(self):await self.client.close();await super().asyncTearDown()
    async def test_auth_crud_revocation(self):
        response=await self.client.get('/api/bookings?date=2026-10-02');self.assertEqual(response.status,401)
        headers={'Authorization':'tma '+signed(55),'Idempotency-Key':str(uuid4())}
        response=await self.client.get('/api/admin/overview',headers=headers);self.assertEqual(response.status,403)
        response=await self.client.post('/api/bookings',headers=headers,json=booking());self.assertEqual(response.status,200);created=(await response.json())['booking']
        response=await self.client.get('/api/bookings?date=2026-10-02',headers=headers);self.assertEqual(len((await response.json())['bookings']),1);self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.s.block(OWNER_ID,55,True)
        for method,path in [('get','/api/bookings?date=2026-10-02'),('delete',f"/api/bookings/{created['id']}")]:
            response=await getattr(self.client,method)(path,headers=headers);self.assertEqual(response.status,403)
        response=await self.client.get('/api/admin/overview',headers={'Authorization':'tma '+signed()});self.assertEqual(response.status,200)
    async def test_ban_routes_staff_enforcement(self):
        owner={'Authorization':'tma '+signed()};staff={'Authorization':'tma '+signed(55)}
        payload=dict(shift_date='2026-10-02',start_time='18:00',end_time='03:00',reason='Частная вечеринка')
        r=await self.client.post('/api/admin/bans',headers=staff,json=payload);self.assertEqual(r.status,403)
        r=await self.client.post('/api/admin/bans',headers=owner,json=payload);self.assertEqual(r.status,200);ban=(await r.json())['ban']
        r=await self.client.get('/api/bookings?date=2026-10-02',headers=staff);self.assertEqual((await r.json())['bans'][0]['reason'],payload['reason'])
        r=await self.client.post('/api/bookings',headers=staff|{'Idempotency-Key':str(uuid4())},json=booking());self.assertEqual(r.status,409);self.assertIn(payload['reason'],(await r.json())['error'])
        r=await self.client.delete('/api/admin/bans/'+str(ban['id']),headers=staff);self.assertEqual(r.status,403)
        r=await self.client.delete('/api/admin/bans/'+str(ban['id']),headers=owner);self.assertEqual(r.status,200)
        r=await self.client.post('/api/bookings',headers=staff|{'Idempotency-Key':str(uuid4())},json=booking());self.assertEqual(r.status,200)

    async def test_static_and_validation(self):
        response=await self.client.get('/');self.assertEqual(response.status,200)
        response=await self.client.get('/static/../../bookings.sqlite3');self.assertEqual(response.status,404)
        response=await self.client.post('/api/bookings',headers={'Authorization':'tma '+signed(),'Idempotency-Key':str(uuid4())},json=[]);self.assertEqual(response.status,400)

if __name__=='__main__':unittest.main()
