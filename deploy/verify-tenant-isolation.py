import asyncio

from userloop.config import load_config
from userloop.core.tenants import TenantStores, tenant_config


async def main() -> None:
    base = load_config()
    ts = TenantStores(base)
    for tid in ("main", "acme"):
        st = await ts.get(tid)
        backend = getattr(st.events, "backend", "?")
        users = (await st.counts()).get("users")
        events = (await st.counts()).get("events")
        print(f"{tid}: events_backend={backend} users={users} events={events}")
    print("main storage:", (tenant_config(base, ts.registry.get('main')).get('storage') or {}).get('events', {}).get('backend'))
    print("acme storage:", tenant_config(base, ts.registry.get('acme')).get('storage') or {})
    await ts.close_all()


asyncio.run(main())
