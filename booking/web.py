import json
import logging
from pathlib import Path
from aiohttp import web

from .access import validate_init_data
from .domain import OWNER_ID, Problem, current_shift, now_msk

STATIC = Path(__file__).with_name('static')


def make_app(service, access, token):
    @web.middleware
    async def security(request, handler):
        try:
            if request.path.startswith('/api/'):
                header = request.headers.get('Authorization', '')
                user = validate_init_data(header[4:] if header.startswith('tma ') else '', token)
                await access.require(user)
                request['user'] = user
                if request.path.startswith('/api/admin/'):
                    service.require_owner(user['id'])
            response = await handler(request)
        except Problem as exc:
            response = web.json_response({'error': str(exc)}, status=exc.status)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = web.json_response({'error': 'Некорректные данные запроса.'}, status=400)
        except web.HTTPException as exc:
            response = web.json_response({'error': exc.reason}, status=exc.status)
        except Exception:
            logging.exception('Booking HTTP request failed')
            response = web.json_response({'error': 'Не удалось выполнить действие. Попробуйте позже.'}, status=500)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'"
        return response

    app = web.Application(middlewares=[security], client_max_size=32 * 1024)

    async def body(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise Problem('Ожидается объект с полями.')
        return data

    async def index(request):
        return web.FileResponse(STATIC / 'index.html')

    async def asset(request):
        name = request.match_info['name']
        if name not in ('app.js', 'style.css'):
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    async def me(request):
        now = service.now()
        return web.json_response(dict(user=request['user'], is_owner=request['user']['id'] == OWNER_ID,
                                      shift_date=current_shift(now).isoformat(), now=now.isoformat(),
                                      registered=bool(service.settings()['chat_id'])))

    async def visits(request):
        data = await body(request)
        service.visit(request['user']['id'], data.get('session_id'))
        return web.json_response({'ok': True})

    async def bookings(request):
        return web.json_response(service.day_view(request.query.get('date')))

    async def mutate(request):
        data = await body(request)
        operation = {'POST': 'create', 'PUT': 'update', 'DELETE': 'delete'}[request.method]
        raw_id = request.match_info.get('id')
        if raw_id is not None and not raw_id.isdecimal():
            raise Problem('Некорректный номер брони.')
        result = service.mutate(request['user']['id'], operation, data,
                                request.headers.get('Idempotency-Key'), int(raw_id) if raw_id else None)
        return web.json_response(result)

    async def admin(request):
        return web.json_response(service.admin_data(request['user']['id']))

    async def settings(request):
        service.change_time(request['user']['id'], (await body(request)).get('digest_time'))
        return web.json_response({'ok': True})

    async def block(request):
        data = await body(request)
        service.block(request['user']['id'], data.get('user_id'), data.get('blocked'))
        # Access is revoked immediately on the server even if menu refresh fails.
        try:
            await access.menu({'id': data['user_id']})
        except Exception:
            logging.info('Menu update unavailable for user %s', data['user_id'])
        return web.json_response({'ok': True})

    async def bans(request):
        if request.method == 'POST':
            return web.json_response(service.add_ban(request['user']['id'], await body(request)))
        raw_id = request.match_info['id']
        if not raw_id.isdecimal():
            raise Problem('Некорректный номер запрета.')
        return web.json_response(service.remove_ban(request['user']['id'], int(raw_id)))

    async def digest(request):
        return web.json_response(service.manual_digest(request['user']['id'], (await body(request)).get('shift_date')))

    app.add_routes([web.get('/', index), web.get('/static/{name}', asset),
                    web.get('/api/me', me), web.post('/api/visits', visits),
                    web.get('/api/bookings', bookings), web.post('/api/bookings', mutate),
                    web.put('/api/bookings/{id}', mutate), web.delete('/api/bookings/{id}', mutate),
                    web.post('/api/admin/bans', bans), web.delete('/api/admin/bans/{id}', bans),
                    web.get('/api/admin/overview', admin), web.put('/api/admin/settings', settings),
                    web.put('/api/admin/access', block), web.post('/api/admin/digest', digest)])
    return app
