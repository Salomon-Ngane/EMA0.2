from aiohttp import web

async def handle_health(request):
    return web.Response(text="OK", status=200)

async def start_health_server(port: int = 8080):
    server = web.Application()
    server.router.add_get('/', handle_health)
    server.router.add_get('/health', handle_health)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
